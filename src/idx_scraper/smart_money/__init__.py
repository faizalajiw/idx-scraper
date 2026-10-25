"""Facade paket smart_money — re-export seluruh nama lama (FAI-6).

Konsumen (cli, notify, api.analytics, research.recommend) memakai nama
privat seperti ``_clean_rows`` dan konstanta ambang, jadi semuanya
di-export ulang di sini. Setiap impor adalah re-export sengaja (lihat
per-file-ignores F401 di pyproject.toml).
"""

from __future__ import annotations

from .inti import (
    FLAT_RANGE_MAX_PCT,
    INITIATION_NET_PCT,
    LARGE_NETVAL_PCT,
    MEANINGFUL_NETVAL_PCT,
    MIN_WINDOW_DAYS,
    PRICE_WINDOW,
    RADAR_MIN_WINDOW_VALUE,
    RANGE_LOOKBACK,
    RANGE_WINDOW,
    RISING_MIN_PCT,
    VERDICT_ACCUMULATION,
    VERDICT_DISTRIBUTION,
    VERDICT_NEUTRAL,
    VOL_SPIKE_RATIO,
    VOLUME_BASELINE_WINDOW,
    _clean_rows,
    _f,
    _streak,
    _window_stats,
    consolidation_range,
    verdict,
)
from .konteks import (
    _MONTHS_ID,
    PATTERNS_BOARD_TOP_N,
    SIZE_BANDS,
    VERDICT_EMOJI,
    _fmt_date_short,
    _fmt_rp,
    build_alert_message,
    build_narrative,
    build_radar,
    market_breadth,
    market_context_sentence,
    patterns_board,
    size_label,
)
from .pola import (
    _CONFIDENCE_ORDER,
    CONFIDENCE_BANDS,
    EPISODE_GAP,
    HORIZON_DAYS,
    MIN_HISTORY_N,
    PATTERNS,
    _confidence_word,
    _mean,
    _median,
    _pattern_conditions,
    _tstat,
    collect_pattern_episodes,
    detect_patterns,
    pattern_evidence_sentence,
    pattern_flags,
    pattern_history,
    pattern_track_record,
    patterns_history,
    rank_patterns,
)
