"""Tests for the JSON-backed alert rule store (no database involved)."""

from __future__ import annotations

from pathlib import Path

from idx_scraper.alerts_store import (
    add_rule,
    new_rule_id,
    read_rules,
    remove_rule,
    write_rules,
)


def make_rule(code: str = "BBCA", rule_type: str = "price_above", threshold: float = 7000.0):
    return {"code": code, "type": rule_type, "threshold": threshold, "note": None}


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_rules(tmp_path / "nope.json") == []


def test_read_corrupt_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    p.write_text("{ not json", encoding="utf-8")
    assert read_rules(p) == []


def test_read_ignores_junk_entries(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    p.write_text(
        '[{"id": "a1", "code": "BBCA"}, {"code": "BBRI"}, "nonsense", {"id": "a2"}]',
        encoding="utf-8",
    )
    assert [r["id"] for r in read_rules(p)] == ["a1"]


def test_add_assigns_id_and_timestamp(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    stored = add_rule(make_rule(), p)
    assert stored["id"]
    assert stored["created_at"]
    assert read_rules(p) == [stored]


def test_add_keeps_insertion_order(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    add_rule(make_rule("BBCA"), p)
    add_rule(make_rule("BBRI"), p)
    add_rule(make_rule("TLKM"), p)
    assert [r["code"] for r in read_rules(p)] == ["BBCA", "BBRI", "TLKM"]


def test_remove_deletes_only_the_target(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    first = add_rule(make_rule("BBCA"), p)
    add_rule(make_rule("BBRI"), p)

    assert remove_rule(first["id"], p) is True
    assert [r["code"] for r in read_rules(p)] == ["BBRI"]
    assert remove_rule(first["id"], p) is False


def test_write_creates_parent_directory_and_round_trips(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "data" / "alerts.json"
    rules = [{"id": "x1", "code": "BBCA", "type": "rsi_below", "threshold": 30.0}]
    write_rules(rules, p)
    assert p.exists()
    assert read_rules(p) == rules


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    p = tmp_path / "alerts.json"
    add_rule(make_rule(), p)
    assert not (tmp_path / "alerts.json.tmp").exists()


def test_new_rule_ids_are_unique_and_short() -> None:
    ids = {new_rule_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 8 for i in ids)
