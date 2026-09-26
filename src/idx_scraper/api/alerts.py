"""Alert-rule service: rule CRUD plus live evaluation against the research layer.

Rules themselves live in a JSON file (see ``alerts_store``); only *evaluation*
touches Postgres, and it reads exactly the same tables and indicators the
dashboard uses. That way the status the UI shows is what the CLI worker will push
to Telegram — there is no second, subtly different implementation.

Sending alerts is the worker's job (``cli.py``); this module only answers "what
is true right now", which keeps the API free of surprise side effects.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import alert_rules
from ..alerts_store import add_rule, read_rules, remove_rule
from ..analysis import calculate_indicators, generate_signal
from ..notify import TelegramNotifier
from .database import get_cursor
from .services import _build_frame, _f

WIB = timezone(timedelta(hours=7))

_CODE_RE = re.compile(r"^[A-Z]{4}$")

# Rolling baseline for the volume-spike rule; matches the screener's definition
# so a "volume spike" means the same thing everywhere in the product.
_VOL_WINDOW = 20


def telegram_enabled() -> bool:
    return TelegramNotifier().enabled


def send_test_message() -> bool:
    """Send a one-off Telegram message so the user can verify the wiring."""
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return False
    return notifier.send_message(
        "<b>🔔 Market Labs</b>\nKoneksi alert Telegram berhasil. "
        "Aturan yang kamu pasang akan dikirim ke chat ini."
    )


# ------------------------------------------------------------------ snapshots


def _snapshots_for(codes: list[str]) -> dict[str, alert_rules.RuleSnapshot]:
    """Latest metrics per emiten, from the same data the dashboard reads."""
    snapshots: dict[str, alert_rules.RuleSnapshot] = {}
    with get_cursor() as cur:
        for code in codes:
            df, _live = _build_frame(cur, code)
            if df.empty:
                snapshots[code] = alert_rules.RuleSnapshot(code=code)
                continue

            df = calculate_indicators(df)
            last = df.iloc[-1]
            close = _f(last.get("close"))

            prev_close = _f(df.iloc[-2].get("close")) if len(df) > 1 else None
            percent = (
                (close - prev_close) / prev_close * 100
                if close is not None and prev_close
                else None
            )

            volumes = df["volume"].fillna(0) if "volume" in df.columns else None
            vol_ratio = None
            if volumes is not None and len(volumes):
                baseline = volumes.tail(_VOL_WINDOW).mean()
                if baseline and baseline > 0:
                    vol_ratio = float(volumes.iloc[-1] / baseline)

            snapshots[code] = alert_rules.RuleSnapshot(
                code=code,
                close=close,
                percent=round(percent, 4) if percent is not None else None,
                rsi=_f(last.get("RSI")),
                vol_ratio=round(vol_ratio, 4) if vol_ratio is not None else None,
                signal=generate_signal(df),
            )
    return snapshots


def evaluate_all() -> list[alert_rules.RuleEvaluation]:
    """Evaluate every stored rule. Shared by the API status and the CLI worker."""
    rules = read_rules()
    if not rules:
        return []
    codes = sorted({str(r.get("code", "")).upper() for r in rules if r.get("code")})
    snapshots = _snapshots_for(codes)
    return alert_rules.evaluate_rules(rules, snapshots)


# ------------------------------------------------------------------ CRUD


def _validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise and check a create payload, raising ValueError on anything off."""
    code = str(payload.get("code", "")).strip().upper()
    if not _CODE_RE.match(code):
        raise ValueError("kode emiten harus 4 huruf, mis. BBCA")

    rule_type = str(payload.get("type", "")).strip()
    spec = alert_rules.RULE_TYPES.get(rule_type)
    if spec is None:
        valid = ", ".join(alert_rules.RULE_TYPES)
        raise ValueError(f"tipe aturan tidak dikenal: {rule_type!r} ({valid})")

    raw_threshold = payload.get("threshold")
    try:
        threshold = float(raw_threshold)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("ambang batas harus berupa angka") from None
    if math.isnan(threshold) or math.isinf(threshold):
        raise ValueError("ambang batas bukan angka valid")
    if spec.min is not None and threshold < spec.min:
        raise ValueError(f"ambang batas minimal {spec.min:g}")
    if spec.max is not None and threshold > spec.max:
        raise ValueError(f"ambang batas maksimal {spec.max:g}")

    note = str(payload.get("note") or "").strip()
    return {
        "code": code,
        "type": rule_type,
        "threshold": threshold,
        "note": note or None,
    }


def create_rule(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and append a rule, rejecting an exact duplicate."""
    rule = _validate(payload)
    for existing in read_rules():
        same = (
            existing.get("code") == rule["code"]
            and existing.get("type") == rule["type"]
            and float(existing.get("threshold", 0)) == rule["threshold"]
        )
        if same:
            raise ValueError("aturan yang sama sudah ada")
    return add_rule(rule)


def delete_rule(rule_id: str) -> bool:
    return remove_rule(rule_id)


# ------------------------------------------------------------------ status


def _rule_status(
    rule: dict[str, Any], snapshot: alert_rules.RuleSnapshot
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(rule.get("id", "")),
        "code": str(rule.get("code", "")),
        "type": str(rule.get("type", "")),
        "threshold": float(rule.get("threshold", 0.0)),
        "note": rule.get("note"),
        "created_at": rule.get("created_at"),
        "close": snapshot.close,
        "percent": snapshot.percent,
        "rsi": snapshot.rsi,
        "vol_ratio": snapshot.vol_ratio,
        "signal": snapshot.signal,
    }

    spec = alert_rules.RULE_TYPES.get(base["type"])
    if spec is None:
        # Keep a hand-edited/corrupt rule visible so it can still be deleted.
        return {
            **base,
            "type_label": "(tidak dikenal)",
            "unit": "",
            "current": None,
            "triggered": False,
            "message": "Tipe aturan tidak dikenal — hapus lalu buat ulang.",
        }

    evaluation = alert_rules.evaluate_rule(rule, snapshot)
    return {
        **base,
        "type_label": spec.label,
        "unit": spec.unit,
        "current": evaluation.current,
        "triggered": evaluation.triggered,
        "message": evaluation.message,
    }


def status() -> dict[str, Any]:
    """Stored rules with their live values, plus the Telegram wiring state."""
    rules = read_rules()
    codes = sorted({str(r.get("code", "")).upper() for r in rules if r.get("code")})
    snapshots = _snapshots_for(codes)

    items = [
        _rule_status(
            rule,
            snapshots.get(str(rule.get("code", "")).upper())
            or alert_rules.RuleSnapshot(code=str(rule.get("code", ""))),
        )
        for rule in rules
    ]
    return {
        "checked_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "telegram_enabled": telegram_enabled(),
        "rule_types": alert_rules.list_rule_types(),
        "rules": items,
        "triggered_count": sum(1 for item in items if item["triggered"]),
    }
