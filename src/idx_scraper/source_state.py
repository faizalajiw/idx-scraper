"""Live-data source selector with IDX cooldown + auto-switch.

While IDX sits behind a Cloudflare managed challenge (and the IP is likely
rate-limited), we serve live prices from Yahoo Finance. A throttled probe
periodically checks whether IDX is reachable again; once it is, the selector
flips back to IDX automatically.

State is persisted to a small JSON file so it survives scheduler restarts.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

SOURCE_YAHOO = "YAHOO"
SOURCE_IDX = "IDX"

_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "source_state.json",
)


@dataclass
class SourceState:
    source: str = SOURCE_YAHOO
    last_probe_ts: float = 0.0
    last_switch_ts: float = 0.0
    probe_count: int = 0
    idx_ok_count: int = 0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "last_probe_ts": self.last_probe_ts,
            "last_switch_ts": self.last_switch_ts,
            "probe_count": self.probe_count,
            "idx_ok_count": self.idx_ok_count,
        }


def _load() -> SourceState:
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return SourceState(
            source=d.get("source", SOURCE_YAHOO),
            last_probe_ts=float(d.get("last_probe_ts", 0.0)),
            last_switch_ts=float(d.get("last_switch_ts", 0.0)),
            probe_count=int(d.get("probe_count", 0)),
            idx_ok_count=int(d.get("idx_ok_count", 0)),
        )
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
        return SourceState()


def _save(state: SourceState) -> None:
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp, _STATE_PATH)


# In-process cache so hot jobs don't re-read the file every tick.
_cache: SourceState | None = None


def get_state() -> SourceState:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def current_source() -> str:
    return get_state().source


def _set_source(source: str) -> None:
    state = get_state()
    if state.source != source:
        state.source = source
        state.last_switch_ts = time.time()
    _save(state)


def maybe_probe_and_switch(reachable_fn, probe_interval_sec: int, need_ok: int = 2) -> str:
    """Throttled auto-switch check. Returns the (possibly updated) source.

    - Only probes when in Yahoo mode (no point probing once we're on IDX).
    - Probes at most once per ``probe_interval_sec``.
    - Requires ``need_ok`` consecutive successful probes before flipping to IDX,
      to avoid flapping on a single lucky response.
    - ``reachable_fn`` must be a cheap, single-request callable returning bool.
    """
    state = get_state()
    if state.source == SOURCE_IDX:
        return SOURCE_IDX

    now = time.time()
    if now - state.last_probe_ts < probe_interval_sec:
        return state.source

    state.last_probe_ts = now
    state.probe_count += 1
    ok = False
    try:
        ok = bool(reachable_fn())
    except Exception:
        ok = False

    if ok:
        state.idx_ok_count += 1
    else:
        state.idx_ok_count = 0

    if state.idx_ok_count >= need_ok:
        state.source = SOURCE_IDX
        state.last_switch_ts = now
        state.idx_ok_count = 0
        _save(state)
        print(f"[{datetime.now(WIB).isoformat()}] SOURCE AUTO-SWITCH -> IDX (cooldown selesai)")
        return SOURCE_IDX

    _save(state)
    return SOURCE_YAHOO


def force_source(source: str) -> None:
    """Manual override (e.g. force back to Yahoo if IDX starts blocking again)."""
    source = source.upper()
    if source not in (SOURCE_YAHOO, SOURCE_IDX):
        raise ValueError(f"unknown source: {source}")
    _set_source(source)
    # reset in-process cache so next read reflects the change
    global _cache
    _cache = None
