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
import os
import pathlib
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


def _yahoo_daily(start: date, end: date) -> dict[date, dict[str, float]]:
    """Bar harian ^JKSE -> {tanggal: {open, high, low, close}}."""
    df = yf.Ticker(YF_TICKER).history(
        start=(start - timedelta(days=14)).isoformat(),
        end=(end + timedelta(days=7)).isoformat(),
        auto_adjust=False,
    )
    df = df.dropna(how="all")
    bars: dict[date, dict[str, float]] = {}
    for ts, row in df.iterrows():
        close = row.get("Close")
        if close is None or close != close or float(close) <= 0:
            continue
        bars[ts.date()] = {
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(close),
        }
    return bars


def backfill(start: date, end: date) -> None:
    dsn = os.environ["DATABASE_URL"]
    bars = _yahoo_daily(start, end)
    if not bars:
        print("[idx] tidak ada bar dari Yahoo — tidak ada yang ditulis.")
        return
    print(f"[idx] yahoo {YF_TICKER}: {len(bars)} bar ({min(bars)} .. {max(bars)})")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
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


def main() -> None:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    args = ap.parse_args()
    backfill(args.start_date, args.end_date)


if __name__ == "__main__":
    main()
