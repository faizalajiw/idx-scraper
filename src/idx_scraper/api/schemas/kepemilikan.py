"""Skema pemilik & aksi pemilik (keterbukaan IDX)."""

from __future__ import annotations

from pydantic import BaseModel


class OwnershipHolder(BaseModel):
    """Satu pemegang saham pada snapshot terbaru."""

    holder_name: str
    category: str | None = None
    shares: float | None = None
    pct: float | None = None
    is_controller: bool = False


class OwnershipChange(BaseModel):
    """Aksi pemilik: perubahan porsi vs snapshot sebelumnya."""

    holder_name: str
    category: str | None = None
    prev_pct: float | None = None
    curr_pct: float | None = None
    delta_pct: float | None = None
    action: str  # tambah | kurang | baru | keluar


class StockOwnership(BaseModel):
    code: str
    has_data: bool = False
    as_of: str | None = None
    prev_date: str | None = None
    holders: list[OwnershipHolder] = []
    free_float_pct: float | None = None
    controller: list[str] = []
    changes: list[OwnershipChange] = []
