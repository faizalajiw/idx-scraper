"""Price / RSI / volume alert rules for watchlist emiten.

This module owns the rule vocabulary and the pure evaluation logic. It never
touches the database, so the API (which evaluates against the Postgres research
layer) and the CLI worker (which pushes to Telegram) share one definition of
what "triggered" means.

Anti-spam is a re-arm latch: ``RuleState`` remembers whether each rule was
triggered last check, so a rule fires once when it crosses and then stays quiet
until it goes back below the threshold.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ vocabulary


@dataclass(frozen=True)
class RuleType:
    """One kind of watch condition. ``metric`` keys into RuleSnapshot."""

    id: str
    label: str
    description: str
    metric: str
    metric_label: str
    direction: str  # "above" | "below"
    unit: str  # "IDR" | "RSI" | "x"
    default: float
    min: float | None = None
    max: float | None = None
    step: float = 1


RULE_TYPES: dict[str, RuleType] = {
    "price_above": RuleType(
        id="price_above",
        label="Harga di atas level",
        description="Terpicu saat harga terakhir melewati level yang ditentukan.",
        metric="close",
        metric_label="harga",
        direction="above",
        unit="IDR",
        default=1000.0,
        min=1.0,
        step=10,
    ),
    "price_below": RuleType(
        id="price_below",
        label="Harga di bawah level",
        description="Terpicu saat harga terakhir jatuh di bawah level, mis. batas stop.",
        metric="close",
        metric_label="harga",
        direction="below",
        unit="IDR",
        default=1000.0,
        min=1.0,
        step=10,
    ),
    "rsi_above": RuleType(
        id="rsi_above",
        label="RSI di atas",
        description="Terpicu saat RSI(14) melewati ambang, mis. 70 untuk overbought.",
        metric="rsi",
        metric_label="RSI",
        direction="above",
        unit="RSI",
        default=70.0,
        min=0.0,
        max=100.0,
        step=1,
    ),
    "rsi_below": RuleType(
        id="rsi_below",
        label="RSI di bawah",
        description="Terpicu saat RSI(14) turun di bawah ambang, mis. 30 untuk oversold.",
        metric="rsi",
        metric_label="RSI",
        direction="below",
        unit="RSI",
        default=30.0,
        min=0.0,
        max=100.0,
        step=1,
    ),
    "volume_spike": RuleType(
        id="volume_spike",
        label="Lonjakan volume",
        description="Terpicu saat volume ≥ sekian kali rata-rata 20 hari.",
        metric="vol_ratio",
        metric_label="volume ratio",
        direction="above",
        unit="x",
        default=2.0,
        min=1.0,
        max=50.0,
        step=0.5,
    ),
}


def list_rule_types() -> list[dict[str, Any]]:
    """Rule catalog for the UI, in a stable order."""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "description": spec.description,
            "unit": spec.unit,
            "default": spec.default,
            "min": spec.min,
            "max": spec.max,
            "step": spec.step,
        }
        for spec in RULE_TYPES.values()
    ]


# ------------------------------------------------------------------ snapshots


@dataclass(frozen=True)
class RuleSnapshot:
    """Latest metrics for one emiten, the input every rule is judged on."""

    code: str
    close: float | None = None
    percent: float | None = None
    rsi: float | None = None
    vol_ratio: float | None = None
    signal: str | None = None

    def metric(self, name: str) -> float | None:
        value = getattr(self, name, None)
        return value if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class RuleEvaluation:
    """The verdict for one rule against one snapshot."""

    rule: dict[str, Any]
    rule_type: RuleType
    current: float | None
    triggered: bool
    message: str


def format_value(value: float | None, unit: str) -> str:
    """Human-readable metric value for the given unit."""
    if value is None:
        return "-"
    if unit == "IDR":
        return f"{value:,.0f}"
    if unit == "RSI":
        return f"{value:.1f}"
    return f"{value:.2f}x"


def evaluate_rule(rule: dict[str, Any], snapshot: RuleSnapshot) -> RuleEvaluation:
    """Decide whether one rule is currently satisfied.

    A metric that is missing (e.g. RSI needs 15 bars) never triggers, so a thin
    or brand-new listing can't produce a false alert.
    """
    spec = RULE_TYPES.get(str(rule.get("type", "")))
    if spec is None:
        raise ValueError(f"tipe aturan tidak dikenal: {rule.get('type')!r}")

    threshold = float(rule["threshold"])
    current = snapshot.metric(spec.metric)
    triggered = current is not None and (
        current > threshold if spec.direction == "above" else current < threshold
    )
    relation = "di atas" if spec.direction == "above" else "di bawah"
    message = (
        f"{snapshot.code} {spec.metric_label} "
        f"{format_value(current, spec.unit)} {relation} ambang "
        f"{format_value(threshold, spec.unit)}"
    )
    return RuleEvaluation(
        rule=rule,
        rule_type=spec,
        current=current,
        triggered=triggered,
        message=message,
    )


def evaluate_rules(
    rules: list[dict[str, Any]], snapshots: dict[str, RuleSnapshot]
) -> list[RuleEvaluation]:
    """Evaluate every rule, skipping (not failing on) unknown rule types."""
    out: list[RuleEvaluation] = []
    for rule in rules:
        snapshot = snapshots.get(str(rule.get("code", "")).upper())
        if snapshot is None:
            continue
        try:
            out.append(evaluate_rule(rule, snapshot))
        except (ValueError, KeyError, TypeError):
            continue
    return out


# ------------------------------------------------------------------ re-arm latch


class RuleState:
    """Per-rule armed latch, persisted to JSON.

    A rule counts as newly triggered only on the check that crosses its
    threshold; while it stays triggered it returns nothing, and it re-arms as
    soon as the condition goes false again.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(
            path or os.getenv("IDX_ALERT_STATE") or self._default_path()
        )
        self._data: dict[str, bool] = {}
        self._load()

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[2] / "data" / "alert_state.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {str(k): bool(v) for k, v in raw.items()}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_armed(self, rule_id: str) -> bool:
        """True when the rule was not triggered on the previous check."""
        return not self._data.get(rule_id, False)

    def newly_triggered(self, evaluations: list[RuleEvaluation]) -> list[RuleEvaluation]:
        """Filter to rules that just crossed, then persist the new latch state."""
        fresh: list[RuleEvaluation] = []
        changed = False
        for evaluation in evaluations:
            rule_id = str(evaluation.rule.get("id", ""))
            if not rule_id:
                continue
            was = self._data.get(rule_id, False)
            if evaluation.triggered and not was:
                fresh.append(evaluation)
            if evaluation.triggered != was:
                self._data[rule_id] = evaluation.triggered
                changed = True
        if changed:
            self.save()
        return fresh
