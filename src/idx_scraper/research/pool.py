"""Kolam kandidat: satu definisi "siapa yang berhak masuk papan" (spec D4).

Sebelum P4, likuiditas disaring di dalam ``recommend.score_candidate`` dengan
lantai absolut Rp 500 jt. Akibatnya definisi kelayakan hidup di tiga jalur yang
berbeda (papan, halaman emiten, jurnal harian) dan tidak pernah tertulis
seberapa banyak nama yang keluar. Modul ini menggantinya dengan **satu** kolam
eksplisit:

- **tebal** = nilai transaksi harian >= persentil-60 lintas saham **sesi itu**
  (bukan ambang tetap), dan
- **masuk kolam** = tebal di >= 3 dari 5 sesi terakhir sampai tanggal itu,
  punya histori >= :data:`recommend.MIN_MARKET_HISTORY`, sinyalnya bukan SELL,
  dan tidak sedang dikarantina.

Panel yang baru punya kurang dari 5 sesi (dataset muda, atau fixture uji)
mengecilkan syaratnya mengikuti sesi yang ada: ``min(THICK_MIN_SESSIONS, sesi
tersedia)``. Tanpa itu, panel muda menghasilkan kolam kosong - dan papan kosong
bukan "pilihan yang ketat", itu bug.

Grade A/B/C dihitung dari himpunan ini: **satu** kolam, bukan daftar terpisah
(spec 6.6).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.rows import dict_row

from .recommend import MIN_DAY_VALUE, MIN_MARKET_HISTORY

#: Persentil lintas saham yang memisahkan "tebal" dari "tipis" pada satu sesi.
THICK_VALUE_PERCENTILE = 60

#: Jendela sesi yang dinilai untuk ketebalan, dan berapa sesi tebal yang wajib.
THICK_WINDOW_SESSIONS = 5
THICK_MIN_SESSIONS = 3

#: Di bawah jumlah nilai berhingga ini, persentil lintas saham tidak bermakna:
#: ambang sesi jatuh ke lantai absolut ``MIN_DAY_VALUE``.
POOL_MIN_NAMES = 20

#: Rentang baca karantina EOD (dalam sesi; dikali 2 untuk hari kalender).
QUARANTINE_SESSIONS = 30

_QUARANTINE_SQL = """
select distinct code from research.quarantine_eod
 where trade_date >= current_date - %s::int
"""


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def value_thresholds(metrics: pd.DataFrame) -> dict[pd.Timestamp, float]:
    """Ambang "tebal" per sesi: persentil-60 nilai lintas saham sesi itu.

    Sesi dengan kurang dari :data:`POOL_MIN_NAMES` nilai berhingga memakai
    ``MIN_DAY_VALUE`` (lantai absolut yang sama dengan sebelum P4) supaya kolam
    tidak mengosongkan papan hanya karena panelnya belum lebar.
    """
    out: dict[pd.Timestamp, float] = {}
    if metrics.empty:
        return out
    for date_value, group in metrics.groupby("date", sort=True):
        values = [_finite(v) for v in group["value"]]
        finite = [v for v in values if v is not None]
        if len(finite) < POOL_MIN_NAMES:
            out[pd.Timestamp(date_value)] = MIN_DAY_VALUE
        else:
            out[pd.Timestamp(date_value)] = float(
                np.percentile(np.asarray(finite, dtype=float), THICK_VALUE_PERCENTILE)
            )
    return out


def thick_flags(metrics: pd.DataFrame) -> pd.Series:
    """True per baris bila nilai baris itu >= ambang sesinya (NaN -> False)."""
    if metrics.empty:
        return pd.Series(dtype=bool)
    thresholds = value_thresholds(metrics)
    dates = pd.to_datetime(metrics["date"])
    per_row = dates.map(lambda d: thresholds.get(pd.Timestamp(d), MIN_DAY_VALUE))
    values = pd.to_numeric(metrics["value"], errors="coerce")
    return (values >= per_row).fillna(False).astype(bool)


def _window_of(metrics: pd.DataFrame, target: pd.Timestamp) -> list[pd.Timestamp]:
    dates = pd.to_datetime(metrics["date"])
    available = sorted(pd.Timestamp(d) for d in dates.unique() if pd.Timestamp(d) <= target)
    return available[-THICK_WINDOW_SESSIONS:]


def pool_members(
    metrics: pd.DataFrame,
    date_value: Any,
    *,
    quarantined: set[str] | None = None,
) -> set[str]:
    """Kode yang berhak masuk papan pada ``date_value`` - satu-satunya kolam.

    Hanya memakai baris ``<= date_value`` (point-in-time, tanpa menengok ke
    depan). Kode masuk kolam bila tebal di >= :data:`THICK_MIN_SESSIONS` dari
    :data:`THICK_WINDOW_SESSIONS` sesi terakhir, historinya >=
    :data:`recommend.MIN_MARKET_HISTORY`, sinyalnya bukan SELL, dan tidak ada di
    ``quarantined``.
    """
    if metrics.empty:
        return set()
    target = pd.Timestamp(date_value)
    window = _window_of(metrics, target)
    if not window:
        return set()
    need = min(THICK_MIN_SESSIONS, len(window))

    dates = pd.to_datetime(metrics["date"])
    flags = thick_flags(metrics)
    thick = metrics[dates.isin(window) & flags]
    thick_sessions = thick.groupby("code")["date"].nunique()

    banned = {str(c).upper() for c in (quarantined or ())}
    members: set[str] = set()
    for row in metrics[dates == target].itertuples(index=False):
        code = str(row.code).upper()
        if code in banned:
            continue
        if int(thick_sessions.get(row.code, 0)) < need:
            continue
        history = _finite(getattr(row, "hist_days", None))
        if history is not None and history < MIN_MARKET_HISTORY:
            continue
        if getattr(row, "signal", None) == "SELL":
            continue
        members.add(code)
    return members


def _code_of(row: Any) -> str:
    """Kode dari baris apa pun bentuknya (dict_row atau tuple)."""
    return str(row["code"] if isinstance(row, Mapping) else row[0]).upper()


def quarantined_codes(cur: Any, sessions: int = QUARANTINE_SESSIONS) -> set[str]:
    """Kode berkarantina EOD dari satu cursor yang sudah dibuka pemanggil."""
    cur.execute(_QUARANTINE_SQL, (sessions * 2,))
    return {_code_of(row) for row in cur.fetchall()}


def load_quarantined_codes(dsn: str, sessions: int = QUARANTINE_SESSIONS) -> set[str]:
    """Sama seperti :func:`quarantined_codes`, tapi membuka koneksinya sendiri.

    Read-only dan jujur: kegagalan query dilempar ke pemanggil. Yang memutuskan
    "kolam tanpa karantina lebih baik daripada job mati" adalah pemanggil yang
    tahu konteksnya (job harian / analytics), bukan modul ini.
    """
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=15, row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        return quarantined_codes(cur, sessions=sessions)
