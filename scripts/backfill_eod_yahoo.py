"""Backfill EOD 2026-09-25 dari Yahoo Finance ke research.raw_eod + refresh prices_pit.

IDX diblok Cloudflare, jadi EOD 25 Sep hilang. Script ini nambal bar harian 25 Sep
dari Yahoo daily (source='Yahoo') untuk seluruh universe di research.raw_eod.
- name diwarisi dari record 24 Sep (Yahoo tak menyediakan nama emiten).
- foreign flow di-NULL-kan (Yahoo tak punya data foreign buy/sell).
- prev_close = close 24 Sep dari research (bukan dari Yahoo) supaya change konsisten.
Idempotent: kalau 25 Sep sudah ada dari IDX, script berhenti (tidak menimpa).
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg
import yfinance as yf

WIB = timezone(timedelta(hours=7))
TARGET = date(2026, 9, 25)
DSN = os.environ["DATABASE_URL"]


def _to_jk(code: str) -> str:
    code = code.strip().upper()
    return code if code.endswith(".JK") else f"{code}.JK"


def main() -> int:
    with psycopg.connect(DSN, autocommit=True) as conn, conn.cursor() as cur:
        # 0) guard: jangan timpa kalau 25 Sep sudah ada
        cur.execute("select count(*) from research.raw_eod where trade_date=%s", (TARGET,))
        if cur.fetchone()[0] > 0:
            print(f"[skip] {TARGET} sudah ada di raw_eod. Batal backfill.")
            return 0

        # 1) universe + nama + prev_close (dari latest_pit 24 Sep)
        cur.execute("select max(trade_date) from research.latest_pit")
        prev_date = cur.fetchone()[0]
        cur.execute(
            """select code, name, close
               from research.latest_pit
               where trade_date=%s""",
            (prev_date,),
        )
        universe = {r[0]: {"name": r[1], "prev_close": r[2]} for r in cur.fetchall()}
        codes = sorted(universe.keys())
        print(f"universe={len(codes)} kode, prev_date={prev_date}, target={TARGET}")

        # 2) batched Yahoo daily download (chunk biar tidak kena limit)
        target_rows: dict[str, dict] = {}
        CHUNK = 100
        for start in range(0, len(codes), CHUNK):
            chunk = codes[start : start + CHUNK]
            tickers = [_to_jk(c) for c in chunk]
            try:
                df = yf.download(
                    tickers=" ".join(tickers),
                    period="7d",
                    interval="1d",
                    group_by="ticker",
                    progress=False,
                    threads=True,
                    auto_adjust=False,
                )
            except Exception as e:
                print(f"[warn] chunk {start}-{start+len(chunk)} gagal: {e}", file=sys.stderr)
                continue

            single = len(tickers) == 1
            for code, tk in zip(chunk, tickers):
                try:
                    sub = df if single else df[tk]
                except Exception:  # ticker hilang dari batch -> skip senyap
                    continue
                sub = sub.dropna(how="all")
                if len(sub) == 0:
                    continue
                # cari baris dengan index tanggal == TARGET
                match = None
                for idx, row in sub.iterrows():
                    d = idx.date() if hasattr(idx, "date") else idx
                    if d == TARGET:
                        match = row
                        break
                if match is None:
                    continue

                def _f(row, key):
                    try:
                        v = float(row[key])
                        # NaN guard: NaN != NaN, bukan pembandingan diri biasa.
                        return None if (v != v) else v
                    except (KeyError, ValueError, TypeError):
                        return None

                close = _f(match, "Close")
                if close is None:
                    continue
                vol = _f(match, "Volume")
                target_rows[code] = {
                    "open": _f(match, "Open"),
                    "high": _f(match, "High"),
                    "low": _f(match, "Low"),
                    "close": close,
                    "volume": int(vol) if vol is not None else None,
                }
            print(f"  chunk {start}-{start+len(chunk)}: {len(target_rows)} total ketemu 25 Sep")

        if not target_rows:
            print("[abort] tidak ada bar 25 Sep dari Yahoo sama sekali.")
            return 1

        # 3) insert ke raw_eod (source='Yahoo'), prev_close & name dari research 24 Sep
        knowledge = datetime.now(WIB)
        inserted = 0
        quarantined = 0
        for code, bar in target_rows.items():
            meta = universe.get(code, {})
            prev_close = meta.get("prev_close")
            name = meta.get("name")
            # skip OHL yang 0/None untuk hari tanpa transaksi
            def _ohl(v):
                return v if v else None
            o, h, l = _ohl(bar["open"]), _ohl(bar["high"]), _ohl(bar["low"])
            # quality gate — sama seperti _ingest_eod_research
            cur.execute(
                "select research.eod_reject_reason(%s::numeric,%s::numeric,%s::numeric,%s::numeric,%s::bigint)",
                (o, h, l, bar["close"], bar["volume"]),
            )
            reason = cur.fetchone()[0]
            if reason is not None:
                cur.execute(
                    """insert into research.quarantine_eod
                       (code, trade_date, payload, reason)
                       values (%s,%s,%s,%s)""",
                    (
                        code, TARGET,
                        psycopg.types.json.Json(
                            {
                                "open": o, "high": h, "low": l,
                                "close": bar["close"], "volume": bar["volume"],
                                "source": "Yahoo",
                            }
                        ),
                        reason,
                    ),
                )
                quarantined += 1
                continue
            cur.execute(
                """insert into research.raw_eod
                   (code, trade_date, open, high, low, close, prev_close,
                    volume, value, frequency, source,
                    name, foreign_buy, foreign_sell, foreign_net, ingested_at)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    code, TARGET, o, h, l,
                    bar["close"], prev_close, bar["volume"], None, None, "Yahoo",
                    name, None, None, None, knowledge,
                ),
            )
            inserted += 1

        # 4) refresh prices_pit dari raw_eod (sama seperti _ingest_eod_research)
        conn.execute(
            """insert into research.prices_pit
                   (code, trade_date, knowledge_date, open, high, low, close, volume, value,
                    name, prev_close, foreign_buy, foreign_sell, foreign_net)
               select distinct on (code, trade_date)
                   code, trade_date, ingested_at, open, high, low, close, volume, value,
                   name, prev_close, foreign_buy, foreign_sell, foreign_net
               from research.raw_eod
               where trade_date=%s
               order by code, trade_date, ingested_at desc
               on conflict (code, trade_date, knowledge_date) do nothing""",
            (TARGET,),
        )
        print(f"[done] raw_eod +{inserted} (source=Yahoo), quarantine +{quarantined}, prices_pit refreshed untuk {TARGET}")
        # 5) verifikasi
        cur.execute(
            "select count(*) from research.latest_pit where trade_date=%s", (TARGET,)
        )
        print(f"latest_pit rows @ {TARGET}: {cur.fetchone()[0]}")
        cur.execute("select max(trade_date) from research.latest_pit")
        print(f"latest_pit max trade_date sekarang: {cur.fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
