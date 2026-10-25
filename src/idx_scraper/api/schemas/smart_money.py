"""Skema smart money: radar, verdict, pola, track record."""

from __future__ import annotations

from pydantic import BaseModel


class SmartMoneyRadarRow(BaseModel):
    """Satu baris radar: emiten dengan jejak aliran asing terkuat di jendela N."""

    code: str
    name: str | None = None
    side: str  # "akumulasi" | "distribusi"
    net_sum_idr: float | None = None
    netval_pct: float | None = None
    streak: int | None = None
    window_value: float | None = None
    date: str | None = None


class SmartMoneyRadar(BaseModel):
    days: int
    date: str | None = None
    accumulation: list[SmartMoneyRadarRow]
    distribution: list[SmartMoneyRadarRow]
    scanned: int


class SmartMoneyVerdict(BaseModel):
    side: str  # "akumulasi" | "distribusi" | "netral"
    strength: str | None = None  # "besar" | "menengah" | None
    streak: int | None = None
    streak_side: str | None = None  # "net_buy" | "net_sell" | "flat"
    streak_start_date: str | None = None  # sesi pertama streak berjalan
    net_sum_idr: float | None = None
    netval_pct: float | None = None
    n_net_days: int | None = None
    date: str | None = None
    insufficient: bool = False


class SmartMoneyHorizonStats(BaseModel):
    """Statistik satu horizon (5/10/21 hari bursa) untuk satu pola."""

    n: int = 0
    aligned_hit_rate: float | None = None
    tstat: float | None = None
    effective_alpha: float | None = None
    horizon_days: int | None = None


class SmartMoneyPatternHistory(BaseModel):
    """Arti historis satu pola: seberapa sering bekerja, di horizon berapa.

    ``horizon``/``horizon_days`` = horizon dengan bukti terbaik (sampel >=
    ``MIN_HISTORY_N``). ``reliable`` False = horizon terbaik pun masih sampel
    kecil, jadi angkanya indikatif dan UI wajib mengatakannya.
    """

    n: int | None = None
    n_resolved: int | None = None
    window_sessions: int | None = None
    horizon: str | None = None
    horizon_days: int | None = None
    aligned_hit_rate: float | None = None
    n_resolved_horizon: int | None = None
    tstat: float | None = None
    effective_alpha: float | None = None
    reliable: bool = True
    confidence: str | None = None  # "tinggi" | "sedang" | "lemah" (dari |t|)
    best_tstat: float | None = None
    edge_pct: float | None = None  # |effective_alpha| di horizon terpilih
    horizons: dict[str, SmartMoneyHorizonStats] = {}


class SmartMoneyPattern(BaseModel):
    id: str
    label: str
    direction: str | None = None  # "buy-side" | "sell-side"
    note: str | None = None
    history: SmartMoneyPatternHistory | None = None


class SmartMoneyRange(BaseModel):
    lookback: int
    low: float | None = None
    high: float | None = None
    support: float | None = None
    resistance: float | None = None
    range_pct: float | None = None


class SmartMoneySectorContext(BaseModel):
    sector: str | None = None
    comparable: bool = True
    sector_total: int | None = None
    accumulating: int | None = None
    distributing: int | None = None
    position: str | None = None


class SmartMoneyPatternsBoardRow(BaseModel):
    """Satu emiten yang memicu sebuah pola di sesi terakhir."""

    code: str
    name: str | None = None
    side: str | None = None
    net_sum_idr: float | None = None
    netval_pct: float | None = None
    streak: int | None = None
    window_value: float | None = None
    date: str | None = None


class SmartMoneyPatternsBoardGroup(BaseModel):
    """Satu pola + buktinya + emiten yang memicunya.

    ``fired`` = semua emiten yang memicu pola (termasuk yang tipis);
    ``count`` = yang lolos lantai likuiditas dan boleh ditampilkan. Keduanya
    dikirim supaya UI bisa jujur soal berapa yang disembunyikan lantai.
    """

    pattern: str
    label: str
    direction: str | None = None
    confidence: str | None = None
    horizon_days: int | None = None
    aligned_hit_rate: float | None = None
    n_resolved_horizon: int | None = None
    edge_pct: float | None = None
    fired: int = 0
    count: int = 0
    emitters: list[SmartMoneyPatternsBoardRow] = []


class SmartMoneyPatternsBoard(BaseModel):
    """Papan "pola terkuat hari ini" — dikelompokkan per pola, urut kekuatan bukti."""

    date: str | None = None
    scanned: int = 0
    window_days: int = 10
    min_window_value: float | None = None
    groups: list[SmartMoneyPatternsBoardGroup] = []


class SmartMoneyMarketContext(BaseModel):
    """Sebaran verdict seluruh pasar — pembanding verdict satu emiten.

    ``distributing_share`` = porsi distribusi di antara emiten yang PUNYA arah
    (netral & data kurang keluar dari penyebut); itu pembanding yang benar
    untuk sebuah verdict. ``position`` = kalimat plain-language siap tampil.
    """

    date: str | None = None
    total: int | None = None
    accumulating: int | None = None
    distributing: int | None = None
    neutral: int | None = None
    insufficient: int | None = None
    sided: int | None = None
    accumulating_pct: float | None = None
    distributing_pct: float | None = None
    distributing_share: float | None = None
    position: str | None = None


class SmartMoneyStock(BaseModel):
    code: str
    name: str | None = None
    has_data: bool = False
    verdict: SmartMoneyVerdict | None = None
    patterns: list[SmartMoneyPattern] = []
    range: SmartMoneyRange | None = None
    narrative: list[str] = []
    sector: SmartMoneySectorContext | None = None
    market: SmartMoneyMarketContext | None = None
    date: str | None = None


class SmartMoneyTrackHorizon(BaseModel):
    """Metrik satu horizon forward (5/10/21 sesi) untuk satu pola."""

    n: int = 0
    hit_rate: float | None = None
    aligned_hit_rate: float | None = None
    mean: float | None = None
    median: float | None = None
    tstat: float | None = None
    alpha: float | None = None
    effective_alpha: float | None = None


class SmartMoneyTrackPattern(BaseModel):
    pattern: str
    label: str
    direction: str | None = None
    n: int = 0
    n_resolved: int = 0
    horizons: dict[str, SmartMoneyTrackHorizon] = {}


class SmartMoneyTrackRecord(BaseModel):
    patterns: list[SmartMoneyTrackPattern] = []
    n_episodes: int = 0
    history_sessions: int | None = None
    horizon_note: str | None = None


class SmartMoneyWatchRow(BaseModel):
    """Verdict jejak smart money satu emiten di watchlist (kartu Pantau)."""

    code: str
    name: str | None = None
    side: str | None = None
    insufficient: bool = False
    net_sum_idr: float | None = None
    netval_pct: float | None = None
    streak: int | None = None
    date: str | None = None
    patterns: list[str] = []


class SmartMoneyWatchList(BaseModel):
    n: int = 0
    rows: list[SmartMoneyWatchRow] = []
    telegram_enabled: bool = False
