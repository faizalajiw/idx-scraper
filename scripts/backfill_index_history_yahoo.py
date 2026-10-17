"""Isi riwayat harian IHSG (COMPOSITE) untuk rentang tanggal yang bolong.

Bedanya dengan dua skrip index yang sudah ada:
- ``backfill_index_eod.py`` (Yahoo) mengambil ``--days`` dari hari ini, dan
  ikut menulis satu snapshot ke ``index_quotes`` — di mesin yang jalur
  IDX-live-nya aktif, itu berisiko menimpa close resmi IDX yang dibaca
  dashboard (selisih beberapa poin).
- ``backfill_index_eod_idx.py`` (IDX) hanya bisa HARI INI: ``GetIndexList``
  selalu berarti "sekarang".
- Skrip ini mengisi ``index_summary_daily`` untuk rentang eksplisit dan
  TIDAK menyentuh ``index_quotes``. Baris yang sudah ada tidak ditimpa
  (``on conflict do nothing``), jadi nilai resmi IDX tetap menang.

Unique index ``(code, date)`` yang dibutuhkan ``on conflict`` dibuat skrip ini
sendiri (deploy baru tidak mati di insert pertama). ``sql/schema.sql`` kini juga
mendefinisikannya, jadi DB yang dibangun dari file itu sudah punya index-nya.

Bar yang ``Open``/``High``/``Low``/``Close``-nya kosong, NaN, atau tak positif
dibuang dan dihitung; kolom numeric Postgres tidak boleh menerima NaN.

Jendela yang tidak menghasilkan satu bar pun dianggap GAGAL (permintaan putus
atau rentang tanpa sesi): dilaporkan eksplisit dan keluar non-nol, tidak diam.

Provenance: baris yang diisi di sini berasal dari Yahoo ^JKSE — selisih
beberapa poin dari close resmi IDX (tabel tidak punya kolom source), dan
volume/value sengaja dibiarkan NULL (angka volume Yahoo bukan volume bursa).
Dipakai untuk benang sambung seri harian panjang (benchmark RRG, rezim), bukan
sebagai angka close yang ditampilkan dashboard.

Skrip ini juga melaporkan kesinambungan di periode tumpang tindih dengan baris
yang sudah ada (selisih >0.5% ditandai) sebagai jaring pengaman jahitan.

Jalankan (dari idx-scraper/; .env memuat DATABASE_URL):
    .venv/Scripts/python.exe -m scripts.backfill_index_history_yahoo \
        --start-date 2024-10-15 --end-date 2025-10-05
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import sys
from datetime import date, datetime, time, timedelta, timezone

import psycopg
import yfinance as yf

WIB = timezone(timedelta(hours=7))
CLOSE_T = time(16, 0, 0)  # pasca closing auction, konvensi index_summary_daily
YF_TICKER = "^JKSE"


def _load_env() -> None:
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        import dotenv

        dotenv.load_dotenv(str(env_path), override=False)


def _angka_positif(v) -> float | None:
    """``float(v)`` bila angka positif & finit; selain itu ``None``.

    Menangkap NaN/inf (``math.isfinite``), teks, dan nilai kosong supaya tidak
    ada yang lolos ke kolom ``numeric`` Postgres.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _tanggal(v) -> date:
    """Index Yahoo biasanya Timestamp; kalau bukan, tetap harus jadi ``date``."""
    return v.date() if hasattr(v, "date") else v


def _baris_valid(iterasi) -> tuple[dict[date, dict[str, float]], int]:
    """Saring bar mentah ``(tanggal, mapping)`` -> (bars valid, jumlah dilewati).

    Baris dengan ``Open``/``High``/``Low``/``Close`` kosong, NaN, bukan angka,
    atau tak positif dibuang: bar separuh (mis. ``High`` NaN) merusak seri
    harian dan NaN tidak boleh masuk kolom numeric. Murni — diuji in-memory.
    """
    bars: dict[date, dict[str, float]] = {}
    dilewati = 0
    for ts, row in iterasi:
        o = _angka_positif(row.get("Open"))
        h = _angka_positif(row.get("High"))
        lo = _angka_positif(row.get("Low"))
        c = _angka_positif(row.get("Close"))
        if o is None or h is None or lo is None or c is None:
            dilewati += 1
            continue
        bars[_tanggal(ts)] = {"open": o, "high": h, "low": lo, "close": c}
    return bars, dilewati


def _yahoo_daily(start: date, end: date) -> tuple[dict[date, dict[str, float]], int]:
    """Bar harian ^JKSE -> ({tanggal: {open, high, low, close}}, jumlah dilewati)."""
    df = yf.Ticker(YF_TICKER).history(
        start=(start - timedelta(days=14)).isoformat(),
        end=(end + timedelta(days=7)).isoformat(),
        auto_adjust=False,
    )
    if df is None or df.empty:
        return {}, 0
    return _baris_valid(df.dropna(how="all").iterrows())


def backfill(start: date, end: date) -> int:
    """Isi satu rentang dari Yahoo. Exit code: 0 bila ada bar tertulis, 1 bila gagal."""
    dsn = os.environ["DATABASE_URL"]
    try:
        bars, dilewati = _yahoo_daily(start, end)
    except Exception as exc:
        print(f"[idx] GAGAL: permintaan Yahoo error: {exc!r}", file=sys.stderr)
        return 1

    print(f"[idx] yahoo {YF_TICKER}: {len(bars)} bar valid, {dilewati} dilewati")
    if not bars:
        print(
            f"[idx] GAGAL: Yahoo tidak mengembalikan bar untuk {start} .. {end} — "
            "permintaan putus atau rentang benar-benar tanpa sesi. Tidak ada yang ditulis.",
            file=sys.stderr,
        )
        return 1
    print(f"[idx] bar {min(bars)} .. {max(bars)}")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        # 0) Pastikan unique index (code, date) ada sebelum insert dengan
        #    "on conflict". sql/schema.sql sekarang mendefinisikannya juga, tapi
        #    DB yang belum menjalankan file itu (atau versi lamanya) tetap perlu
        #    baris ini. Pola sama dengan backfill_index_eod.py:72 /
        #    backfill_index_eod_idx.py:146.
        cur.execute(
            """create unique index if not exists ux_index_summary_daily_code_date
               on index_summary_daily(code, date)"""
        )
        cur.execute("select date from index_summary_daily where code = 'COMPOSITE'")
        sudah = {r[0] for r in cur.fetchall()}

        baru = 0
        for d, b in sorted(bars.items()):
            if not (start <= d <= end) or d in sudah:
                continue
            cur.execute(
                """insert into index_summary_daily (code, close, open, high, low, date, captured_at)
                   values ('COMPOSITE', %s, %s, %s, %s, %s, %s)
                   on conflict (code, date) do nothing""",
                (
                    b["close"],
                    b["open"],
                    b["high"],
                    b["low"],
                    d,
                    datetime.combine(d, CLOSE_T, tzinfo=WIB),
                ),
            )
            baru += cur.rowcount
        print(f"[idx] baris baru di index_summary_daily: {baru}")

        # Jahitan: bandingkan hari yang sudah ada di rentang berdekatan dgn Yahoo.
        cur.execute(
            """select date, close from index_summary_daily
               where code = 'COMPOSITE' and date between %s and %s order by date""",
            (start, end + timedelta(days=31)),
        )
        menyimpang = []
        for d, close in cur.fetchall():
            b = bars.get(d)
            if b is None or float(close) == 0:
                continue
            if abs(b["close"] - float(close)) / float(close) * 100 > 0.5:
                menyimpang.append((d, float(close), b["close"]))
        print(f"[idx] hari tumpang tindih menyimpang >0.5%: {menyimpang or 'tidak ada'}")

        cur.execute(
            """select min(date)::text, max(date)::text, count(*) from index_summary_daily
               where code = 'COMPOSITE'"""
        )
        lo, hi, n = cur.fetchone()
        print(f"[idx] index_summary_daily COMPOSITE: {lo} .. {hi} ({n} sesi)")

    return 0


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    args = ap.parse_args()
    return backfill(args.start_date, args.end_date)


if __name__ == "__main__":
    raise SystemExit(main())
