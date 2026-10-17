"""Tutup lubang sesi: hari kerja tanpa baris di ``research.raw_eod`` -> coba ulang.

Latar: IDX membalas 503 sesaat untuk sekitar 10% request, bahkan di dalam
jendela retensi 2 tahunnya. Kalau satu tanggal terlanjur dilewati, ia tinggal
lubang di panel riset — dan karena ``raw_eod`` hanya boleh punya satu baris per
(code, trade_date), lubang itu tidak muncul lagi di backfill berikutnya yang
melewati tanggal yang sudah terisi.

Yang dilakukan skrip ini:
1. Daftar hari kerja di rentang (default: min..max ``trade_date`` yang ada)
   yang belum punya satu baris pun.
2. Untuk tiap tanggal: ``ambil_hari`` dari ``scripts.backfill_raw_eod``
   (3 percobaan, backoff 0/30/90 s; status transport dikosongkan sebelum tiap
   percobaan, jadi kegagalan transport tidak lolos sebagai "libur bursa").
3. Tiap sesi ditulis dalam SATU transaksi (utuh atau tidak sama sekali); jumlah
   baris yang benar-benar tercatat dibandingkan dengan yang diterima IDX.
4. Hasil diklasifikasikan jujur:
   - dapat baris, semua tercatat   -> terisi
   - 200 tanpa baris               -> LIBUR bursa
   - baris tak lengkap tercatat    -> SEBAGIAN (hari TIDAK berhasil)
   - status lain / transport gagal -> LUBANG di sisi IDX (bukan libur!) — dicatat
     dan dutarakan di ringkasan supaya terlihat, bukan disembunyikan.
5. ``prices_pit`` disegarkan bila ada yang terisi.
6. Exit code jujur untuk scheduler: 0 hanya bila tidak ada hari yang gagal
   (libur bursa asli bukan kegagalan); 1 bila run berhenti karena penolakan
   beruntun (403/429) atau ada hari yang dicoba tapi tidak berhasil. Run
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
import os
import pathlib
import sys
import time
from datetime import date, timedelta

import psycopg

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from idx_scraper.client import IDXClient
from scripts.backfill_raw_eod import (
    _INSERT_RAW_EOD,
    _REJECT_REASON,
    _num,
    _ohl,
    _prev_close,
    ambil_hari,
    refresh_prices_pit,
)


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
    """Klasifikasi hasil satu hari SETELAH penyisipan.

    "terisi"  : baris diterima dan semuanya tercatat — masuk ``raw_eod`` ATAU
                dikarantina; keduanya tulisan yang disengaja.
    "sebagian": baris diterima tapi sebagian tidak tercatat -> hari TIDAK
                berhasil. Run berikutnya harus menarik ulang; jangan dianggap
                sudah ada (mode "tanggal ada, baris kurang, tanpa alarm" yang
                plan ini ada untuk membunuh).
    "libur"   : 200 tanpa baris = libur bursa asli.
    "lubang"  : transport/status lain tanpa baris = di sisi IDX, bukan libur.
    """
    if rows:
        return "terisi" if tertangani == len(rows) else "sebagian"
    if status == 200:
        return "libur"
    return "lubang"


def titik_lanjut(lubang: list[tuple[date, object]], sebagian: list[date]) -> date | None:
    """Tanggal paling awal yang belum berhasil, titik ulang untuk run berikutnya.

    Aman diulang dari sini: pre-cek melewati tanggal yang sudah punya baris dan
    hari libur di rentang itu cuma diputar ulang tanpa efek.
    """
    kandidat = [d for d, _ in lubang] + list(sebagian)
    return min(kandidat) if kandidat else None


def kode_keluar(*, berhenti_menolak: bool, lubang: int, sebagian: int) -> int:
    """1 bila run berhenti karena penolakan beruntun atau ada hari gagal.

    Libur bursa asli TIDAK dihitung gagal — 48 hari kerja tanpa sesi itu memang
    libur bursa, dan scheduler harus tetap membedakan run bersih dari run yang
    berhenti separuh jalan.
    """
    if berhenti_menolak or lubang or sebagian:
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

    dsn = os.environ["DATABASE_URL"]
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()
    cur.execute("select min(trade_date), max(trade_date) from research.raw_eod")
    lo, hi = cur.fetchone()
    lo = args.start_date or lo
    hi = args.end_date or hi

    if args.dates:
        lubang_tanggal = [date.fromisoformat(s.strip()) for s in args.dates.split(",") if s.strip()]
    else:
        lubang_tanggal = hilang(cur, lo, hi)
    print(f"[gap] rentang {lo} .. {hi} | hari kerja tanpa data: {len(lubang_tanggal)}", flush=True)
    print("      " + ", ".join(str(x) for x in lubang_tanggal), flush=True)
    if args.dry_run or not lubang_tanggal:
        return
    if args.limit:
        lubang_tanggal = lubang_tanggal[: args.limit]

    client = IDXClient()
    client.warmup()
    terisi: list[str] = []
    libur: list[str] = []
    lubang: list[tuple[date, object]] = []
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
            arsip = status_hari(rows, status, tersimpan + dikarantina)
            if arsip == "terisi":
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
        elif status == 200:
            libur.append(str(day))
            tolak_beruntun = 0
            print(f"  {i}/{len(lubang_tanggal)} {day} -> libur bursa", flush=True)
        else:
            lubang.append((day, status))
            print(f"  {i}/{len(lubang_tanggal)} {day} -> LUBANG IDX (status {status})", flush=True)
            if status in (403, 429):
                tolak_beruntun += 1
                if tolak_beruntun >= 2:
                    print(
                        "[gap] IDX menolak beruntun (403/429) — berhenti, coba lagi nanti. "
                        f"Lanjutkan: --start-date {day.isoformat()}",
                        flush=True,
                    )
                    berhenti_menolak = True
                    break
        time.sleep(args.pace)

    client.close()
    kode = kode_keluar(
        berhenti_menolak=berhenti_menolak, lubang=len(lubang), sebagian=len(sebagian)
    )
    print(
        f"[gap] terisi={len(terisi)} | libur={len(libur)} | lubang IDX={len(lubang)} "
        f"| sebagian={len(sebagian)}",
        flush=True,
    )
    if terisi:
        print("      terisi:", ", ".join(terisi), flush=True)
    if lubang:
        print("      lubang:", ", ".join(f"{d}:{s}" for d, s in lubang), flush=True)
    if sebagian:
        print("      sebagian:", ", ".join(str(d) for d in sebagian), flush=True)
    if terisi:
        refresh_prices_pit(dsn)

    if kode != 0:
        titik = titik_lanjut(lubang, sebagian)
        if titik is not None:
            print(
                f"[gap] ada hari yang belum berhasil. Lanjutkan: --start-date {titik.isoformat()}",
                flush=True,
            )
    sys.exit(kode)


if __name__ == "__main__":
    main()
