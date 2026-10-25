"""Scheduler + CLI for IDX scraper.

Usage:
    idx snapshot          # one-off index + watchlist fetch → storage
    idx eod 20260919      # fetch EOD summary for a date → storage
    idx serve             # start polling loop (market hours only)
"""

from __future__ import annotations

from .alerts import (
    _cetak_aman,
    _is_market_hours,
    _job_alerts,
    _job_health_eod,
    _job_indices,
    _job_news_refresh,
    _job_watchlist,
    _lapor_bisu,
    _periksa,
    cmd_alerts,
    cmd_health,
    run_recommendation_alerts,
    run_rule_alerts,
    run_smart_money_alerts,
)
from .backfill import (
    _BACKFILL_HARI,
    _BACKFILL_HARI_DEFAULT,
    _backfill_hari_lampau,
    _eod_catchup,
    _eod_catchup_hari_ini,
    _hari_bursa,
    _hari_bursa_mundur,
    _job_eod_fallback,
)
from .eod import (
    _EOD_RETRY_DELAYS,
    _PIPELINE_BUSY,
    _boleh_tulis_close_index,
    _clear_api_cache,
    _eod_fresh_for,
    _ingest_eod_research,
    _job_eod_full,
    _job_index_eod_close,
    _job_pipeline_refresh,
    _pipeline_refresh_body,
    _retry_percobaan,
    _retry_step,
    _simpan_eod_hari,
    _upsert_segment_day,
    cmd_eod,
    cmd_refresh,
    cmd_seed,
    cmd_signals,
    cmd_snapshot,
    cmd_trade_summary,
)
from .fetch import (
    WIB,
    _parse_watchlist,
    fetch_and_store_eod,
    fetch_and_store_indices,
    fetch_and_store_indices_yahoo,
    fetch_and_store_watchlist,
    fetch_and_store_watchlist_yahoo,
    liquid_codes,
    seed_historical_data,
    show_signals,
)
from .jobs import (
    _job_broker_eod,
    _job_monthly_ic,
    _job_ownership,
    _job_recommendations,
    _job_regime_daily,
    _job_signal_log,
    _next_weekday_at,
    cmd_ic,
    cmd_ownership,
    cmd_recommendations,
    cmd_signal_log,
    snapshot_ownership,
)
from .journal import (
    _job_journal_claims,
    _job_journal_outcomes,
    _job_journal_reminder,
    _journal_dsn,
    _journal_gate_parts,
    _journal_session,
    _journal_write,
    _last_session_date,
    _now_wib,
    cmd_journal,
    cmd_journal_halt,
    cmd_journal_resume,
)
from .main import (
    main,
)
from .serve import (
    _SERVE_LOCK_FD,
    _serve_lock_path,
    _take_single_instance_lock,
    _tutup_pool_api,
    cmd_serve,
)
