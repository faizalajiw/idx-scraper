"""Ingest & store IDX broker summary EOD (bandarmologi) -> research.broker_daily.

IDX ``/primary/TradingSummary/GetBrokerSummary`` mengembalikan agregat
transaksi per broker firm untuk SATU tanggal bursa (seluruh pasar — IDX tidak
menyediakan breakdown per saham di endpoint publik; analitik flow per emiten
memakai faktor order-book dari snapshot intraday).

Modul ini:
- mem-parse payload-nya secara defensif (angka bisa datang sebagai string
  berformat "1,234,567"),
- memastikan tabel tujuan ada (DDL idempoten, mirror sql/research_schema.sql),
- upsert on (trade_date, broker_code) — aman dijalankan ulang.

Pemanggil: scripts.backfill_broker_eod (manual/CLI) dan job harian 16:30 WIB
di cli.cmd_serve.
"""

from __future__ import annotations

from typing import Any

DDL = """
create table if not exists research.broker_daily (
    trade_date  date not null,
    broker_code text not null,
    broker_name text,
    volume      bigint,
    value       numeric(24,4),
    frequency   bigint,
    source      text not null default 'IDX',
    captured_at timestamptz not null default now(),
    primary key (trade_date, broker_code)
);
create index if not exists idx_broker_daily_broker
    on research.broker_daily (broker_code, trade_date);
"""


def parse_broker_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse payload GetBrokerSummary -> rows bersih (skip baris tanpa IDFirm).

    Field hilang/korup -> None (NULL di DB), bukan 0 — membedakan "tidak ada
    data" dari "memang nol".
    """
    if not payload or not isinstance(payload.get("data"), list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        code = item.get("IDFirm")
        if code is None or (isinstance(code, str) and not code.strip()):
            continue
        out.append(
            {
                "broker_code": str(code).strip(),
                "broker_name": item.get("FirmName") or None,
                "volume": _int(item.get("Volume")),
                "value": _num(item.get("Value")),
                "frequency": _int(item.get("Frequency")),
            }
        )
    return out


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def ensure_table(cur: Any) -> None:
    cur.execute(DDL)


def upsert_broker_rows(cur: Any, trade_date: str, rows: list[dict[str, Any]]) -> int:
    """Upsert rows broker ke research.broker_daily untuk satu tanggal ISO."""
    ensure_table(cur)
    count = 0
    for r in rows:
        cur.execute(
            """insert into research.broker_daily
                   (trade_date, broker_code, broker_name, volume, value, frequency, source)
               values (%s, %s, %s, %s, %s, %s, 'IDX')
               on conflict (trade_date, broker_code) do update set
                 broker_name = excluded.broker_name,
                 volume = excluded.volume,
                 value = excluded.value,
                 frequency = excluded.frequency,
                 captured_at = now()""",
            (
                trade_date,
                r["broker_code"],
                r["broker_name"],
                r["volume"],
                r["value"],
                r["frequency"],
            ),
        )
        count += 1
    return count
