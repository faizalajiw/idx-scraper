"""Tests for the grade-A recommendation alert latch + message formatting."""

from __future__ import annotations

from pathlib import Path

from idx_scraper.notify import format_recommendation_message
from idx_scraper.recommendation_state import RecommendationState


def row(code: str, grade: str, **overrides):
    r = {
        "code": code,
        "name": f"{code} Tbk",
        "grade": grade,
        "score": 78.0,
        "entry_low": 1000.0,
        "entry_high": 1010.0,
        "entry_ref": 1005.0,
        "stop": 900.0,
        "target": 1215.0,
        "rr": 2.0,
        "horizon_days": 21,
        "date": "2026-10-06",
        "grade_change": None,
    }
    r.update(overrides)
    return r


def test_baseline_run_seeds_without_alerting(tmp_path: Path):
    st = RecommendationState(tmp_path / "rec.json")
    # Run pertama: seluruh grade A hanya di-baseline, tidak ada event.
    assert st.evaluate([row("AAAA", "A"), row("BBBB", "B")]) == []
    # Grade A yang sudah tercatat tidak dibunyikan lagi.
    assert st.evaluate([row("AAAA", "A")]) == []


def test_entering_grade_a_fires_once(tmp_path: Path):
    st = RecommendationState(tmp_path / "rec.json")
    st.evaluate([row("AAAA", "B")])  # baseline B
    events = st.evaluate([row("AAAA", "A")])
    assert len(events) == 1
    assert events[0].code == "AAAA"
    assert events[0].previous_grade == "B"
    assert events[0].entry_ref == 1005.0
    # Sudah A -> tidak diulang.
    assert st.evaluate([row("AAAA", "A")]) == []


def test_rearm_after_leaving_grade_a(tmp_path: Path):
    st = RecommendationState(tmp_path / "rec.json")
    st.evaluate([row("AAAA", "A")])  # baseline
    assert st.evaluate([row("AAAA", "C")]) == []  # turun: bukan berita
    events = st.evaluate([row("AAAA", "A")])  # kembali masuk A -> bunyi
    assert len(events) == 1 and events[0].previous_grade == "C"


def test_state_persists_across_instances(tmp_path: Path):
    p = tmp_path / "rec.json"
    RecommendationState(p).evaluate([row("AAAA", "B")])
    st2 = RecommendationState(p)
    assert len(st2.evaluate([row("AAAA", "A")])) == 1


def test_format_message_includes_levels_and_disclaimer(tmp_path: Path):
    st = RecommendationState(tmp_path / "rec.json")
    st.evaluate([row("AAAA", "B")])
    events = st.evaluate([row("AAAA", "A")])
    msg = format_recommendation_message(events)
    assert "AAAA" in msg
    assert "1,010" in msg  # entry_high dari zona entry
    assert "1,215" in msg  # target
    assert "stop" in msg.lower()
    assert "bukan rekomendasi keuangan" in msg.lower()
