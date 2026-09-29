"""Pydantic response schemas for the API (JSON-serializable, Recharts-ready)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class IndexOverview(BaseModel):
    code: str
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    current: float | None = None
    captured_at: str | None = None


class MarketTotals(BaseModel):
    total_volume: float | None = None
    total_value: float | None = None
    stock_count: int = 0


class Mover(BaseModel):
    code: str
    close: float | None = None
    percent: float | None = None


class MarketOverview(BaseModel):
    index: IndexOverview | None = None
    totals: MarketTotals
    movers_source: str = "eod"  # "intraday" | "eod"
    movers_captured_at: str | None = None
    top_gainers: list[Mover]
    top_losers: list[Mover]


class SessionMovers(BaseModel):
    date: str | None = None
    captured_at: str | None = None
    top_gainers: list[Mover]
    top_losers: list[Mover]

class LeaderRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    percent: float | None = None
    volume: float | None = None
    value: float | None = None
    frequency: float | None = None

class MarketLeaders(BaseModel):
    metric: str
    source: str  # "intraday" | "eod"
    date: str | None = None
    captured_at: str | None = None
    rows: list[LeaderRow]

class BrokerLeaderRow(BaseModel):
    broker_code: str
    broker_name: str | None = None
    volume: float | None = None
    value: float | None = None
    frequency: float | None = None
    # Kategori bandarmologi dari map kurasi kode (asing/lokal/bumn).
    category: str | None = None

class TopBrokers(BaseModel):
    date: str | None = None
    captured_at: str | None = None
    rows: list[BrokerLeaderRow]


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


class SentimentItem(BaseModel):
    """Satu emiten di daftar sorotan sentimen (akumulasi / distribusi).

    ``score`` = skor aliran+buku ``-1..+1``; ``reasons`` = alasan terbaca yang
    bisa dipakai UI. ``ob_*`` null bila capture order-book tidak tersedia.
    """

    code: str
    name: str | None = None
    close: float | None = None
    percent: float | None = None
    value: float | None = None
    foreign_net: float | None = None
    foreign_net_pct: float | None = None
    foreign_rank: float | None = None
    ob_imbalance: float | None = None
    ob_absorption: float | None = None
    score: float
    label: str
    reasons: list[str]


class SentimentMarket(BaseModel):
    """Gauge sentimen pasar 0..100 dari breadth + IHSG + intensitas asing."""

    score: float
    label: str
    components: dict[str, float]
    breadth_up: float | None = None
    foreign_breadth: float | None = None
    up: int = 0
    down: int = 0
    flat: int = 0
    index_percent: float | None = None
    total_value: float | None = None
    total_foreign_net: float | None = None
    foreign_to_value: float | None = None


class SentimentResponse(BaseModel):
    """Sentimen posisi/aliran dari data tersimpan (bukan berita)."""

    as_of: str | None = None
    generated_at: str | None = None
    market: SentimentMarket
    accumulation: list[SentimentItem]
    distribution: list[SentimentItem]
    stats: dict[str, int]
    weights: dict[str, float]
    orderbook_available: bool = False
    disclaimer: str | None = None


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


class BrokerRow(BaseModel):
    broker: str
    code: str | None = None
    buy_value: float = 0.0
    sell_value: float = 0.0
    net: float = 0.0
    buy_rank: int | None = None
    sell_rank: int | None = None
    estimated: bool = True


class StockBrokerSummary(BaseModel):
    code: str
    name: str | None = None
    date: str | None = None
    top_buyers: list[BrokerRow]
    top_sellers: list[BrokerRow]


class BrokerConcentrationRow(BaseModel):
    broker_code: str
    broker_name: str | None = None
    volume: float | None = None
    value: float | None = None
    frequency: float | None = None
    share: float | None = None


class BrokerConcentration(BaseModel):
    """Konsentrasi transaksi per broker firma (seluruh pasar, EOD)."""

    date: str | None = None
    captured_at: str | None = None
    n_brokers: int = 0
    total_value: float | None = None
    cr1: float | None = None
    cr3: float | None = None
    cr5: float | None = None
    hhi: float | None = None
    top: list[BrokerConcentrationRow]


class BrokerFlowCategory(BaseModel):
    """Satu kategori broker: nilai transaksi + porsi pasar sesi terakhir."""

    value: float | None = None
    share: float | None = None
    n_brokers: int = 0


class BrokerFlowTopRow(BaseModel):
    """Firma terbesar dalam satu kategori (porsi dihitung terhadap pasar)."""

    broker_code: str
    broker_name: str | None = None
    value: float
    share: float | None = None


class BrokerFlowDay(BaseModel):
    """Satu titik tren komposisi (bentuk pipih untuk chart)."""

    date: str
    total_value: float | None = None
    asing_value: float = 0.0
    lokal_value: float = 0.0
    bumn_value: float = 0.0
    asing_share: float | None = None
    lokal_share: float | None = None
    bumn_share: float | None = None


class BrokerClassificationRow(BaseModel):
    """Kategori satu broker pada sesi terakhir — untuk audit klasifikasi."""

    broker_code: str
    broker_name: str | None = None
    category: str


class BrokerFlow(BaseModel):
    """Komposisi nilai transaksi per kategori broker (asing/lokal/BUMN).

    Komposisi (turnover share), bukan net buy/sell — IDX tidak menyediakan
    split beli/jual per firma. ``classification`` memaparkan kategori tiap
    broker sesi terakhir supaya map kurasi bisa diaudit dari UI.
    """

    date: str | None = None
    captured_at: str | None = None
    n_brokers: int = 0
    total_value: float | None = None
    categories: dict[str, BrokerFlowCategory]
    top: dict[str, list[BrokerFlowTopRow]]
    history: list[BrokerFlowDay]
    classification: list[BrokerClassificationRow]


class BrokerActivityFactor(BaseModel):
    """Satu faktor aliran + hasil ujinya (IC) dan bobot yang dipakai."""

    factor: str
    label: str
    description: str | None = None
    mean_ic: float | None = None
    icir: float | None = None
    t_stat: float | None = None
    hit_rate: float | None = None
    n_days: int | None = None
    eligible: bool = False
    direction: float = 0.0
    weight: float = 0.0


class BrokerActivityDriver(BaseModel):
    """Kontributor skor satu emiten (menjelaskan "kenapa skornya begitu")."""

    factor: str
    label: str
    contribution: float
    percentile: float | None = None


class BrokerActivityRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    change: float | None = None
    percent: float | None = None
    score: float
    coverage: float
    drivers: list[BrokerActivityDriver]


class BrokerActivity(BaseModel):
    """Skor aktivitas broker per emiten + struktur broker pasar.

    ``validated`` False -> ``rows`` kosong dan ``reason`` menjelaskan kenapa
    (biasanya belum ada run IC). Sengaja tidak ada skor pengganti.
    """

    as_of: str | None = None
    validated: bool
    reason: str | None = None
    horizon: int | None = None
    ic_run_date: str | None = None
    eligible_count: int = 0
    factors: list[BrokerActivityFactor]
    rows: list[BrokerActivityRow]
    market: BrokerConcentration


class BrokerActivitySnapshot(BaseModel):
    """Nilai skor satu emiten pada sesi terakhir + peringkatnya di pasar."""

    score: float
    coverage: float
    rank: int
    universe: int
    percentile: float
    drivers: list[BrokerActivityDriver]


class BrokerActivityHistoryPoint(BaseModel):
    """Satu titik riwayat: skor emiten pada satu tanggal + driver saat itu."""

    date: str
    score: float
    coverage: float
    drivers: list[BrokerActivityDriver]
    # Median skor seluruh emiten yang diskor tanggal yang sama (baseline pasar
    # aktual — bukan angka teoretis 50).
    market_median: float | None = None


class BrokerActivityPeer(BaseModel):
    """Satu emiten sebanding (sektor sama) dengan skornya pada sesi terakhir."""

    code: str
    name: str | None = None
    score: float
    coverage: float
    is_self: bool = False


class BrokerActivitySector(BaseModel):
    """Pembanding skor di dalam satu sektor.

    ``comparable`` False = emiten jatuh ke bucket fallback "Lainnya", yang
    bukan sektor sebenarnya — pembandingnya tidak berarti.
    """

    name: str
    comparable: bool
    peer_count: int
    my_rank: int | None = None
    median_score: float | None = None
    peers: list[BrokerActivityPeer]


class StockBrokerActivity(BaseModel):
    """Skor aktivitas broker satu emiten + riwayat skor/driver-nya.

    ``current`` None = emiten tidak masuk cross-section hari terakhir (bukan
    skor nol); ``history`` tetap bisa berisi tanggal-tanggal sebelumnya.
    """

    code: str
    as_of: str | None = None
    validated: bool
    reason: str | None = None
    horizon: int | None = None
    ic_run_date: str | None = None
    eligible_count: int = 0
    sector: BrokerActivitySector | None = None
    current: BrokerActivitySnapshot | None = None
    history: list[BrokerActivityHistoryPoint]


class ForeignFlowDay(BaseModel):
    date: str
    buy: float | None = None
    sell: float | None = None
    net: float | None = None


class ForeignMover(BaseModel):
    code: str
    name: str | None = None
    net: float | None = None
    percent: float | None = None


class ForeignFlow(BaseModel):
    date: str | None = None
    total_buy: float | None = None
    total_sell: float | None = None
    total_net: float | None = None
    days: list[ForeignFlowDay]
    top_net_in: list[ForeignMover]
    top_net_out: list[ForeignMover]


class StockForeignFlowDay(BaseModel):
    """Satu hari aliran asing satu emiten (rupiah, net = buy - sell)."""

    date: str
    net: float | None = None
    buy: float | None = None
    sell: float | None = None
    # Hari berturut-turut net searah (positif = akumulasi). None = hari tanpa data.
    streak: int | None = None
    # True hanya di hari tanda net berubah vs hari non-nol sebelumnya.
    flip: bool | None = None


class StockForeignFlowFlip(BaseModel):
    """Ringkasan pergantian arah aliran asing emiten."""

    last_flip: dict[str, Any] | None = None  # {date, to: "net_buy"|"net_sell"}
    days_since_flip: int | None = None
    current_streak: int | None = None
    current_side: str = "flat"  # "net_buy" | "net_sell" | "flat"


class StockForeignFlowPeer(BaseModel):
    """Pembanding net flow asing di sektor yang sama (jumlah N hari terakhir)."""

    code: str
    net_sum: float
    net_mean: float
    n_days: int
    is_self: bool


class StockForeignFlow(BaseModel):
    """Aliran asing satu emiten + tren, flip, dan pembanding sektor.

    Nilai rupiah dihitung dari ``foreign_net`` (saham) * close hari itu.
    Komposisi kategori broker ada di ``/api/broker-flow``; endpoint ini fokus
    pada ARAH aliran asing agregat per emiten dari hari ke hari.
    """

    code: str
    sector: str
    # False = emiten di bucket fallback "Lainnya" (bukan sektor sebenarnya).
    comparable: bool
    date: str | None = None
    flow: list[StockForeignFlowDay]
    flip_summary: StockForeignFlowFlip
    peers: list[StockForeignFlowPeer]
    peer_rank: dict[str, Any]


class SectorTopStock(BaseModel):
    code: str
    percent: float | None = None


class SectorTopStock(BaseModel):
    code: str
    percent: float | None = None


class SectorRow(BaseModel):
    sector: str
    stock_count: int
    avg_percent: float | None = None
    total_value: float | None = None
    total_foreign_net: float | None = None
    gainers: int
    losers: int
    top_stock: SectorTopStock | None = None


class SectorAnalysis(BaseModel):
    date: str | None = None
    sectors: list[SectorRow]


class RRGPoint(BaseModel):
    """One weekly RRG position of a sector vs the benchmark index."""

    sector: str
    date: str
    rs_ratio: float
    rs_momentum: float


class SectorRRG(BaseModel):
    benchmark: str
    window: int
    date: str | None = None
    points: list[RRGPoint]


class SectorRotationRow(BaseModel):
    """Satu sektor: level skor broker agregat + arah rotasinya.

    ``phase``: AKUMULASI (tinggi & naik) / MEMUDAR (tinggi & turun) / MEMBAIK
    (rendah & naik) / TERPURUK (rendah & turun). ``history`` = median skor
    sektor per sesi (terbaru terakhir) untuk grafik mini.
    """

    sector: str
    n_names: int
    median_score: float
    breadth: float
    delta_5d: float | None = None
    delta_21d: float | None = None
    phase: str | None = None
    history: list[float]


class SectorRotation(BaseModel):
    """Rotasi sektor berbasis skor aktivitas broker (proksi aliran dana).

    ``unmapped_names`` = emiten berskor yang belum punya sektor sebenarnya
    (bucket "Lainnya") dan karena itu tidak diikutkan — dilaporkan supaya
    cakupan rotasinya bisa dinilai apa adanya.
    """

    as_of: str | None = None
    validated: bool
    reason: str | None = None
    horizon: int | None = None
    ic_run_date: str | None = None
    lookback: int
    min_names: int
    unmapped_names: int = 0
    sectors: list[SectorRotationRow]


class NarrationSection(BaseModel):
    title: str
    icon: str
    tone: str
    text: str


class MarketRegime(BaseModel):
    """Regime IHSG + volatilitas ter-annualisasi untuk banner konteks."""

    regime: str | None = None  # TRENDING_UP | TRENDING_DOWN | TRANSITION | RANGING | None
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    realized_vol_annual: float | None = None
    vol_state: str | None = None  # VOLATILE | NORMAL | QUIET
    source: str | None = None
    as_of: str | None = None
    dir_hint: str | None = None
    generated_at: str | None = None


class MarketNarration(BaseModel):
    date: str | None = None
    generated_at: str | None = None
    sections: list[NarrationSection]


class ValuationRow(BaseModel):
    code: str
    name: str | None = None
    close: float | None = None
    z_score: float | None = None
    momentum_pct: float | None = None
    rsi: float | None = None
    trend_up: bool = False
    target_price: float | None = None


class ValuationResponse(BaseModel):
    undervalued: list[ValuationRow]
    overvalued: list[ValuationRow]


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


# --------------------------------------------------------------- data quality

class QualityOverview(BaseModel):
    raw_rows: int
    raw_codes: int
    trading_days: int
    first_day: str | None = None
    last_day: str | None = None
    pit_rows: int
    quarantine_rows: int
    corp_actions: int
    last_ingest: str | None = None
    staleness_hours: float | None = None


class QuarantineRow(BaseModel):
    code: str | None = None
    trade_date: str | None = None
    reason: str
    payload: dict | None = None
    ingested_at: str | None = None


class QuarantineReason(BaseModel):
    reason: str
    count: int


class CoverageWindow(BaseModel):
    first: str
    last: str


class CoverageGaps(BaseModel):
    window: CoverageWindow | None = None
    covered_days: int
    missing_weekdays: list[str]


class ThinDay(BaseModel):
    trade_date: str
    codes: int


class CorpActionSummary(BaseModel):
    by_type: dict[str, int]
    by_source: dict[str, int]


# ---------------------------------------------------- dividends & corp actions


class DividendTotals(BaseModel):
    """Dividend activity counts plus the trailing-yield distribution.

    There is deliberately no "total cash distributed" figure: `cash_amount` is
    a per-share number, so summing it across emiten would be meaningless.
    """

    events: int
    codes: int
    splits: int
    first_ex_date: str | None = None
    last_ex_date: str | None = None
    ttm_events: int
    ttm_codes: int
    avg_ttm_yield: float | None = None
    median_ttm_yield: float | None = None
    max_ttm_yield: float | None = None


class DividendYear(BaseModel):
    """One calendar year of dividend activity (for the bar chart)."""

    year: int
    events: int
    codes: int


class DividendRecent(BaseModel):
    """A cash dividend just paid out (there is no forward calendar)."""

    code: str
    name: str | None = None
    ex_date: str | None = None
    cash_amount: float | None = None
    close: float | None = None


class DividendYielder(BaseModel):
    """Trailing dividend yield: last 12 months of cash / latest close."""

    code: str
    name: str | None = None
    close: float | None = None
    ttm_cash: float
    ttm_events: int
    yield_pct: float | None = None


class DividendOverview(BaseModel):
    as_of: str | None = None
    generated_at: str | None = None
    ttm_days: int
    recent_days: int
    totals: DividendTotals
    by_year: list[DividendYear]
    recent: list[DividendRecent]
    top_yield: list[DividendYielder]
    disclaimer: str


class DividendStock(DividendYielder):
    """One row of the all-emiten dividend table."""

    total_events: int
    # Cash paid per share summed over the emiten's whole history. Only the
    # internal comparisons make sense; it is not split-adjusted.
    total_cash_per_share: float
    first_ex_date: str | None = None
    last_ex_date: str | None = None


class DividendPayment(BaseModel):
    ex_date: str | None = None
    cash_amount: float | None = None


class DividendAnnual(BaseModel):
    year: int
    cash: float
    events: int


class SplitAction(BaseModel):
    ex_date: str | None = None
    action_type: str
    ratio: float | None = None


class DividendDetail(BaseModel):
    """Per-emiten dividend history + split history.

    `total_cash` is per-share cash summed over the whole history and is NOT
    split-adjusted, so a split inflates it; `splits` is returned alongside so
    the UI can warn about exactly that.
    """

    code: str
    name: str | None = None
    as_of: str | None = None
    close: float | None = None
    ttm_cash: float
    ttm_events: int
    yield_pct: float | None = None
    total_cash: float
    total_events: int
    first_ex_date: str | None = None
    last_ex_date: str | None = None
    growth_pct: float | None = None
    history: list[DividendPayment]
    annual: list[DividendAnnual]
    splits: list[SplitAction]
    disclaimer: str


class CorpActionRow(BaseModel):
    """One row of the raw corporate-action ledger."""

    code: str
    name: str | None = None
    ex_date: str | None = None
    action_type: str
    ratio: float | None = None
    cash_amount: float | None = None
    source: str | None = None


# --------------------------------------------------------------- backtest


class StrategyParam(BaseModel):
    """One tunable numeric parameter of a backtest strategy."""

    key: str
    label: str
    default: float
    min: float | None = None
    max: float | None = None
    step: float = 1


class StrategyInfo(BaseModel):
    id: str
    name: str
    description: str
    params: list[StrategyParam]


class CostSettings(BaseModel):
    """Optional cost overrides. ``None`` (the default) uses the realistic IDX
    default for that knob; ``0`` explicitly disables it."""

    commission_pct: float | None = None
    sell_tax_pct: float | None = None
    slippage_pct: float | None = None
    max_adv_pct: float | None = None


class CostModelInfo(BaseModel):
    """The cost model actually used by a run (all values resolved)."""

    commission_pct: float
    sell_tax_pct: float
    slippage_pct: float
    max_adv_pct: float


class RebalanceOption(BaseModel):
    """One selectable rebalance cadence (daily / weekly / monthly)."""

    id: str
    label: str
    description: str


class BacktestConfig(BaseModel):
    """Everything the simulator UI needs to render its form."""

    strategies: list[StrategyInfo]
    cost_defaults: CostModelInfo
    rebalance_options: list[RebalanceOption]
    rebalance_default: str
    max_codes: int
    max_days: int


class BacktestRequest(BaseModel):
    """Run request. codes defaults to the .env watchlist; dates to all history."""

    strategy: str
    codes: list[str] | None = None
    start: str | None = None  # YYYY-MM-DD
    end: str | None = None  # YYYY-MM-DD
    initial_cash: float = 100_000_000
    params: dict[str, float] = {}
    costs: CostSettings = CostSettings()
    # Must match simulation.DEFAULT_REBALANCE; the UI sends it explicitly anyway.
    rebalance: str = "weekly"


class EquityPoint(BaseModel):
    """One simulated day: strategy equity vs the buy & hold benchmark."""

    date: str
    equity: float
    benchmark: float | None = None


class BacktestMetrics(BaseModel):
    final_equity: float
    total_return: float
    annualized_return: float | None = None
    max_drawdown: float
    volatility: float | None = None
    sharpe: float | None = None


class TradeRow(BaseModel):
    """One executed fill inside a rebalance."""

    code: str
    side: str  # "buy" | "sell"
    shares: float
    price: float  # execution price, slippage included
    notional: float
    fee: float
    slippage: float


class HoldingRow(BaseModel):
    """A position in the book at the end of a rebalance."""

    code: str
    shares: float
    price: float
    value: float


class RebalanceEvent(BaseModel):
    """What one rebalance traded, and the book it left behind."""

    date: str
    cash: float
    turnover: float
    fees: float
    trades: list[TradeRow]
    holdings: list[HoldingRow]


class CostImpact(BaseModel):
    """What trading frictions actually cost over the whole simulation."""

    total_fees: float
    total_slippage: float
    total_cost: float
    turnover: float
    cost_pct_of_equity: float | None = None


# --------------------------------------------------------------- alert rules


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


class BacktestResult(BaseModel):
    strategy: str
    strategy_name: str
    params: dict[str, float]
    codes: list[str]
    start: str
    end: str
    days: int
    initial_cash: float
    metrics: BacktestMetrics  # after costs (realistic)
    gross_metrics: BacktestMetrics  # before costs (frictionless)
    benchmark: BacktestMetrics
    costs: CostModelInfo
    rebalance: str
    rebalance_days: int
    rebalances: list[RebalanceEvent]  # newest first, capped (see rebalances_truncated)
    total_rebalances: int
    rebalances_truncated: bool
    cost_impact: CostImpact
    equity_curve: list[EquityPoint]
    disclaimer: str
