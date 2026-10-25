"""Skema aliran dana asing pasar & per emiten."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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
