"""Orkestrator IC bulanan: run analisis -> simpan histori bobot -> reset cache.

Dipakai job cron bulanan di ``cli.cmd_serve`` dan subcommand ``idx ic``.
Terpisah dari ``research.ic`` agar modul engine tetap murni analitik.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from .composite import invalidate_caches
from .ic import run_ic_analysis
from .ic_history import save_ic_summary

WIB = timezone(timedelta(hours=7))


def run_and_store(dsn: str, horizons: list[int] | None = None) -> dict[str, Any]:
    """Run IC analysis lengkap lalu simpan ke research.factor_ic_history.

    Returns ringkasan (rows, weights, run_date) untuk logging/Telegram.
    Idempoten per tanggal: run ulang di hari yang sama menimpa baris lama.
    """
    horizons = horizons or [5, 10]
    report = run_ic_analysis(dsn, horizons=horizons)

    now_wib = datetime.now(WIB).replace(microsecond=0, tzinfo=None)
    daily = report.daily_ic.get(horizons[0])
    run_date = daily["date"].max() if daily is not None and not daily.empty else None
    # run_date = tanggal bursa terakhir yang masuk analisis (bukan tanggal run)
    # agar histori selalu merujuk snapshot data, bukan waktu eksekusi.

    stored_rows = 0
    weights: dict[str, float] = {}
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("set time zone 'Asia/Jakarta'")
        # pastikan tabel ada (idempoten) walau DDL belum dijalankan manual
        cur.execute(
            """
            create table if not exists research.factor_ic_history (
                run_date      DATE        NOT NULL,
                factor        TEXT        NOT NULL,
                horizon       INTEGER     NOT NULL,
                mean_ic       NUMERIC(12,6),
                icir          NUMERIC(12,6),
                t_stat        NUMERIC(12,6),
                hit_rate      NUMERIC(8,6),
                n_days        INTEGER,
                eligible      BOOLEAN     NOT NULL DEFAULT false,
                weight        NUMERIC(8,6) NOT NULL DEFAULT 0,
                generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (run_date, factor, horizon)
            )
            """
        )
        stored_rows, weights = save_ic_summary(
            cur, run_date, {h: report.summaries[h].to_dict("records") for h in horizons}
        )

    # bobot baru tersimpan -> cache composite harus di-reset
    invalidate_caches()
    return {
        "run_date": str(run_date) if run_date is not None else None,
        "rows": stored_rows,
        "weights": weights,
        "generated_at_wib": now_wib.isoformat() + "+07:00",
    }


def main() -> int:  # pragma: no cover - CLI kecil untuk debugging
    import argparse

    ap = argparse.ArgumentParser(prog="monthly_ic")
    ap.add_argument("--k", type=int, nargs="+", default=[5, 10])
    args = ap.parse_args()
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=__import__("sys").stderr)
        return 1
    out = run_and_store(dsn, horizons=args.k)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
