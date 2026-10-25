"""Skema analisis & rotasi sektor."""

from __future__ import annotations

from pydantic import BaseModel


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
