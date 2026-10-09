"""Agregat harian pasar reguler vs non-reguler -> research.market_segment_daily.

Sumber: ``/primary/TradingSummary/GetStockSummary`` (IDX). Satu baris per
emiten memuat BUKAN satu tapi dua pasar:

- **Reguler** — ``Volume`` / ``Value`` / ``Frequency`` (lelang terus-menerus +
  pra-closing); inilah angka yang dipakai seluruh layer PIT/backtest.
- **Non-reguler** — ``NonRegularVolume`` / ``NonRegularValue`` /
  ``NonRegularFrequency`` (pasar tunai + negosiasi), dilaporkan terpisah oleh
  IDX. Sebelum modul ini, kolomnya diabaikan sehingga total pasar di hero
  dashboard cuma merepresentasikan pasar reguler.

Validasi silang (5 Okt 2026): reguler + non-reguler =
Volume/Value "Saham" dari ``/primary/Home/GetTradeSummary`` — angka IDX
sendiri, bukan tafsiran. Karena itu agregat di sini tidak memakai sumber lain.

Modul ini sengaja hanya agregat level-pasar (bukan per emiten): angka per
emiten non-reguler tersimpan apa adanya di ``research.raw_eod`` (kolom
``nr_*``) kalau nanti dibutuhkan.
"""

from __future__ import annotations

from typing import Any

DDL = """
create table if not exists research.market_segment_daily (
    trade_date      date        not null,
    regular_volume  bigint,
    regular_value   numeric(24,4),
    regular_freq    bigint,
    nonreg_volume   bigint,
    nonreg_value    numeric(24,4),
    nonreg_freq     bigint,
    total_volume    bigint,
    total_value     numeric(24,4),
    stock_count     integer,
    source          text        not null default 'IDX',
    captured_at     timestamptz not null default now(),
    primary key (trade_date)
);
create index if not exists idx_market_segment_daily_date
    on research.market_segment_daily (trade_date desc);

-- kolom non-reguler per emiten di raw_eod (append-only: hanya tambah, tak
-- pernah mengubah baris lama yang sudah ada).
alter table research.raw_eod add column if not exists nr_volume bigint;
alter table research.raw_eod add column if not exists nr_value  numeric(24,4);
alter table research.raw_eod add column if not exists nr_freq   bigint;
"""


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
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def aggregate_day(rows: list[Any]) -> dict[str, Any]:
    """Jumlahkan baris EodStockRow satu tanggal -> total reguler & non-reguler.

    Emiten tanpa transaksi non-reguler dikontribusikan None (bukan 0) supaya
    "tidak ada data" tetap berbeda dari "memang nol" di aritmatika di bawah.
    """
    reg_vol = reg_val = reg_freq = None
    nr_vol = nr_val = nr_freq = None
    codes: set[str] = set()

    def _add(acc: float | None, v: Any) -> float | None:
        # ``float`` sudah mencakup ``int`` di anotasi (PEP 484 numeric tower).
        if v is None:
            return acc
        n = _num(v)
        return acc if n is None else (acc or 0) + n

    for r in rows:
        code = getattr(r, "code", None)
        if code:
            codes.add(code)
        reg_vol = _add(reg_vol, getattr(r, "volume", None))
        reg_val = _add(reg_val, getattr(r, "value", None))
        reg_freq = _add(reg_freq, getattr(r, "frequency", None))
        nr_vol = _add(nr_vol, getattr(r, "non_regular_volume", None))
        nr_val = _add(nr_val, getattr(r, "non_regular_value", None))
        nr_freq = _add(nr_freq, getattr(r, "non_regular_frequency", None))

    def _tot(a: float | None, b: float | None) -> float | None:
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    return {
        "regular_volume": _int(reg_vol),
        "regular_value": _num(reg_val),
        "regular_frequency": _int(reg_freq),
        "non_regular_volume": _int(nr_vol),
        "non_regular_value": _num(nr_val),
        "non_regular_frequency": _int(nr_freq),
        "total_volume": _int(_tot(reg_vol, nr_vol)),
        "total_value": _num(_tot(reg_val, nr_val)),
        "stock_count": len(codes),
    }


def ensure_table(cur: Any) -> None:
    cur.execute(DDL)


def upsert_day(cur: Any, trade_date: str, agg: dict[str, Any]) -> None:
    """Upsert agregat satu tanggal ISO (YYYY-MM-DD) — aman dijalankan ulang."""
    ensure_table(cur)
    cur.execute(
        """insert into research.market_segment_daily
               (trade_date, regular_volume, regular_value, regular_freq,
                nonreg_volume, nonreg_value, nonreg_freq,
                total_volume, total_value, stock_count, source)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'IDX')
           on conflict (trade_date) do update set
             regular_volume = excluded.regular_volume,
             regular_value  = excluded.regular_value,
             regular_freq   = excluded.regular_freq,
             nonreg_volume  = excluded.nonreg_volume,
             nonreg_value   = excluded.nonreg_value,
             nonreg_freq    = excluded.nonreg_freq,
             total_volume   = excluded.total_volume,
             total_value    = excluded.total_value,
             stock_count    = excluded.stock_count,
             captured_at    = now()""",
        (
            trade_date,
            agg["regular_volume"],
            agg["regular_value"],
            agg["regular_frequency"],
            agg["non_regular_volume"],
            agg["non_regular_value"],
            agg["non_regular_frequency"],
            agg["total_volume"],
            agg["total_value"],
            agg["stock_count"],
        ),
    )
