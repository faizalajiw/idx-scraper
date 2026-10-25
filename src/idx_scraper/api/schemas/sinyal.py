"""Skema sinyal teknikal, watchlist, screener, dan hold-check."""

from __future__ import annotations

from pydantic import BaseModel


class WatchlistRow(BaseModel):
    code: str
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    volume: float | None = None
    foreign_net: float | None = None
    hist_days: int = 0


class WatchlistUpdate(BaseModel):
    """Full replacement list for the .env-backed watchlist."""

    codes: list[str]


class Signal(BaseModel):
    code: str
    # ``signal`` sudah melewati corp-action filter; ``raw_signal`` = SELL
    # mentah sebelum filter. ``div_adjusted`` True berarti SELL palsu
    # ex-dividend yang dinetralkan jadi HOLD (div_cash = per saham).
    signal: str
    raw_signal: str | None = None
    div_cash: float | None = None
    div_adjusted: bool = False
    close: float | None = None
    pct: float | None = None
    rsi: float | None = None
    sma20: float | None = None
    macd: float | None = None
    trend_up: bool
    live: bool
    # Tanggal sesi bursa bar terakhir (YYYY-MM-DD) — untuk badge "update terakhir".
    as_of: str | None = None
    # Jam backend menghitung sinyal ini (WIB, ISO) — untuk badge "diperbarui".
    generated_at: str | None = None


class PriceBar(BaseModel):
    date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None


class SignalTrackStat(BaseModel):
    """Statistik track record satu irisan (signal x regime x horizon).

    ``mean_abnormal`` = forward return dikurangi return pasar equal-weight pada
    window yang sama; ``avg_mfe``/``avg_mae`` = excursion rata-rata (kenaikan
    terbaik / penurunan terburuk) selama horizon.
    """

    signal: str
    regime: str | None = None
    horizon: int
    n: int
    hit_rate: float | None = None
    mean_fwd: float | None = None
    median_fwd: float | None = None
    mean_abnormal: float | None = None
    t_stat: float | None = None
    avg_mfe: float | None = None
    avg_mae: float | None = None


class SignalTrackRecent(BaseModel):
    """Satu sinyal terbaru dengan return yang sudah terealisasi."""

    code: str
    date: str
    signal: str
    close: float | None = None
    fwd_5: float | None = None
    fwd_10: float | None = None
    fwd_21: float | None = None


class SignalTrack(BaseModel):
    """Track record sinyal BUY/SELL (point-in-time, entry T+1)."""

    generated_at: str | None = None
    signals: int
    buy: int = 0
    sell: int = 0
    first_date: str | None = None
    last_date: str | None = None
    horizons: list[int]
    overall: list[SignalTrackStat]
    by_regime: list[SignalTrackStat]
    recent: list[SignalTrackRecent]


class IndicatorBar(PriceBar):
    ma_short: float | None = None
    ma_long: float | None = None
    rsi: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    macd: float | None = None
    macd_signal: float | None = None


class ScreenerRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    percent: float | None = None
    rsi: float | None = None
    signal: str
    trend_up: bool
    momentum_20d: float | None = None
    vol_ratio: float | None = None
    atr_pct: float | None = None
    dist_52w: float | None = None
    days_since_signal: int | None = None
    # Order-book flow dari snapshot intraday (research.orderbook); null bila
    # emiten tidak tercakup capture / snapshot terlalu tipis.
    ob_imbalance: float | None = None
    ob_absorption: float | None = None
    # Skor aktivitas broker (0-100, proksi aliran ber-bobot IC); null bila skor
    # belum tervalidasi atau emiten di luar coverage.
    broker_score: float | None = None
    foreign_net: float | None = None
    value: float | None = None
    hist_days: int


class TechnicalChart(BaseModel):
    code: str
    signal: str
    bars: list[IndicatorBar]


class HoldCheckItem(BaseModel):
    """One ticker's combined technical + valuation "still worth holding" verdict.

    score is 0-100 (higher = healthier hold); verdict is one of
    STRONG HOLD / HOLD / TRIM / EXIT. reasons carries the human-readable
    bullets shown in the UI.

    Lapisan faktor IC (volatilitas/likuiditas/jarak 52w, lihat
    research.composite): ``score`` sudah termasuk ``factor_adj``;
    ``base_score`` = skor teknikal+valuasi sebelum penyesuaian;
    ``factor_pct`` = percentile cross-sectional pasar per faktor (None bila
    emiten tidak punya histori cukup untuk diranking).
    """

    code: str
    name: str | None = None
    signal: str
    trend_up: bool
    rsi: float | None = None
    macd_bullish: bool = False
    bb_position: str | None = None  # "above" | "inside" | "below"
    z_score: float | None = None
    below_target_pct: float | None = None
    foreign_net: float | None = None
    score: int
    base_score: int | None = None
    factor_adj: float | None = None
    factor_pct: dict[str, float] | None = None
    # Lapisan aktivitas broker (research.broker_activity): score sudah termasuk
    # broker_adj; broker_score null = lapisan tidak aktif (belum tervalidasi IC
    # atau emiten di luar coverage).
    broker_score: float | None = None
    broker_adj: float | None = None
    verdict: str
    reasons: list[str]


class HoldCheckResponse(BaseModel):
    date: str | None = None
    generated_at: str | None = None
    items: list[HoldCheckItem]


# --------------------------------------------------------------- backtest
