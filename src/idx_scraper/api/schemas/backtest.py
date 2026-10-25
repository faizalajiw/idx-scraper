"""Skema simulator backtest point-in-time."""

from __future__ import annotations

from pydantic import BaseModel


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


# --------------------------------------------------------------- jejak smart money
