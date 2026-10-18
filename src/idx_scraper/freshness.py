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

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from .health import EOD_DEADLINE_HOUR, WIB, expected_trading_date, parse_holidays

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


def _iso(row: Any) -> str | None:
    """Nilai `max(...) as d` (date/None) jadi teks ISO."""
    value = row["d"] if row else None
    return str(value) if value else None


def session_bases(cur: Any) -> tuple[str | None, str | None]:
    """Sesi terakhir dua basis: panel saham dan indeks (``COMPOSITE``).

    Query-nya sengaja sama dengan yang dipakai `idx health` /
    `api.services.get_data_health` supaya tidak pernah ada dua definisi
    kesegaran di repo ini.
    """
    cur.execute("select max(trade_date) as d from research.latest_pit")
    panel = cur.fetchone()
    cur.execute(
        "select max((captured_at at time zone 'Asia/Jakarta')::date) as d "
        "from index_quotes where code = 'COMPOSITE' "
        "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16"
    )
    return _iso(panel), _iso(cur.fetchone())


def current(cur: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Status kesegaran saat ini untuk satu cursor (tanpa I/O sendiri).

    Cursor wajib: pembungkus DB-nya hidup di `api.services.get_freshness`,
    supaya modul ini tetap murni dan tak bergantung pada lapisan api.
    """
    moment = now or datetime.now(WIB)
    panel_latest, index_latest = session_bases(cur)
    expected = expected_session(moment, parse_holidays()).isoformat()
    out = verdict(panel_latest, expected, index_latest=index_latest)
    out["checked_at"] = moment.isoformat(timespec="seconds")
    out["basis"] = "panel"
    return out


def format_message(f: Mapping[str, Any]) -> str:
    """Satu kalimat untuk Telegram/log: status + sebabnya (spec D1)."""
    return f"Kesegaran: {f['status']} — {f['reason']}."


def withhold(rows: list[dict[str, Any]], f: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Salinan baris papan dengan verdict ditahan bila datanya tidak segar (spec D1).

    Yang tampil hanya angka, tanggalnya, dan sebabnya: ``grade`` (label
    keyakinan) dan ``position_pct`` (ukuran) dilepas, sementara skor dan level
    entry/stop/target tetap. Salinan dibuat supaya dict yang hidup di cache
    30 menit tidak pernah termutasi.
    """
    out = [{**row} for row in rows]
    if not f.get("withhold"):
        return out
    for row in out:
        row["grade"] = None
        row["position_pct"] = None
        row["withheld"] = True
    return out
