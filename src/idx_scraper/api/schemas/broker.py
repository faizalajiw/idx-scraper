"""Skema broker: summary, konsentrasi, flow, dan aktivitas broker."""

from __future__ import annotations

from pydantic import BaseModel


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
