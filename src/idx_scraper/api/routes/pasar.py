"""Rute pasar & broker: kesehatan data, overview, regime, leaders, broker."""

from __future__ import annotations

from fastapi import Query

from .. import analytics, services
from ..schemas import (
    BrokerActivity,
    BrokerFlow,
    DataHealth,
    Freshness,
    MarketLeaders,
    MarketOverview,
    MarketRegime,
    MarketTradeSummary,
    SessionMovers,
    TopBrokers,
)
from ._shared import Router

route = Router()


@route.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@route.get("/api/data-health", response_model=DataHealth)
def data_health(
    sessions: int = Query(
        default=30, ge=1, le=250, description="Jumlah sesi terakhir yang dinilai tiap pemeriksa"
    ),
    row_threshold_pct: float = Query(
        default=50.0,
        ge=1.0,
        le=100.0,
        description="Ambang baris vs median (%) di bawahnya sesi ditandai",
    ),
) -> dict:
    """Kesegaran + cakupan data EOD: "boleh percaya angka di dashboard?".

    Empat pemeriksa read-only (kesegaran, kolom `foreign_*`, jumlah baris per
    sesi, lubang sesi). Logikanya hidup di `idx_scraper.health` dan dipakai
    bersama oleh `idx health` / job `health_eod`.
    """
    return services.get_data_health(sessions, row_threshold_pct)


@route.get("/api/freshness", response_model=Freshness)
def freshness() -> dict:
    """Status kesegaran data satu-baris (spec D1): `segar` / `basi` / `tak diketahui`.

    Dipakai badge di tiap halaman untuk menjawab "angka ini masih bisa
    dipercaya?" tanpa memuat empat pemeriksa `/api/data-health`. Jam batas EOD
    dihormati: sebelum ±16:00 WIB, sesi hari ini memang belum wajib ada.
    """
    return services.get_freshness()


@route.get("/api/market/overview", response_model=MarketOverview)
def market_overview() -> dict:
    return services.get_market_overview()


@route.get("/api/market/trade-summary", response_model=MarketTradeSummary)
def market_trade_summary(
    date: str | None = Query(default=None, description="YYYY-MM-DD (default: sesi terbaru)"),
) -> dict:
    """Pasar reguler vs non-reguler: volume (lembar + lot) & value satu sesi.

    Non-reguler = pasar tunai + negosiasi. Angka berasal dari GetStockSummary
    IDX yang memisahkan keduanya per emiten; worker mengagregasinya ke
    research.market_segment_daily. Endpoint ini hanya membaca.
    """
    return services.get_market_trade_summary(date)


@route.get("/api/market/regime", response_model=MarketRegime)
def market_regime() -> dict:
    """Regime IHSG (ADX + realized vol) — konteks untuk semua halaman analisis."""
    return analytics.get_market_regime()


@route.get("/api/market/session-movers", response_model=SessionMovers)
def session_movers() -> dict:
    return services.get_session_movers()


@route.get("/api/market/leaders", response_model=MarketLeaders)
def market_leaders(
    metric: str = Query(default="volume", description="volume | value | frequency"),
    limit: int = Query(default=5, ge=1, le=50),
) -> dict:
    """Top emiten by volume/value/frequency — realtime saat jam bursa, else EOD."""
    return services.get_market_leaders(metric, limit)


@route.get("/api/market/top-brokers", response_model=TopBrokers)
def top_brokers(limit: int = Query(default=5, ge=1, le=50)) -> dict:
    """Top broker firm by traded value (EOD GetBrokerSummary)."""
    return services.get_top_brokers(limit)


@route.get("/api/broker-flow", response_model=BrokerFlow)
def broker_flow(
    days: int = Query(default=20, ge=1, le=120, description="Jendela tren riwayat (hari kalender)"),
    top_n: int = Query(default=5, ge=1, le=20, description="Firma teratas per kategori"),
) -> dict:
    """Komposisi nilai transaksi broker per kategori: asing / lokal / BUMN.

    Komposisi turnover, bukan net buy/sell (IDX tidak menyediakan split
    beli/jual per firma). Sumber: research.broker_daily.
    """
    return analytics.get_broker_flow(days=days, top_n=top_n)


@route.get("/api/broker-activity", response_model=BrokerActivity)
def broker_activity(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    """Skor aktivitas broker per emiten (proksi aliran, bobot dari IC) + struktur broker pasar.

    Skor hanya dibentuk dari faktor yang lolos gate IC; kalau tidak ada yang
    lolos, ``validated`` False dan ``rows`` kosong (lihat research.broker_activity).
    """
    return analytics.get_broker_activity(limit)
