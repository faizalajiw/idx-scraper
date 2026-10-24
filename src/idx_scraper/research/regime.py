"""Histori regime IHSG — persist hasil klasifikasi harian ke research.regime_daily.

Tujuan: statistik regime jangka panjang (berapa % waktu IHSG trending/ranging,
frekuensi transisi) yang tidak bisa dihitung dari klasifikasi on-the-fly.

- ``compute_daily``   : ADX + klasifikasi untuk SATU tanggal (backward-only).
- ``backfill``        : isi seluruh histori dari index_summary_daily (idempoten).
- ``record_today``    : upsert baris terbaru — dipanggil job harian scheduler.
- ``summary``         : agregat % waktu per regime + jumlah transisi.

Sumber harga: ``index_summary_daily`` (close resmi; window function ADX
membutuhkan 40+ bar historis sebelum tanggal target, semuanya <= t — no
look-ahead by construction).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import db
from ..analysis import ADX_TREND, IDX_TRADING_DAYS, calculate_adx, classify_regime

_DDL = """
create table if not exists research.regime_daily (
    trade_date  DATE PRIMARY KEY,
    regime      TEXT,
    adx         NUMERIC(8,2),
    plus_di     NUMERIC(8,2),
    minus_di    NUMERIC(8,2),
    realized_vol NUMERIC(8,2),
    vol_state   TEXT,
    source      TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_UPSERT = """
insert into research.regime_daily
    (trade_date, regime, adx, plus_di, minus_di, realized_vol, vol_state, source)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (trade_date) do update set
    regime = excluded.regime, adx = excluded.adx,
    plus_di = excluded.plus_di, minus_di = excluded.minus_di,
    realized_vol = excluded.realized_vol, vol_state = excluded.vol_state,
    source = excluded.source, generated_at = now()
"""


def _load_index_frame(cur: Any) -> pd.DataFrame:
    """Ambil OHLC IHSG; kolom numerik di-coerce float (Decimal -> float)."""
    cur.execute(
        """select date, open, high, low, close from index_summary_daily
           where code = 'COMPOSITE' order by date"""
    )
    rows = cur.fetchall()  # tuple rows (cursor default, bukan dict_row)
    return pd.DataFrame(
        [
            (
                str(r[0]),
                float(r[1]) if r[1] is not None else None,
                float(r[2]) if r[2] is not None else None,
                float(r[3]) if r[3] is not None else None,
                float(r[4]) if r[4] is not None else None,
            )
            for r in rows
        ],
        columns=["date", "open", "high", "low", "close"],
    )


def compute_daily(df: pd.DataFrame, upto_date: str) -> dict[str, Any] | None:
    """Klasifikasi regime pada ``upto_date`` memakai data <= tanggal itu saja."""
    win = df[df["date"] <= upto_date]
    if len(win) < 40:  # warmup ADX(14) + smoothing
        return None
    adx_df = calculate_adx(win)
    out = classify_regime(adx_df)
    out["date"] = upto_date
    return out


def backfill(dsn: str) -> int:
    """Isi research.regime_daily untuk seluruh histori index_summary_daily."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        df = _load_index_frame(cur)
        # kolom numerik psycopg menerima float; date sebagai string ISO
        written = 0
        for d in df["date"]:
            row = compute_daily(df, d)
            if row is None:
                continue
            cur.execute(
                _UPSERT,
                (
                    d,
                    row["regime"],
                    row["adx"],
                    row["plus_di"],
                    row["minus_di"],
                    row["realized_vol_annual"],
                    row["vol_state"],
                    "index_summary_daily",
                ),
            )
            written += 1
    return written


def record_today(dsn: str) -> dict[str, Any] | None:
    """Upsert klasifikasi untuk tanggal bursa terakhir (job harian scheduler)."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        df = _load_index_frame(cur)
        if df.empty:
            return None
        latest = df["date"].iloc[-1]
        row = compute_daily(df, latest)
        if row is None:
            return None
        cur.execute(
            _UPSERT,
            (
                latest,
                row["regime"],
                row["adx"],
                row["plus_di"],
                row["minus_di"],
                row["realized_vol_annual"],
                row["vol_state"],
                "index_summary_daily",
            ),
        )
        return row


def summary(dsn: str) -> dict[str, Any]:
    """% hari per regime, rata-rata ADX, dan jumlah transisi antar regime."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        cur.execute(
            """select trade_date, regime, adx, realized_vol from research.regime_daily
               where regime is not null order by trade_date"""
        )
        rows = cur.fetchall()
    if not rows:
        return {"days": 0, "share": {}, "transitions": 0, "avg_adx": None}

    dates = [r[0] for r in rows]
    regimes = [r[1] for r in rows]
    n = len(regimes)
    share: dict[str, float] = {}
    for rg in set(regimes):
        share[rg] = round(regimes.count(rg) / n * 100, 1)
    transitions = sum(1 for i in range(1, n) if regimes[i] != regimes[i - 1])
    adxs = [float(r[2]) for r in rows if r[2] is not None]
    return {
        "days": n,
        "first": str(dates[0]),
        "last": str(dates[-1]),
        "share": share,
        "transitions": transitions,
        "avg_adx": round(sum(adxs) / len(adxs), 1) if adxs else None,
        "trading_days_per_year": IDX_TRADING_DAYS,
    }


#: Rezim yang SECARA EKSPLISIT menolak masuk (spec 6.5 + keputusan pemilik Q5).
#: Hanya tren turun yang terkonfirmasi ADX; TRANSITION/RANGING/None tidak
#: pernah memblokir, dan VOLATILE cuma ditandai (plafon ukuran = lapis 3).
REJECTING_REGIMES: tuple[str, ...] = ("TRENDING_DOWN",)


def gate(
    regime: str | None,
    adx: float | None,
    vol_state: str | None = None,
    *,
    regime_fresh: bool = True,
) -> dict[str, Any]:
    """Gerbang rezim: boleh menolak, tapi tak boleh membisukan layar tanpa sebab.

    Aturan (spec 6.5 + keputusan pemilik Q5): halaman papan hanya diam saat
    rezim **eksplisit menolak** - tren turun yang terkonfirmasi ADX - dan
    alasannya disebut di layar. Rezim basi tidak pernah memblokir (spec D1:
    bagian berindeks membawa penandanya sendiri lewat ``regime_fresh``).
    """
    adx_ok = adx is not None and adx >= ADX_TREND
    blocked = bool(regime_fresh and regime in REJECTING_REGIMES and adx_ok)
    if blocked:
        reason = f"rezim menolak: tren turun terkonfirmasi (ADX {adx:.1f}) - tahan posisi baru"
    elif not regime_fresh and (regime is not None or adx is not None):
        reason = (
            "rezim basi: sesi indeks terakhir lebih lama dari sesi yang diharapkan"
            " - layar tidak dibisukan"
        )
    elif not regime_fresh:
        reason = "rezim belum terbaca: belum ada data indeks - layar tidak dibisukan"
    elif regime in REJECTING_REGIMES:
        reason = "arah turun, tapi ADX belum mengonfirmasi tren - layar tidak dibisukan"
    else:
        reason = f"rezim {regime or 'tidak terbaca'} tidak menolak - layar terbuka"
    return {
        "blocked": blocked,
        "reason": reason,
        "regime": regime,
        "adx": adx,
        "vol_state": vol_state,
        "volatile": vol_state == "VOLATILE",
        "regime_fresh": bool(regime_fresh),
    }
