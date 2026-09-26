"""Tests for the alert rule vocabulary, evaluation, and re-arm latch.

Pure logic — no database and no Telegram involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from idx_scraper.alert_rules import (
    RULE_TYPES,
    RuleSnapshot,
    RuleState,
    evaluate_rule,
    evaluate_rules,
    format_value,
    list_rule_types,
)


def rule(rule_type: str, threshold: float, code: str = "BBCA", rule_id: str = "r1"):
    return {"id": rule_id, "code": code, "type": rule_type, "threshold": threshold}


SNAPSHOT = RuleSnapshot(
    code="BBCA", close=6600.0, percent=1.25, rsi=62.5, vol_ratio=1.8, signal="BUY"
)


# ------------------------------------------------------------------ catalog


def test_rule_catalog_covers_the_advertised_conditions():
    assert set(RULE_TYPES) == {
        "price_above",
        "price_below",
        "rsi_above",
        "rsi_below",
        "volume_spike",
    }


def test_rule_catalog_is_serialisable_for_the_ui():
    catalog = list_rule_types()
    assert len(catalog) == len(RULE_TYPES)
    for entry in catalog:
        assert entry["id"] and entry["label"] and entry["description"]
        assert entry["unit"] in {"IDR", "RSI", "x"}
        if entry["min"] is not None:
            assert entry["default"] >= entry["min"]
        if entry["max"] is not None:
            assert entry["default"] <= entry["max"]


# ------------------------------------------------------------------ evaluation


@pytest.mark.parametrize(
    ("rule_type", "threshold", "expected"),
    [
        ("price_above", 6500.0, True),
        ("price_above", 6700.0, False),
        ("price_below", 6700.0, True),
        ("price_below", 6500.0, False),
        ("rsi_above", 60.0, True),
        ("rsi_above", 70.0, False),
        ("rsi_below", 70.0, True),
        ("rsi_below", 50.0, False),
        ("volume_spike", 1.5, True),
        ("volume_spike", 2.0, False),
    ],
)
def test_threshold_comparisons(rule_type, threshold, expected):
    result = evaluate_rule(rule(rule_type, threshold), SNAPSHOT)
    assert result.triggered is expected
    assert result.current is not None


def test_threshold_is_strict_not_inclusive():
    """Exactly at the level is not yet a crossing — matches how a stop reads."""
    assert evaluate_rule(rule("price_above", 6600.0), SNAPSHOT).triggered is False
    assert evaluate_rule(rule("price_below", 6600.0), SNAPSHOT).triggered is False
    assert evaluate_rule(rule("rsi_above", 62.5), SNAPSHOT).triggered is False


@pytest.mark.parametrize("rule_type", ["rsi_below", "volume_spike", "price_above"])
def test_missing_metric_never_triggers(rule_type):
    """Thin listings have no RSI/volume baseline — that must not alert."""
    blank = RuleSnapshot(code="NEWY")
    result = evaluate_rule(rule(rule_type, 10.0), blank)
    assert result.triggered is False
    assert result.current is None


def test_message_is_human_readable_and_names_the_metric():
    result = evaluate_rule(rule("price_above", 6500.0), SNAPSHOT)
    assert "BBCA" in result.message
    assert "6,600" in result.message
    assert "6,500" in result.message


def test_unknown_rule_type_raises():
    with pytest.raises(ValueError):
        evaluate_rule(rule("moon_phase", 1.0), SNAPSHOT)


def test_evaluate_rules_skips_unknown_types_and_missing_codes():
    rules = [
        rule("price_above", 100.0, rule_id="ok"),
        rule("moon_phase", 1.0, rule_id="bad"),
        rule("price_above", 100.0, code="ZZZZ", rule_id="nocode"),
    ]
    out = evaluate_rules(rules, {"BBCA": SNAPSHOT})
    assert [e.rule["id"] for e in out] == ["ok"]


def test_evaluate_rules_looks_codes_up_case_insensitively():
    out = evaluate_rules([rule("price_above", 100.0, code="bbca")], {"BBCA": SNAPSHOT})
    assert len(out) == 1


def test_format_value_matches_the_unit():
    assert format_value(6600.0, "IDR") == "6,600"
    assert format_value(62.45, "RSI") == "62.5"
    assert format_value(2.3, "x") == "2.30x"
    assert format_value(None, "IDR") == "-"


# ------------------------------------------------------------------ re-arm latch


def test_latch_fires_once_per_crossing(tmp_path: Path):
    state = RuleState(tmp_path / "state.json")
    triggered = evaluate_rule(rule("price_above", 100.0), SNAPSHOT)
    armed = evaluate_rule(rule("price_above", 9999.0), SNAPSHOT)

    assert len(state.newly_triggered([triggered])) == 1
    # Still triggered on the next check -> no repeat alert.
    assert state.newly_triggered([triggered]) == []
    # Condition clears -> the latch re-arms ...
    assert state.newly_triggered([armed]) == []
    # ... so the next crossing alerts again.
    assert len(state.newly_triggered([triggered])) == 1


def test_latch_persists_across_instances(tmp_path: Path):
    path = tmp_path / "state.json"
    triggered = evaluate_rule(rule("price_above", 100.0), SNAPSHOT)

    assert len(RuleState(path).newly_triggered([triggered])) == 1
    # A fresh instance (e.g. after a scheduler restart) must not re-alert.
    assert RuleState(path).newly_triggered([triggered]) == []
    assert json.loads(path.read_text(encoding="utf-8"))["r1"] is True


def test_latch_handles_never_triggered_rules_without_writing(tmp_path: Path):
    path = tmp_path / "state.json"
    state = RuleState(path)
    armed = evaluate_rule(rule("price_above", 9999.0), SNAPSHOT)
    assert state.newly_triggered([armed]) == []
    assert not path.exists()


def test_latch_survives_a_corrupt_state_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    triggered = evaluate_rule(rule("price_above", 100.0), SNAPSHOT)
    assert len(RuleState(path).newly_triggered([triggered])) == 1


def test_latch_ignores_evaluations_without_an_id(tmp_path: Path):
    state = RuleState(tmp_path / "state.json")
    orphan = evaluate_rule({"code": "BBCA", "type": "price_above", "threshold": 100.0}, SNAPSHOT)
    assert state.newly_triggered([orphan]) == []
