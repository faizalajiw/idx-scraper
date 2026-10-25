"""Analytics services for the IDX dashboard feature set.

All computations run on data already stored in Postgres (stock_summary_daily,
stock_quotes, index_quotes, stock_daily). Nothing here writes to the database.

Sectors: IDX-IC classification is not available from the free endpoints we
poll, so we map tickers to sectors with a curated dictionary of the most
liquid IDX names and fall back to "Lainnya" (other). The map lives in
sector_map.py and is intentionally data, not logic.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import pandas as pd

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))

# Cache in-process untuk event study & factors overview: query panel + compute
# butuh detik-an; data harian tidak berubah intraday. Key -> (monotonic_ts, val).
_RESEARCH_CACHE: dict[str, tuple[float, float, Any]] = {}
_RESEARCH_TTL = 3600.0  # 1 jam


# Single-flight: key -> event yang dibuka begitu hitungannya selesai. Dipakai

# supaya banyak permintaan yang datang bersamaan untuk cache yang sama tidak

# menghitung ulang masing-masing.

_INFLIGHT: dict[str, threading.Event] = {}

_INFLIGHT_LOCK = threading.Lock()


def _research_cache_get(key: str) -> Any | None:
    hit = _RESEARCH_CACHE.get(key)
    if hit and (time.monotonic() - hit[0]) < hit[1]:
        return hit[2]
    return None


def _research_cache_put(key: str, val: Any, ttl: float | None = None) -> None:
    """Simpan ke cache; ``ttl`` khusus untuk hitungan yang mahal & harian."""
    _RESEARCH_CACHE[key] = (time.monotonic(), ttl if ttl is not None else _RESEARCH_TTL, val)


def clear_research_cache() -> int:
    """Kosongkan cache in-process supaya menu langsung menyajikan data terbaru.

    Dipanggil serve loop tiap pipeline refresh (pra-buka / close sesi) — tanpa
    ini Sentimen & Jejak Sinyal bisa menampilkan data basi hingga 1 jam.
    """
    n = len(_RESEARCH_CACHE)
    _RESEARCH_CACHE.clear()
    return n


T = TypeVar("T")


def research_cache_once(
    key: str, compute: Callable[[], T], ttl: float | None = None, wait: float = 180.0
) -> T | None:
    """Hitung ``compute()`` sekali untuk satu key, walau banyak pemanggil bersamaan.

    Permintaan yang menemukan key sedang dihitung menunggu hasilnya, bukan
    menghitung ulang: papan dingin terukur 44-67 detik (tergantung beban saat
    itu), dan enam koneksi browser yang menghitung ulang bersamaan menahan enam
    koneksi pool (maks 10) sementara threadpool uvicorn 40, pool habis dan
    seluruh API macet.

    Gagal-terbuka: kalau yang menghitung mati atau lebih lambat dari ``wait``,
    penunggu menghitung sendiri. Bentuk keluaran tidak berubah, hanya jumlah
    hitungannya yang turun.
    """
    hit = _research_cache_get(key)
    if hit is not None:
        return hit

    with _INFLIGHT_LOCK:
        event = _INFLIGHT.get(key)
        pemilik = event is None
        if pemilik:
            event = threading.Event()
            _INFLIGHT[key] = event
    assert event is not None

    if not pemilik and event.wait(wait):
        hit = _research_cache_get(key)
        if hit is not None:
            return hit
        # kelamaan: hitung sendiri, sama seperti sebelum ada single-flight

    try:
        val = compute()
        _research_cache_put(key, val, ttl=ttl)
        return val
    finally:
        if pemilik:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(key, None)
            event.set()


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None and pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def _latest_eod_date(cur: Any) -> str | None:
    cur.execute("select max(trade_date) as d from research.latest_pit")
    row = cur.fetchone()
    return str(row["d"]) if row and row["d"] else None


def _norm_date(value: Any) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'; ISO/date passes through as ISO string."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10] if len(s) >= 10 else s


def _latest_index(cur: Any, code: str = "COMPOSITE") -> dict[str, Any] | None:
    cur.execute(
        """select close, change, percent, current, captured_at from index_quotes
           where code = %s and close is not null and close > 0
           order by captured_at desc limit 1""",
        (code,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "close": _f(r["close"]),
        "change": _f(r["change"]),
        "percent": _f(r["percent"]),
        "current": _f(r["current"]),
        "captured_at": r["captured_at"].isoformat() if r["captured_at"] else None,
    }
