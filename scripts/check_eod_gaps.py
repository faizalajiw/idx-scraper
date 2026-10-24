"""Tutup lubang sesi: hari kerja tanpa baris di ``research.raw_eod`` -> coba ulang.

Latar: IDX membalas 503 sesaat untuk sekitar 10% request, bahkan di dalam
jendela retensi 2 tahunnya. Kalau satu tanggal terlanjur dilewati, ia tinggal
lubang di panel riset — dan karena ``raw_eod`` hanya boleh punya satu baris per
(code, trade_date), lubang itu tidak muncul lagi di backfill berikutnya yang
melewati tanggal yang sudah terisi.

Yang dilakukan skrip ini:
1. Daftar hari kerja di rentang (default: min..max ``trade_date`` yang ada)
   yang belum punya satu baris pun. Tanggal yang lebih tua dari
   ``JENDELA_RETENSI_HARI`` (720) dilewati TANPA fetch: IDX menjawab 503 untuknya
   dan tanggalnya tak akan pernah bisa ditambal, jadi sweep tidak boleh terus
   membakar 3 request / ~120 s per tanggal mati itu. ``--dates`` yang disebut
   operator tetap dihormati (override eksplisit, tanpa potongan jendela).
2. Untuk tiap tanggal: ``ambil_hari`` dari ``scripts.backfill_raw_eod``
   (3 percobaan, backoff 0/30/90 s; status transport dikosongkan sebelum tiap
   percobaan, jadi kegagalan transport tidak lolos sebagai "libur bursa").
3. Tiap sesi ditulis dalam SATU transaksi (utuh atau tidak sama sekali); jumlah
   baris yang benar-benar tercatat dibandingkan dengan yang diterima IDX.
4. Hasil dipetakan lewat ember ``klasifikasi`` dari
   ``scripts.backfill_raw_eod`` (aturan status tidak diduplikasi di sini):
   - dapat baris, semua tercatat   -> terisi
   - baris tak lengkap tercatat    -> SEBAGIAN (hari TIDAK berhasil)
   - 200 tanpa baris               -> LIBUR bursa
   - 503                           -> LUAR JENDELA RETENSI (bukan lubang, tak
     akan pernah bisa ditambal, jadi tidak pernah memicu exit 1)
   - 403/429                       -> DIBLOKIR (Cloudflare; beruntun = berhenti)
   - status lain / transport gagal -> GAGAL di sisi IDX (bukan libur!) — dicatat
     dan dutarakan di ringkasan supaya terlihat, bukan disembunyikan.
5. ``prices_pit`` disegarkan bila ada yang terisi.
6. Exit code jujur untuk scheduler: 0 hanya bila tidak ada sesi yang tak
   tersimpan (libur bursa asli dan tanggal di luar jendela retensi bukan
   kegagalan); 1 bila run berhenti karena penolakan beruntun (403/429) atau ada
   sesi GAGAL / DIBLOKIR / tersimpan separuh — termasuk 403/429 tunggal yang
   tidak sampai menghentikan run, karena sesi itu memang tidak tersimpan. Run
   ``--dry-run`` dan run bersih selalu 0.

Pemakaian:
    # lihat dulu tanpa menyentuh IDX
    .venv/Scripts/python.exe -m scripts.check_eod_gaps --dry-run

    # tarik lubang (jeda 4 s); --limit untuk menguji cepat
    .venv/Scripts/python.exe -m scripts.check_eod_gaps --limit 10
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import date, datetime, timedelta

import psycopg

from idx_scraper import db

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from idx_scraper.client import IDXClient
from scripts.backfill_raw_eod import (
    _INSERT_RAW_EOD,
    _REJECT_REASON,
    WIB,
    _num,
    _ohl,
    _prev_close,
    ambil_hari,
    klasifikasi,
    refresh_prices_pit,
)

# Retensi IDX ±2 tahun bergulir. Lebih tua dari ini IDX menjawab 503 dan
# tanggalnya tak akan pernah bisa ditambal — jangan di-fetch sama sekali.
JENDELA_RETENSI_HARI = 720


def dalam_jendela(d: date, hari_ini: date) -> bool:
    """True bila ``d`` masih di dalam jendela retensi IDX (±720 hari, inklusif)."""
    return d >= hari_ini - timedelta(days=JENDELA_RETENSI_HARI)


def load_env() -> None:
    env_path = _REPO_ROOT / ".env"
    if env_path.exists():
        import dotenv

        dotenv.load_dotenv(str(env_path), override=False)


def hilang(cur, lo: date, hi: date) -> list[date]:
    cur.execute("select distinct trade_date from research.raw_eod")
    ada = {r[0] for r in cur.fetchall()}
    out: list[date] = []
    d = lo
    while d <= hi:
        if d.weekday() < 5 and d not in ada:
            out.append(d)
        d += timedelta(days=1)
    return out


def status_hari(rows: list | None, status: object, tertangani: int) -> str:
    """Ember hasil satu hari SETELAH penyisipan.

    "terisi"  : baris diterima dan semuanya tercatat — masuk ``raw_eod`` ATAU
                dikarantina; keduanya tulisan yang disengaja.
    "sebagian": baris diterima tapi sebagian tidak tercatat -> hari TIDAK
                berhasil. Run berikutnya harus menarik ulang; jangan dianggap
                sudah ada (mode "tanggal ada, baris kurang, tanpa alarm" yang
                plan ini ada untuk membunuh).

    Sisanya diambil apa adanya dari ``klasifikasi`` di ``scripts.backfill_raw_eod``
    supaya aturan status hanya hidup di satu tempat: "libur" (200 tanpa baris),
    "luar-jendela" (503 — tak akan pernah bisa ditambal, jadi BUKAN lubang),
    "diblokir" (403/429), "gagal" (status lain / transport).
    """
    ember = klasifikasi(rows, status)
    if ember != "data":
        return ember
    return "terisi" if tertangani == len(rows) else "sebagian"


def titik_lanjut(belum_berhasil: list[tuple[date, object]], sebagian: list[date]) -> date | None:
    """Tanggal paling awal yang belum berhasil, titik ulang untuk run berikutnya.

    Isi ``belum_berhasil`` = hari GAGAL + hari DIBLOKIR (libur bursa dan
    luar-jendela tidak ikut: keduanya memang bukan pekerjaan yang tertunda).
    Aman diulang dari sini: pre-cek melewati tanggal yang sudah punya baris dan
    hari libur di rentang itu cuma diputar ulang tanpa efek.
    """
    kandidat = [d for d, _ in belum_berhasil] + list(sebagian)
    return min(kandidat) if kandidat else None


def kode_keluar(*, berhenti_menolak: bool, gagal: int, diblokir: int, sebagian: int) -> int:
    """1 bila run berhenti karena penolakan beruntun atau ada sesi yang tak tersimpan.

    Tiga sebab sesi tak tersimpan: ``gagal`` (status lain / transport), ``diblokir``
    (403/429 — termasuk yang tidak sampai menghentikan run) dan ``sebagian``
    (tersimpan separuh). Libur bursa asli dan tanggal di luar jendela retensi
    (503) TIDAK dihitung gagal: keduanya memang tak bisa ditambal, dan alarm
    permanen untuknya membuat sinyal scheduler merah abadi (temuan I1 review final).
    """
    if berhenti_menolak or gagal or diblokir or sebagian:
        return 1
    return 0


def simpan_hari(conn: psycopg.Connection, day: date, rows: list) -> tuple[int, int]:
    """Tulis SATU sesi dalam satu transaksi: utuh atau tidak sama sekali.

    Autocommit koneksi tidak menghalangi — psycopg3 menjalankan blok ini dengan
    transaksi eksplisit. Interupsi di tengah sesi menggelinding balik seluruh
    barisnya, jadi tidak ada lagi sesi yang tersimpan separuh lalu terlewat
    selamanya oleh run berikutnya.

    ``on conflict (code, trade_date) do nothing`` menjaga idempotensi terhadap
    ``idx serve`` yang menulis tabel yang sama. Kembalikan
    (tersimpan di raw_eod, dikarantina); baris yang dilewati konflik tidak
    dihitung tersimpan.
    """
    tersimpan = 0
    dikarantina = 0
    with conn.transaction(), conn.cursor() as cur:
        for r in rows:
            reason = cur.execute(
                _REJECT_REASON,
                (r.open, r.high, r.low, r.close, r.volume),
            ).fetchone()[0]
            if reason is not None:
                cur.execute(
                    """insert into research.quarantine_eod (code, trade_date, payload, reason)
                       values (%s, %s, %s, %s)""",
                    (r.code, day, json.dumps(r.model_dump(mode="json")), reason),
                )
                dikarantina += 1
                continue
            cur.execute(
                _INSERT_RAW_EOD + " on conflict (code, trade_date) do nothing",
                (
                    r.code,
                    day,
                    _ohl(r.open),
                    _ohl(r.high),
                    _ohl(r.low),
                    _num(r.close),
                    _num(_prev_close(r)),
                    _num(r.volume),
                    _num(r.value),
                    _num(r.frequency),
                    r.source,
                    r.name,
                    _num(r.foreign_buy),
                    _num(r.foreign_sell),
                    _num(r.foreign_net),
                ),
            )
            tersimpan += cur.rowcount
    return tersimpan, dikarantina


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-date", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--end-date", type=date.fromisoformat, default=None, help="YYYY-MM-DD")
    ap.add_argument("--pace", type=float, default=4.0, help="jeda antar request (detik)")
    ap.add_argument("--limit", type=int, default=0, help="proses N tanggal pertama saja")
    ap.add_argument(
        "--dates",
        default="",
        help="tanggal spesifik dipisah koma (YYYY-MM-DD); menimpa pemindaian rentang",
    )
    ap.add_argument("--dry-run", action="store_true", help="hanya daftar lubang, tanpa fetch")
    args = ap.parse_args()

    dsn = db.dsn() if db.tersedia() else None
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("select min(trade_date), max(trade_date) from research.raw_eod")
    lo, hi = cur.fetchone()
    lo = args.start_date or lo
    hi = args.end_date or hi
    hari_ini = datetime.now(WIB).date()

    if args.dates:
        lubang_tanggal = [date.fromisoformat(s.strip()) for s in args.dates.split(",") if s.strip()]
        dilewati_luar_jendela = 0
    else:
        # Potongan jendela retensi: tanggal yang tak mungkin ditambal jangan
        # di-fetch sama sekali (503 + backoff 0/30/90 = ~120 s per tanggal mati).
        semua = hilang(cur, lo, hi)
        lubang_tanggal = [d for d in semua if dalam_jendela(d, hari_ini)]
        dilewati_luar_jendela = len(semua) - len(lubang_tanggal)
    print(f"[gap] rentang {lo} .. {hi} | hari kerja tanpa data: {len(lubang_tanggal)}", flush=True)
    print("      " + ", ".join(str(x) for x in lubang_tanggal), flush=True)
    if dilewati_luar_jendela:
        print(
            f"[gap] luar jendela retensi (>{JENDELA_RETENSI_HARI} hari): "
            f"{dilewati_luar_jendela} tanggal tidak di-fetch — IDX membalas 503 dan "
            "tanggalnya tak akan pernah bisa ditambal",
            flush=True,
        )
    if args.dry_run or not lubang_tanggal:
        return
    if args.limit:
        lubang_tanggal = lubang_tanggal[: args.limit]

    client = IDXClient()
    client.warmup()
    terisi: list[str] = []
    libur: list[str] = []
    luar_jendela: list[date] = []
    diblokir: list[tuple[date, object]] = []
    gagal: list[tuple[date, object]] = []
    sebagian: list[date] = []
    tolak_beruntun = 0
    berhenti_menolak = False

    for i, day in enumerate(lubang_tanggal, 1):
        cur.execute("select 1 from research.raw_eod where trade_date = %s limit 1", (day,))
        if cur.fetchone():
            print(f"  {i}/{len(lubang_tanggal)} {day} -> sudah ada, dilewati", flush=True)
            continue
        rows, status = ambil_hari(client, day.strftime("%Y%m%d"))
        if rows:
            tolak_beruntun = 0
            tersimpan, dikarantina = simpan_hari(conn, day, rows)
            ember = status_hari(rows, status, tersimpan + dikarantina)
            if ember == "terisi":
                terisi.append(f"{day}({tersimpan})")
                print(
                    f"  {i}/{len(lubang_tanggal)} {day} -> terisi {tersimpan}/{len(rows)} baris",
                    flush=True,
                )
            else:
                sebagian.append(day)
                print(
                    f"  {i}/{len(lubang_tanggal)} {day} -> SEBAGIAN "
                    f"{tersimpan}+{dikarantina} dari {len(rows)} baris — dianggap tidak berhasil",
                    flush=True,
                )
        else:
            ember = status_hari(rows, status, 0)
            if ember == "libur":
                libur.append(str(day))
                tolak_beruntun = 0
                print(f"  {i}/{len(lubang_tanggal)} {day} -> libur bursa", flush=True)
            elif ember == "luar-jendela":
                luar_jendela.append(day)
                print(
                    f"  {i}/{len(lubang_tanggal)} {day} -> luar jendela retensi (503), "
                    "bukan lubang",
                    flush=True,
                )
            elif ember == "diblokir":
                diblokir.append((day, status))
                print(
                    f"  {i}/{len(lubang_tanggal)} {day} -> DIBLOKIR IDX (status {status})",
                    flush=True,
                )
                tolak_beruntun += 1
                if tolak_beruntun >= 2:
                    print(
                        "[gap] IDX menolak beruntun (403/429) — berhenti, coba lagi nanti. "
                        f"Lanjutkan: --start-date {day.isoformat()}",
                        flush=True,
                    )
                    berhenti_menolak = True
                    break
            else:
                gagal.append((day, status))
                print(
                    f"  {i}/{len(lubang_tanggal)} {day} -> GAGAL di sisi IDX (status {status})",
                    flush=True,
                )
        time.sleep(args.pace)

    client.close()
    kode = kode_keluar(
        berhenti_menolak=berhenti_menolak,
        gagal=len(gagal),
        diblokir=len(diblokir),
        sebagian=len(sebagian),
    )
    print(
        f"[gap] terisi={len(terisi)} | libur={len(libur)} "
        f"| luar jendela retensi={len(luar_jendela)} "
        f"| diblokir={len(diblokir)} | gagal={len(gagal)} | sebagian={len(sebagian)}",
        flush=True,
    )
    if terisi:
        print("      terisi:", ", ".join(terisi), flush=True)
    if luar_jendela:
        print("      luar jendela:", ", ".join(str(d) for d in luar_jendela), flush=True)
    if diblokir:
        print("      diblokir:", ", ".join(f"{d}:{s}" for d, s in diblokir), flush=True)
    if gagal:
        print("      gagal:", ", ".join(f"{d}:{s}" for d, s in gagal), flush=True)
    if sebagian:
        print("      sebagian:", ", ".join(str(d) for d in sebagian), flush=True)
    if terisi:
        refresh_prices_pit(dsn)

    if kode != 0:
        titik = titik_lanjut(gagal + diblokir, sebagian)
        if titik is not None:
            print(
                f"[gap] ada hari yang belum berhasil. Lanjutkan: --start-date {titik.isoformat()}",
                flush=True,
            )
    sys.exit(kode)


if __name__ == "__main__":
    main()
