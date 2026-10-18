"""Kesegaran data: satu dari tiga status, dihitung di backend (spec D1).

Permukaan dashboard harus bisa menjawab "angka ini masih bisa dipercaya?"
tanpa pemilik menebak. Satu status per basis data:

- ``segar``       — data berasal dari sesi pasar terakhir yang diharapkan.
- ``basi``        — berasal dari sesi yang lebih lama, dan sesi yang lebih
                    baru sudah ada.
- ``tak diketahui`` — sesi pasar terakhir tidak bisa ditentukan.

**Aturan keras (spec D1):** verdict yang lahir dari data ``basi`` ditahan —
yang tampil hanya angka, tanggalnya, dan sebabnya. Bagian yang bersumber
indeks (mis. kartu rezim) membawa penandanya sendiri lewat ``index_status``
dan tidak menularkan statusnya ke halaman.

Jam batas EOD dihormati: sebelum ``EOD_DEADLINE_HOUR``, sesi hari ini belum
wajib ada, jadi sesi yang diharapkan adalah sesi bursa sebelumnya — kalau
tidak, setiap pagi bursa seluruh papan akan terbaca "basi" tanpa sebab.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .health import EOD_DEADLINE_HOUR, expected_trading_date

SEGAR = "segar"
BASI = "basi"
TAK_DIKETAHUI = "tak diketahui"


def expected_session(now: datetime, holidays: set[date] | None = None) -> date:
    """Sesi bursa yang seharusnya sudah punya data EOD pada ``now``.

    Hari ini belum dihitung sebelum jam batas EOD; akhir pekan dan libur
    bursa dilewati oleh :func:`health.expected_trading_date`.
    """
    today = expected_trading_date(now, holidays)
    if now.date() == today and now.hour < EOD_DEADLINE_HOUR:
        return expected_trading_date(now - timedelta(days=1), holidays)
    return today


def _status(latest: str | None, expected: str | None) -> str:
    """Status satu basis data. Tanggal ISO (``YYYY-MM-DD``) aman dibanding teks."""
    if latest is None or expected is None:
        return TAK_DIKETAHUI
    return SEGAR if latest >= expected else BASI


def _reason(status: str, panel_latest: str | None, expected: str | None) -> str:
    if status == SEGAR:
        return f"data dari sesi terakhir yang diharapkan ({expected})"
    if status == BASI:
        return f"data dari {panel_latest}, sesi yang lebih baru sudah lewat ({expected})"
    if expected is None:
        return "sesi pasar terakhir tidak bisa ditentukan"
    return "tidak ada data sesi sama sekali"


def verdict(
    panel_latest: str | None,
    expected: str | None,
    *,
    index_latest: str | None = None,
) -> dict[str, Any]:
    """Tiga status kesegaran panel saham + penanda terpisah untuk basis indeks.

    ``withhold`` True berarti permukaan wajib menahan verdict-nya (spec D1);
    ``index_status`` memberi penanda sendiri pada bagian yang bersumber indeks
    dan tidak pernah mengubah ``status`` halaman.
    """
    status = _status(panel_latest, expected)
    return {
        "status": status,
        "withhold": status != SEGAR,
        "reason": _reason(status, panel_latest, expected),
        "panel_latest": panel_latest,
        "expected": expected,
        "index_latest": index_latest,
        "index_status": _status(index_latest, expected),
    }
