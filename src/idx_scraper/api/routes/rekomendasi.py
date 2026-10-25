"""Rute rekomendasi: papan kandidat, track record, keputusan."""

from __future__ import annotations

from fastapi import Query

from .. import analytics
from ..schemas import (
    RecommendationResponse,
    RecommendationTrack,
    StockBrokerActivity,
    StockBrokerSummary,
    StockDecision,
    StockRecommendation,
)
from ._shared import Router

route = Router()


@route.get("/api/stocks/{code}/brokers", response_model=StockBrokerSummary)
def stock_brokers(code: str, date: str | None = Query(default=None)) -> dict:
    return analytics.get_broker_summary(code.upper(), date=date)


@route.get("/api/recommendations", response_model=RecommendationResponse)
def recommendations(
    limit: int = Query(default=20, ge=1, le=100, description="Jumlah kandidat yang ditampilkan"),
    min_grade: str = Query(
        default="C", pattern="^[ABCabc]$", description="Grade terendah yang ditampilkan"
    ),
    modal: float | None = Query(
        default=None, gt=0, description="Modal rupiah; kosong = mode persen saja"
    ),
    risk_pct: float | None = Query(
        default=None, gt=0, le=100, description="Risiko per transaksi (% modal)"
    ),
    open_positions: int | None = Query(
        default=None, ge=0, description="Jumlah posisi terbuka (angka yang kamu isi)"
    ),
    exposure_pct: float | None = Query(
        default=None, ge=0, le=100, description="Eksposur sekarang (% modal)"
    ),
) -> dict:
    """Papan kandidat beli se-pasar: skor, grade, dan level eksekusi per emiten.

    Lapisan faktor IC dan aliran broker ikut HANYA bila lolos gate IC; kalau
    belum, keduanya tidak menggerakkan skor dan statusnya disebut di ``layers``.
    Alat bantu riset, bukan rekomendasi keuangan.
    """
    return analytics.get_recommendations(
        limit=limit,
        min_grade=min_grade.upper(),
        modal=modal,
        risk_pct=risk_pct,
        open_positions=open_positions,
        exposure_pct=exposure_pct,
    )


@route.get("/api/recommendations/track", response_model=RecommendationTrack)
def recommendations_track() -> dict:
    """Track record kandidat beli: hit rate & abnormal return per grade/horizon."""
    return analytics.get_recommendation_track()


@route.get("/api/stocks/{code}/recommendation", response_model=StockRecommendation)
def stock_recommendation(
    code: str,
    modal: float | None = Query(
        default=None, gt=0, description="Modal rupiah; kosong = mode persen saja"
    ),
    risk_pct: float | None = Query(
        default=None, gt=0, le=100, description="Risiko per transaksi (% modal)"
    ),
    open_positions: int | None = Query(
        default=None, ge=0, description="Jumlah posisi terbuka (angka yang kamu isi)"
    ),
    exposure_pct: float | None = Query(
        default=None, ge=0, le=100, description="Eksposur sekarang (% modal)"
    ),
) -> dict:
    """Kandidat beli satu emiten (skor & level memakai mesin yang sama dengan papan)."""
    return analytics.get_stock_recommendation(
        code.upper(),
        modal=modal,
        risk_pct=risk_pct,
        open_positions=open_positions,
        exposure_pct=exposure_pct,
    )


@route.get("/api/stocks/{code}/decision", response_model=StockDecision)
def stock_decision(code: str) -> dict:
    """Ruang Keputusan satu emiten: verdict + konteks + level invalidasi.

    Menggabungkan hold-check (verdict dasar), regime IHSG, sentimen aliran,
    jejak asing 10 sesi, base rate event study, dan level pembatalan teknikal
    ke dalam satu respons — semua dari sumber yang sudah ada, cache 30 menit.
    """
    return analytics.get_stock_decision(code.upper())


@route.get("/api/stocks/{code}/broker-activity", response_model=StockBrokerActivity)
def stock_broker_activity(
    code: str,
    lookback: int = Query(default=60, ge=2, le=250, description="Jumlah hari bursa riwayat skor"),
) -> dict:
    """Skor aktivitas broker + riwayat driver untuk satu emiten (cache 1 jam).

    Skor terkini identik dengan angka di halaman Aktivitas Broker; riwayatnya
    dihitung per tanggal terhadap pasar hari itu. Kalau faktor aliran belum lolos
    gate IC, ``validated`` False dan ``current``/``history`` kosong.
    """
    return analytics.get_stock_broker_activity(code.upper(), lookback)
