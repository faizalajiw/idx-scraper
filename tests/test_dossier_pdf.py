"""Tes renderer PDF dossier (tanpa database; data dummy)."""

from __future__ import annotations

from idx_scraper.api.dossier_pdf import _scalar_rows, format_value, render_dossier_pdf


def test_nested_object_is_flattened_not_dropped():
    rows = dict(_scalar_rows({"regime": {"label": "TRENDING_UP", "adx": 28.5}, "events": [{"a": 1}]}))
    assert rows["regime label"] == "TRENDING_UP"
    assert rows["regime adx"] == "28.50"
    assert rows["events"] == "1 item"


def _dossier(stale: bool = False, error: str | None = None) -> dict:
    section = {
        "data": {"name": "Bank Rakyat", "score": 62.5, "eligible": False, "reasons": ["a", "b"]},
        "session_date": "2026-10-08" if stale else "2026-10-09",
        "stale": stale,
        "error": error,
    }
    return {
        "code": "BBRI",
        "latest_session": "2026-10-09",
        "generated_at": "2026-10-12T16:00:00+07:00",
        "verdict": {"verdict": "HOLD", "score": 55, "reasons": ["x"]},
        "sections": {
            "profil": section,
            "valuasi": {"data": None, "session_date": None, "stale": True, "error": "boom"},
            "faktor": section,
            "arus_asing": section,
            "aktivitas_broker": section,
            "sentimen": section,
            "catatan_risiko": section,
        },
        "stale_sections": ["sentimen"] if stale else [],
    }


def test_format_value_handles_none_bool_and_floats():
    assert format_value(None) == "—"
    assert format_value(True) == "Ya"
    assert format_value(1234.5) == "1,234.50"


def test_render_returns_valid_pdf_bytes():
    pdf = render_dossier_pdf(_dossier())
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_render_survives_stale_and_failed_sections():
    pdf = render_dossier_pdf(_dossier(stale=True, error="gagal"))
    assert pdf.startswith(b"%PDF-")
