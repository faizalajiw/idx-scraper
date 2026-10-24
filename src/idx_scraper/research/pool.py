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
from collections import Counter
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from .. import db
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


def value_threshold_for(values: Any) -> float:
    """Ambang "tebal" SATU sesi dari nilai lintas sahamnya - satu aturannya.

    Sesi dengan kurang dari :data:`POOL_MIN_NAMES` nilai berhingga memakai
    ``MIN_DAY_VALUE`` (lantai absolut yang sama dengan sebelum P4) supaya kolam
    tidak mengosongkan papan hanya karena panelnya belum lebar.

    Ambang bersifat lintas saham **per sesi**, jadi ia tidak berubah karena
    tanggal yang ditanyakan berubah - inilah bagian yang boleh dihitung sekali
    lalu dipakai berkali-kali (lihat :class:`PoolIndex`).
    """
    finite = [_finite(v) for v in values]
    finite = [v for v in finite if v is not None]
    if len(finite) < POOL_MIN_NAMES:
        return MIN_DAY_VALUE
    return float(np.percentile(np.asarray(finite, dtype=float), THICK_VALUE_PERCENTILE))


def value_thresholds(metrics: pd.DataFrame) -> dict[pd.Timestamp, float]:
    """Ambang "tebal" per sesi: persentil-60 nilai lintas saham sesi itu."""
    out: dict[pd.Timestamp, float] = {}
    if metrics.empty:
        return out
    for date_value, group in metrics.groupby("date", sort=True):
        out[pd.Timestamp(date_value)] = value_threshold_for(group["value"])
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


def _window_of(sessions: list[pd.Timestamp], target: pd.Timestamp) -> list[pd.Timestamp]:
    """``THICK_WINDOW_SESSIONS`` sesi terakhir yang <= ``target`` (point-in-time)."""
    return [s for s in sessions if s <= target][-THICK_WINDOW_SESSIONS:]


class PoolIndex:
    """Satu panel kolam, disiapkan sekali, lalu ditanyai per tanggal.

    Ambang "tebal" bersifat lintas saham **per sesi**: menanyakan 126 tanggal
    tidak mengubah jawaban satu sesi pun. Menghitungnya per tanggal membuat
    jurnal harian membayar ulang kerja yang sama berkali-kali (temuan review:
    147 detik untuk 126 tanggal, hampir semuanya hitungan yang sama). Kelas ini
    menghitung tiap sesi sekali dan mengingatnya - aturannya tidak berubah, ia
    jalur yang sama dengan :func:`pool_members`.
    """

    __slots__ = ("_banned", "_dates", "_metrics", "_sessions", "_thick")

    def __init__(self, metrics: pd.DataFrame, *, quarantined: set[str] | None = None) -> None:
        self._metrics = metrics
        self._banned = {str(c).upper() for c in (quarantined or ())}
        self._thick: dict[pd.Timestamp, set[str]] = {}
        if metrics.empty:
            self._dates = pd.Series(dtype="datetime64[ns]")
            self._sessions: list[pd.Timestamp] = []
        else:
            self._dates = pd.to_datetime(metrics["date"])
            self._sessions = sorted(pd.Timestamp(d) for d in self._dates.unique())

    @property
    def sessions(self) -> list[pd.Timestamp]:
        """Sesi yang ada di panel, urut naik."""
        return list(self._sessions)

    def _thick_codes(self, session: pd.Timestamp) -> set[str]:
        """Kode yang nilainya >= ambang sesinya - dihitung SEKALI per sesi."""
        hit = self._thick.get(session)
        if hit is None:
            rows = self._metrics[self._dates == session]
            threshold = value_threshold_for(rows["value"])
            values = pd.to_numeric(rows["value"], errors="coerce")
            hit = {str(c).upper() for c in rows["code"][values >= threshold]}
            self._thick[session] = hit
        return hit

    def members(self, date_value: Any) -> set[str]:
        """Kode yang berhak masuk papan pada ``date_value`` (aturan :func:`pool_members`)."""
        if self._metrics.empty:
            return set()
        target = pd.Timestamp(date_value)
        window = _window_of(self._sessions, target)
        if not window:
            return set()
        need = min(THICK_MIN_SESSIONS, len(window))

        thick_sessions: Counter[str] = Counter()
        for session in window:
            thick_sessions.update(self._thick_codes(session))

        members: set[str] = set()
        for row in self._metrics[self._dates == target].itertuples(index=False):
            code = str(row.code).upper()
            if code in self._banned or thick_sessions[code] < need:
                continue
            history = _finite(getattr(row, "hist_days", None))
            if history is not None and history < MIN_MARKET_HISTORY:
                continue
            if getattr(row, "signal", None) == "SELL":
                continue
            members.add(code)
        return members


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

    Untuk banyak tanggal pakai :class:`PoolIndex`: aturannya sama persis, tapi
    ambang tiap sesi dibayar sekali, bukan sekali per tanggal.
    """
    return PoolIndex(metrics, quarantined=quarantined).members(date_value)


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
    with (
        db.connect(dict_rows=True) as conn,
        conn.cursor() as cur,
    ):
        return quarantined_codes(cur, sessions=sessions)
