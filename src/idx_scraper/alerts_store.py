"""Read/write the user's alert-rule list.

Rules live in a small JSON file (default ``data/alerts.json``) instead of the
database: the API is deliberately not a DB writer, so mutable user config stays
in files — the same pattern as the .env-backed watchlist and the scraper's
source state.

``IDX_ALERTS_PATH`` overrides the location (used by tests).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WIB = timezone(timedelta(hours=7))


def _default_path() -> Path:
    override = os.getenv("IDX_ALERTS_PATH")
    if override:
        return Path(override)
    # Project root = two levels above this file (src/idx_scraper/ -> ..).
    return Path(__file__).resolve().parents[2] / "data" / "alerts.json"


def read_rules(path: Path | None = None) -> list[dict[str, Any]]:
    """Every stored rule, in insertion order.

    A missing, unreadable or corrupt file reads as an empty list: a broken alert
    config must never take the API or the scheduler down.
    """
    p = path or _default_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id") and r.get("code")]


def write_rules(rules: list[dict[str, Any]], path: Path | None = None) -> list[dict[str, Any]]:
    """Persist the rule list atomically (temp file + replace)."""
    p = path or _default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(p)
    return rules


def new_rule_id() -> str:
    return uuid.uuid4().hex[:8]


def add_rule(rule: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Append a rule, assigning an id and creation timestamp."""
    rules = read_rules(path)
    stored = {
        **rule,
        "id": rule.get("id") or new_rule_id(),
        "created_at": rule.get("created_at")
        or datetime.now(WIB).isoformat(timespec="seconds"),
    }
    rules.append(stored)
    write_rules(rules, path)
    return stored


def remove_rule(rule_id: str, path: Path | None = None) -> bool:
    """Delete one rule. Returns False when the id was not found."""
    rules = read_rules(path)
    kept = [r for r in rules if r.get("id") != rule_id]
    if len(kept) == len(rules):
        return False
    write_rules(kept, path)
    return True
