"""Dossier analisis satu emiten: gabungan sumber yang sudah ada, dengan tanggal sesi.

Modul ini TIDAK menghitung skor baru. Setiap bagian memanggil fungsi analitik
yang sudah ada, lalu menambahkan dua metadata:

- ``session_date``: tanggal sesi bursa data bagian itu (``YYYY-MM-DD`` atau None).
- ``stale``: True bila ``session_date`` lebih lama dari sesi terakhir pasar.

Satu bagian yang gagal tidak menjatuhkan dossier: bagian itu diberi
``error`` dan bagian lain tetap dikembalikan.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import analytics
from .database import get_cursor
from .sector_map import sector_for

SECTION_KEYS = (
    "profil",
    "valuasi",
    "faktor",
    "arus_asing",
    "aktivitas_broker",
    "sentimen",
    "catatan_risiko",
)


def latest_session_date() -> str | None:
    """Sesi terakhir pasar (dari ``research.latest_pit``). Sumber kebenaran kesegaran."""
    with get_cursor() as cur:
        return analytics._latest_eod_date(cur)


def mark_stale(session_date: str | None, latest: str | None) -> bool:
    """True bila data bagian lebih lama dari sesi terakhir. Tanpa tanggal = stale."""
    if latest is None:
        return False
    if session_date is None:
        return True
    return session_date < latest


def _sentimen(code: str, latest: str | None) -> dict[str, Any]:
    """Sentimen emiten. Tanggal bagian = tanggal buku order (bukan sesi), karena
    komponen buku bisa tertinggal dari arus asing."""
    d = dict(analytics.get_stock_decision(code).get("sentiment") or {})
    ob = analytics._latest_orderbook_factors().get(code) or {}
    ob_date = ob.get("ob_date")
    d["orderbook_date"] = ob_date
    d["orderbook_stale"] = (
        mark_stale(ob_date, latest)
        if (ob.get("ob_imbalance") is not None or ob.get("ob_absorption") is not None)
        else True
    )
    return d


def _section(
    name: str,
    fn: Callable[[], dict[str, Any]],
    session_of: Callable[[dict[str, Any]], str | None],
    latest: str | None,
) -> dict[str, Any]:
    """Jalankan satu bagian; tangkap galat agar bagian lain tetap jalan."""
    try:
        data = fn()
    except Exception as exc:  # satu bagian gagal tidak boleh menjatuhkan dossier
        print(f"[warn] dossier bagian {name} dilewati: {exc}", file=sys.stderr)
        return {"data": None, "session_date": None, "stale": True, "error": str(exc)}
    session = session_of(data) if data is not None else None
    return {
        "data": data,
        "session_date": session,
        "stale": mark_stale(session, latest),
        "error": None,
    }


def _profil(code: str) -> dict[str, Any]:
    with get_cursor() as cur:
        cur.execute(
            """select distinct on (code) code, name, trade_date from research.latest_pit
               where code = %s order by code, trade_date desc, knowledge_date desc""",
            (code,),
        )
        row = cur.fetchone()
    return {
        "code": code,
        "name": (row or {}).get("name"),
        "sector": sector_for(code),
        "trade_date": str(row["trade_date"]) if row and row.get("trade_date") else None,
    }


def _valuasi(code: str) -> dict[str, Any] | None:
    """Baris valuasi emiten bila masuk daftar undervalued/overvalued; None bila tidak."""
    res = analytics.get_valuation()
    for bucket in ("undervalued", "overvalued"):
        for r in res.get(bucket) or []:
            if str(r.get("code", "")).upper() == code:
                return {**r, "bucket": bucket}
    return None


def build_dossier(code: str) -> dict[str, Any]:
    """Susun dossier satu emiten. Setiap bagian membawa ``session_date`` dan ``stale``."""
    code = code.strip().upper()
    latest = latest_session_date()

    decision = _section(
        "keputusan",
        lambda: analytics.get_stock_decision(code),
        lambda d: latest,
        latest,
    )
    d = decision["data"] or {}

    sections: dict[str, Any] = {
        "profil": _section("profil", lambda: _profil(code), lambda _d: latest, latest),
        "valuasi": _section("valuasi", lambda: _valuasi(code) or {}, lambda _x: latest, latest),
        "faktor": _section("faktor", lambda: d.get("hold_check") or {}, lambda _d: latest, latest),
        "arus_asing": _section(
            "arus_asing",
            lambda: analytics.get_stock_foreign_flow(code, days=30, peer_days=10, peer_limit=10),
            lambda x: x.get("date") or latest,
            latest,
        ),
        "aktivitas_broker": _section(
            "aktivitas_broker",
            lambda: analytics.get_stock_broker_activity(code, 60),
            lambda _x: latest,
            latest,
        ),
        "sentimen": _section(
            "sentimen", lambda: _sentimen(code, latest), lambda x: x.get("orderbook_date"), latest
        ),
        "catatan_risiko": _section(
            "catatan_risiko",
            lambda: {
                "regime": d.get("regime"),
                "market_gauge": d.get("market_gauge"),
                "events": d.get("events") or [],
                "invalidation": d.get("invalidation"),
            },
            lambda _x: latest,
            latest,
        ),
    }
    return {
        "code": code,
        "latest_session": latest,
        "generated_at": datetime.now(analytics.WIB).isoformat(timespec="seconds"),
        "verdict": d.get("hold_check"),
        "sections": sections,
        "stale_sections": [k for k, v in sections.items() if v["stale"]],
    }
