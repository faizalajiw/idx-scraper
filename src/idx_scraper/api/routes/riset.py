"""Rute riset: sektor, berita, valuasi, screener, sentimen, faktor."""

from __future__ import annotations

from fastapi import HTTPException, Query

from idx_scraper import news

from .. import analytics
from ..database import get_cursor
from ..schemas import (
    NewsList,
    ScreenerRow,
    SectorAnalysis,
    SectorRotation,
    SentimentResponse,
    SignalTrack,
    ValuationResponse,
)
from ._shared import Router

route = Router()


@route.get("/api/sectors/rotation", response_model=SectorRotation)
def sectors_rotation(
    lookback: int = Query(
        default=60, ge=5, le=250, description="Hari bursa riwayat median skor sektor"
    ),
    min_names: int = Query(default=3, ge=2, le=50, description="Minimum emiten berskor per sektor"),
) -> dict:
    """Rotasi sektor dari skor aktivitas broker (proksi aliran dana).

    Mengukur sektor mana yang jejak akumulasi alirannya sedang naik. Sektor
    dengan emiten berskor kurang dari ``min_names`` dibuang; emiten tanpa
    sektor sebenarnya tidak diikutkan.
    """
    return analytics.get_sector_rotation(lookback=lookback, min_names=min_names)


@route.get("/api/sectors", response_model=SectorAnalysis)
def sectors(date: str | None = Query(default=None)) -> dict:
    return analytics.get_sector_analysis(date=date)


@route.get("/api/news/latest", response_model=NewsList)
def news_latest(limit: int = Query(default=7, ge=1, le=50)) -> dict:
    with get_cursor() as cur:
        return {"items": news.latest(cur, limit=limit)}


@route.post("/api/cache/clear")
def cache_clear() -> dict:
    """Kosongkan cache analitik in-process (dipakai serve loop tiap refresh)."""
    return {"cleared": analytics.clear_research_cache()}


@route.get("/api/valuation", response_model=ValuationResponse)
def valuation() -> dict:
    return analytics.get_valuation()


@route.get("/api/screener", response_model=list[ScreenerRow])
def screener(
    signal: str | None = Query(default=None, pattern="^(BUY|SELL|HOLD)$"),
    rsi_min: float | None = Query(default=None, ge=0, le=100),
    rsi_max: float | None = Query(default=None, ge=0, le=100),
    min_momentum: float | None = Query(default=None),
    max_momentum: float | None = Query(default=None),
    min_value: float | None = Query(default=None, ge=0),
    foreign_in_only: bool = Query(default=False),
    min_vol_ratio: float | None = Query(default=None, ge=0),
    min_broker_score: float | None = Query(
        default=None,
        ge=0,
        le=100,
        description="Skor aktivitas broker minimal (0-100); butuh skor tervalidasi IC",
    ),
    min_days: int = Query(default=30, ge=1, le=500),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    return analytics.get_screener(
        signal=signal,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        min_momentum=min_momentum,
        max_momentum=max_momentum,
        min_value=min_value,
        foreign_in_only=foreign_in_only,
        min_vol_ratio=min_vol_ratio,
        min_broker_score=min_broker_score,
        min_days=min_days,
        limit=limit,
    )


@route.get("/api/signals/track", response_model=SignalTrack)
def signals_track() -> dict:
    """Track record sinyal BUY/SELL: hit-rate, forward return, abnormal vs pasar.

    Statistik dipecah per jenis sinyal, horizon, dan regime IHSG saat sinyal
    terbentuk ? supaya "sinyal ini benar-benar berguna?" terjawab dengan angka,
    bukan opini. Log diisi job harian (`idx signal-log`) atau backfill otomatis.
    """
    return analytics.get_signal_track()


@route.get("/api/sentiment", response_model=SentimentResponse)
def sentiment(limit: int = Query(default=15, ge=1, le=50)) -> dict:
    """Sentimen posisi & aliran dari data tersimpan (arus asing + buku intraday).

    Bukan sentimen berita: skornya dihitung dari jejak transaksi yang sudah kita
    punya, jadi bisa diverifikasi dan tidak butuh sumber baru.
    """
    return analytics.get_sentiment(limit=limit)


@route.get("/api/stocks/{code}/events")
def stock_events(code: str) -> dict:
    """Event study utk satu emiten: riwayat event + baseline pasar (cache 1 jam)."""
    result = analytics.get_stock_events(code.upper())
    if result is None:
        raise HTTPException(status_code=404, detail=f"No event study data for {code.upper()}")
    return result


@route.get("/api/factors/overview")
def factors_overview() -> dict:
    """Kalibrasi faktor: run IC terbaru, bobot aktif, histori rekalibrasi."""
    return analytics.get_factors_overview()


@route.get("/api/market/regime/history")
def regime_history(days: int = Query(default=90, ge=30, le=500)) -> dict:
    """Histori regime harian + agregat (% waktu per regime, transisi)."""
    return analytics.get_regime_history(days=days)
