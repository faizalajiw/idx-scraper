"""Skema aturan pantauan & alert Telegram."""

from __future__ import annotations

from pydantic import BaseModel


class AlertRuleType(BaseModel):
    """One kind of watch condition the user can pick."""

    id: str
    label: str
    description: str
    unit: str
    default: float
    min: float | None = None
    max: float | None = None
    step: float = 1


class AlertRuleCreate(BaseModel):
    code: str
    type: str
    threshold: float
    note: str | None = None


class AlertRuleStatus(BaseModel):
    """A stored rule plus its live value and current verdict."""

    id: str
    code: str
    type: str
    type_label: str
    threshold: float
    unit: str
    note: str | None = None
    created_at: str | None = None
    current: float | None = None
    close: float | None = None
    percent: float | None = None
    rsi: float | None = None
    vol_ratio: float | None = None
    signal: str | None = None
    triggered: bool
    message: str


class AlertStatus(BaseModel):
    checked_at: str
    telegram_enabled: bool
    rule_types: list[AlertRuleType]
    rules: list[AlertRuleStatus]
    triggered_count: int


class AlertTestResult(BaseModel):
    sent: bool
    detail: str
