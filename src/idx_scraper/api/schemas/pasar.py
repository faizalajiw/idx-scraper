"""Skema ringkasan pasar: overview, leaders, sentimen, rezim, health."""

from __future__ import annotations

from datetime import datetime

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


class MarketTradeSegment(BaseModel):
    """Satu segmen pasar: volume dalam lembar & lot (1 lot = 100 lembar)."""

    volume_shares: float | None = None
    volume_lot: float | None = None
    value: float | None = None
    frequency: float | None = None


class MarketTradeSummary(BaseModel):
    """Pasar reguler vs non-reguler (tunai + negosiasi) untuk satu sesi."""

    date: str | None = None
    captured_at: str | None = None
    stock_count: int = 0
    regular: MarketTradeSegment
    non_regular: MarketTradeSegment
    total: MarketTradeSegment
    # Porsi non-reguler terhadap total pasar (0..1) — biasanya kecil.
    non_regular_share_volume: float | None = None
    non_regular_share_value: float | None = None


class StockSearchResult(BaseModel):
    code: str
    name: str | None = None


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


class Freshness(BaseModel):
    """Tiga status kesegaran (spec D1) — "angka ini masih bisa dipercaya?".

    ``withhold`` True = permukaan wajib menahan verdict yang lahir dari data
    ini (hanya angka, tanggal, dan ``reason`` yang boleh tampil).
    ``index_status`` adalah penanda terpisah untuk bagian berindeks (mis. kartu
    rezim): ia tidak pernah mengubah ``status`` halaman.
    """

    status: str
    withhold: bool
    reason: str
    panel_latest: str | None = None
    index_latest: str | None = None
    index_status: str
    expected: str | None = None
    checked_at: str
    basis: str = "panel"


class RegimeGate(BaseModel):
    """Gerbang rezim (spec 6.5): boleh menolak, tak boleh membisukan tanpa sebab.

    ``blocked`` True = papan kandidat menahan tawaran baru dan menyebut
    ``reason`` di layar. ``volatile`` cuma penanda (plafon ukuran posisi
    urusan lapis 3). ``regime_fresh`` False selalu berarti tidak memblokir:
    rezim basi menandai dirinya sendiri, bukan membisukan halaman (spec D1).
    """

    blocked: bool
    reason: str
    regime: str | None = None
    adx: float | None = None
    vol_state: str | None = None
    volatile: bool = False
    regime_fresh: bool = True


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
    gate: RegimeGate | None = None


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


class NewsItem(BaseModel):
    title: str
    source: str
    url: str
    published_at: datetime


class NewsList(BaseModel):
    items: list[NewsItem]


class DataFreshness(BaseModel):
    """Kesegaran EOD: tanggal terbaru vs hari bursa yang diharapkan.

    ``stocks_behind`` / ``index_behind`` = berapa sesi bursa yang tertinggal per
    sumber (0 = hanya sesi hari ini yang belum masuk; itu normal sebelum ~16:15
    WIB). ``overdue`` = sudah lewat jam batas di hari kerja.
    """

    expected: str | None = None
    eod_latest: str | None = None
    index_latest: str | None = None
    overdue: bool = False
    missing: list[str] = []
    stocks_behind: int = 0
    index_behind: int = 0


class CoverageCheck(BaseModel):
    """Satu pemeriksa cakupan: berapa sesi dinilai + sesi yang bermasalah.

    ``checked == 0`` berarti tidak ada yang bisa diperiksa (mis. `raw_eod`
    kosong) — bukan berarti aman.
    """

    checked: int = 0
    worst_pct: float | None = None
    troublemakers: list[str] = []


class RowCountCheck(CoverageCheck):
    """Jumlah baris per sesi vs median jendela terakhir (sesi separuh)."""

    median_rows: int = 0
    threshold_pct: float = 50.0


class SessionGapsCheck(BaseModel):
    """Hari bursa tanpa baris raw_eod antara sesi pertama & terakhir.

    ``basis``: "kalender-bursa" (dipinjam dari data IHSG), "kalender-bursa-pendek"
    (acuan IHSG tidak menutup seluruh rentang — lubang di luar cakupannya tidak
    terlihat), atau "hari-kerja" (fallback kasar; libur nasional tak terdaftar
    bisa keliru ditandai).
    """

    first: str | None = None
    last: str | None = None
    sessions: int = 0
    missing: list[str] = []
    basis: str = "kalender-bursa"


class DataHealth(BaseModel):
    """Ringkasan "boleh percaya angka di dashboard?": kesegaran + 3 pemeriksa cakupan.

    ``ok`` = bersih DAN lengkap: tidak ada yang tertunda (`freshness.missing`
    kosong), tiap pemeriksa benar-benar memeriksa sesuatu (`checked > 0`) dan
    tidak menemukan sesi bermasalah. Ini BUKAN `EodHealth.ok` (yang cuma "tidak
    overdue"): hari kerja sebelum ±16:15 WIB menghasilkan ``ok: false`` yang wajar
    — sesi hari ini belum wajib ada. Konsumen yang mencari "perlu tindakan?"
    membaca ``freshness.overdue``.
    """

    checked_at: datetime
    ok: bool
    freshness: DataFreshness
    field_coverage: CoverageCheck
    row_counts: RowCountCheck
    session_gaps: SessionGapsCheck
