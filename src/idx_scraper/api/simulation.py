"""Backtest / strategy simulator service.

API-facing layer over ``idx_scraper.backtest`` (``PITReader`` + ``Backtester``).
Everything is point-in-time safe: a strategy standing on trading day T only
sees bars with ``trade_date <= T``, and prices are split/bonus-adjusted using
only the corporate actions known at that time (see ``sql/pit_functions.sql``),
so a run can never peek at the future.

Design notes
------------
* Strategies are declarative cross-sectional rules — transparent, no black box.
* How often the book is traded is orthogonal to the strategy, so it lives with
  the engine: weekly/monthly runs let positions drift between rebalance days
  instead of forcing every day's targets back to weight.
* Each code's series is fetched ONCE per run, then sliced per simulated day, so
  a run costs O(codes) queries instead of O(codes x days).
* The benchmark is always an equal-weighted buy & hold of the same universe,
  built from the same series, so the comparison is apples-to-apples.
* Trading frictions (commission, sell tax, slippage, liquidity cap) are applied
  through the engine's ``CostModel``, and the no-cost counterfactual is run too
  so the UI can show how much costs actually eat. When every friction is zero
  the extra pass is skipped.
* This is a research aid, not investment advice. See ``DISCLAIMER``.

``psycopg`` (and therefore ``idx_scraper.backtest``) is imported lazily inside
``run_backtest`` so the pure selection/metric logic stays unit-testable without
a database.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..backtest import PITReader

TRADING_DAYS_PER_YEAR = 252
MAX_CODES = 60
MAX_DAYS = 756  # ~3 years of trading days

# The trade ledger is bounded so a long daily run stays a sane HTTP payload.
# Aggregates (turnover, cost) are always complete; only the per-trade detail is
# trimmed, newest first.
MAX_REBALANCE_EVENTS = 200
MAX_REBALANCE_TRADES = 4000

# Realistic IDX frictions: a typical online-broker commission, the 0.1% final
# sales tax, a deliberately conservative slippage guess, and a 2% participation
# cap against the day's traded value. Callers may override or zero them out.
DEFAULT_COSTS: dict[str, float] = {
    "commission_pct": 0.0015,
    "sell_tax_pct": 0.001,
    "slippage_pct": 0.001,
    "max_adv_pct": 0.02,
}

COST_LIMITS: dict[str, tuple[float, float]] = {
    "commission_pct": (0.0, 0.02),
    "sell_tax_pct": (0.0, 0.02),
    "slippage_pct": (0.0, 0.05),
    "max_adv_pct": (0.0, 1.0),
}

# Rebalance cadences. Ids must stay in sync with backtest.RebalanceFreq (a test
# asserts it); daily trading churns almost the whole book every session, so
# weekly is the sensible research default.
REBALANCE_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "daily",
        "label": "Harian",
        "description": "Rebalance tiap hari bursa — turnover dan biaya paling tinggi.",
    },
    {
        "id": "weekly",
        "label": "Mingguan",
        "description": "Rebalance di hari bursa pertama tiap minggu (kalender ISO).",
    },
    {
        "id": "monthly",
        "label": "Bulanan",
        "description": "Rebalance di hari bursa pertama tiap bulan — turnover paling rendah.",
    },
)

DEFAULT_REBALANCE = "weekly"

DISCLAIMER = (
    "Hasil backtest adalah simulasi historis, bukan prediksi atau nasihat "
    "keuangan. Komisi, pajak jual, slippage, dan batas likuiditas dimodelkan "
    "secara kasar, jadi hasil nyata bisa tetap berbeda. Data IDX gratis "
    "bersifat delayed dan bisa direvisi."
)

# code -> [(trade_date, adjusted_close), ...] sorted ascending by date
SeriesMap = dict[str, list[tuple[date, float]]]
# (series, simulated_day, resolved_params) -> codes to hold, equal-weighted
Selector = Callable[[SeriesMap, date, dict[str, float]], list[str]]


# ------------------------------------------------------------------ parameters


@dataclass(frozen=True)
class ParamSpec:
    """One tunable numeric parameter of a strategy."""

    key: str
    label: str
    default: float
    min: float | None = None
    max: float | None = None
    step: float = 1


# ------------------------------------------------------------------ primitives


def _upto(points: list[tuple[date, float]], d: date) -> list[tuple[date, float]]:
    """Prefix of ``points`` whose trade_date is <= ``d`` (sorted ascending)."""
    idx = bisect.bisect_right(points, (d, math.inf))
    return points[:idx]


def _closes(points: list[tuple[date, float]], d: date, n: int | None = None) -> list[float]:
    """Adjusted closes known by day ``d``; the last ``n`` when given."""
    values = [price for _, price in _upto(points, d)]
    return values[-n:] if n else values


def _equal_weight(selected: list[str]) -> dict[str, float]:
    """Spread 100% of capital evenly across the selected codes."""
    if not selected:
        return {}
    return dict.fromkeys(selected, 1.0 / len(selected))


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder-style simple-average RSI, matching ``analysis.calculate_indicators``."""
    if period < 1 or len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


# ------------------------------------------------------------------ strategies


def _select_ma_cross(
    series: SeriesMap, d: date, short: int = 20, long: int = 50
) -> list[str]:
    """Hold every code whose short moving average is above its long one."""
    selected: list[str] = []
    for code, points in series.items():
        closes = _closes(points, d)
        if len(closes) < long:
            continue
        if statistics.fmean(closes[-short:]) > statistics.fmean(closes[-long:]):
            selected.append(code)
    return sorted(selected)


def _select_momentum(
    series: SeriesMap, d: date, lookback: int = 20, top_n: int = 5
) -> list[str]:
    """Hold the top-N codes by trailing return over ``lookback`` days."""
    scored: list[tuple[float, str]] = []
    for code, points in series.items():
        closes = _closes(points, d)
        if len(closes) <= lookback:
            continue
        base = closes[-1 - lookback]
        if base <= 0:
            continue
        scored.append((closes[-1] / base - 1.0, code))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return sorted(code for _, code in scored[: int(top_n)])


def _select_rsi_oversold(
    series: SeriesMap, d: date, period: int = 14, threshold: float = 35
) -> list[str]:
    """Hold the codes whose RSI is below ``threshold`` (oversold bounce)."""
    selected: list[str] = []
    for code, points in series.items():
        value = _rsi(_closes(points, d), int(period))
        if value is not None and value < threshold:
            selected.append(code)
    return sorted(selected)


def _ma_cross(series: SeriesMap, d: date, params: dict[str, float]) -> list[str]:
    return _select_ma_cross(series, d, short=int(params["short"]), long=int(params["long"]))


def _momentum(series: SeriesMap, d: date, params: dict[str, float]) -> list[str]:
    return _select_momentum(
        series, d, lookback=int(params["lookback"]), top_n=int(params["top_n"])
    )


def _rsi_oversold(series: SeriesMap, d: date, params: dict[str, float]) -> list[str]:
    return _select_rsi_oversold(
        series, d, period=int(params["period"]), threshold=float(params["threshold"])
    )


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    description: str
    params: tuple[ParamSpec, ...]
    select: Selector


STRATEGIES: dict[str, StrategySpec] = {
    "ma_cross": StrategySpec(
        id="ma_cross",
        name="MA Cross (Trend Following)",
        description=(
            "Pegang emiten yang SMA jangka pendeknya di atas SMA jangka panjang. "
            "Gaya trend-following: ikut tren naik, keluar saat tren patah."
        ),
        params=(
            ParamSpec("short", "SMA pendek (hari)", 20, min=2, max=200),
            ParamSpec("long", "SMA panjang (hari)", 50, min=3, max=400),
        ),
        select=_ma_cross,
    ),
    "momentum_top": StrategySpec(
        id="momentum_top",
        name="Momentum Top-N (Rotasi)",
        description=(
            "Pegang N emiten dengan return terbaik dalam periode lookback, "
            "direbalance tiap hari bursa. Strategi rotasi momentum lintas-saham."
        ),
        params=(
            ParamSpec("lookback", "Lookback momentum (hari)", 20, min=2, max=250),
            ParamSpec("top_n", "Jumlah emiten (top N)", 5, min=1, max=30),
        ),
        select=_momentum,
    ),
    "rsi_oversold": StrategySpec(
        id="rsi_oversold",
        name="RSI Oversold (Mean Reversion)",
        description=(
            "Pegang emiten dengan RSI di bawah ambang (jenuh jual), berharap "
            "rebound. Gaya mean-reversion: beli saat lemah, keluar saat RSI pulih."
        ),
        params=(
            ParamSpec("period", "Periode RSI", 14, min=2, max=50),
            ParamSpec("threshold", "Ambang RSI", 35, min=5, max=60),
        ),
        select=_rsi_oversold,
    ),
}


def list_strategies() -> list[dict[str, Any]]:
    """Metadata for every strategy, including its tunable parameters."""
    return [
        {
            "id": spec.id,
            "name": spec.name,
            "description": spec.description,
            "params": [
                {
                    "key": p.key,
                    "label": p.label,
                    "default": p.default,
                    "min": p.min,
                    "max": p.max,
                    "step": p.step,
                }
                for p in spec.params
            ],
        }
        for spec in STRATEGIES.values()
    ]


def get_config() -> dict[str, Any]:
    """Strategies, cost defaults, cadences, and limits — everything the UI form needs."""
    return {
        "strategies": list_strategies(),
        "cost_defaults": dict(DEFAULT_COSTS),
        "rebalance_options": [dict(o) for o in REBALANCE_OPTIONS],
        "rebalance_default": DEFAULT_REBALANCE,
        "max_codes": MAX_CODES,
        "max_days": MAX_DAYS,
    }


def _resolve_costs(raw: dict[str, float | None] | None) -> dict[str, float]:
    """Merge caller overrides with the realistic defaults and validate ranges.

    A ``None`` value (or a missing key) falls back to the default, so an
    explicit 0 — meaning "no cost" — is honoured instead of treated as absent.
    """
    provided = raw or {}
    unknown = set(provided) - set(DEFAULT_COSTS)
    if unknown:
        raise ValueError(f"biaya tidak dikenal: {', '.join(sorted(unknown))}")

    resolved: dict[str, float] = {}
    for key, default in DEFAULT_COSTS.items():
        value = provided.get(key)
        if value is None:
            value = default
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"biaya '{key}' harus berupa angka") from None
        if math.isnan(num) or math.isinf(num):
            raise ValueError(f"biaya '{key}' bukan angka valid")
        low, high = COST_LIMITS[key]
        if not low <= num <= high:
            raise ValueError(f"biaya '{key}' harus di antara {low:g} dan {high:g}")
        resolved[key] = num
    return resolved


def _resolve_params(spec: StrategySpec, raw: dict[str, float] | None) -> dict[str, float]:
    """Merge caller overrides with defaults and validate ranges."""
    provided = raw or {}
    known = {p.key for p in spec.params}
    unknown = set(provided) - known
    if unknown:
        raise ValueError(f"parameter tidak dikenal: {', '.join(sorted(unknown))}")

    resolved: dict[str, float] = {}
    for p in spec.params:
        value = provided.get(p.key, p.default)
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"parameter '{p.key}' harus berupa angka") from None
        if math.isnan(num) or math.isinf(num):
            raise ValueError(f"parameter '{p.key}' bukan angka valid")
        if p.min is not None and num < p.min:
            raise ValueError(f"parameter '{p.key}' minimal {p.min:g}")
        if p.max is not None and num > p.max:
            raise ValueError(f"parameter '{p.key}' maksimal {p.max:g}")
        resolved[p.key] = num
    return resolved


# ------------------------------------------------------------------ series


def _build_series(reader: PITReader, codes: list[str], end: date) -> SeriesMap:
    """Adjusted close series per code, known as of ``end`` (one query per code)."""
    series: SeriesMap = {}
    for code in codes:
        points = [
            (bar.trade_date, float(bar.close))
            for bar in reader.history(code, end)
            if bar.close and bar.close > 0
        ]
        if points:
            points.sort(key=lambda pair: pair[0])
            series[code] = points
    return series


def _make_strategy(
    select: Selector, series: SeriesMap, params: dict[str, float]
) -> Callable[[date, PITReader], dict[str, float]]:
    """Wrap a selector into the ``Strategy`` signature the backtester calls."""

    def strategy(d: date, _reader: PITReader) -> dict[str, float]:
        return _equal_weight(select(series, d, params))

    return strategy


def _buy_and_hold_curve(
    series: SeriesMap,
    days: list[date],
    initial_cash: float,
    commission_pct: float = 0.0,
    slippage_pct: float = 0.0,
) -> list[tuple[date, float]]:
    """Equal-weighted buy & hold benchmark entered on the first simulated day.

    Entry pays commission and slippage so the benchmark carries the same one-off
    friction a real buy & hold would; there is no exit, so the sell tax never
    applies.
    """
    if not days or not series:
        return []

    day0 = days[0]
    entries: dict[str, float] = {}
    for code, points in series.items():
        known = _upto(points, day0)
        if known and known[-1][1] > 0:
            entries[code] = known[-1][1] * (1.0 + slippage_pct)
    if not entries:
        return []

    deployed = initial_cash * (1.0 - commission_pct)
    weight = 1.0 / len(entries)
    curve: list[tuple[date, float]] = []
    for d in days:
        equity = 0.0
        for code, entry in entries.items():
            known = _upto(series[code], d)
            price = known[-1][1] if known else entry
            equity += weight * deployed * (price / entry)
        curve.append((d, equity))
    return curve


# ------------------------------------------------------------------ metrics


@dataclass(frozen=True)
class Metrics:
    final_equity: float
    total_return: float
    annualized_return: float | None
    max_drawdown: float
    volatility: float | None
    sharpe: float | None


def _compute_metrics(curve: list[tuple[date, float]], initial_cash: float) -> Metrics:
    """Return / risk statistics derived from a daily equity curve."""
    if not curve or initial_cash <= 0:
        return Metrics(initial_cash, 0.0, None, 0.0, None, None)

    equities = [equity for _, equity in curve]
    final = equities[-1]
    total = final / initial_cash - 1.0

    returns = [
        equities[i] / equities[i - 1] - 1.0
        for i in range(1, len(equities))
        if equities[i - 1]
    ]
    volatility = (
        statistics.pstdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR)
        if returns
        else None
    )
    mean_return = statistics.fmean(returns) if returns else 0.0
    sharpe = (mean_return * TRADING_DAYS_PER_YEAR / volatility) if volatility else None
    annualized = (
        (final / initial_cash) ** (TRADING_DAYS_PER_YEAR / len(curve)) - 1.0
        if len(curve) > 1 and final > 0
        else None
    )

    peak = equities[0]
    max_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    return Metrics(final, total, annualized, max_drawdown, volatility, sharpe)


def _trade_dict(trade: Any) -> dict[str, Any]:
    return {
        "code": trade.code,
        "side": trade.side,
        "shares": round(trade.shares, 4),
        "price": round(trade.price, 4),
        "notional": round(trade.notional, 2),
        "fee": round(trade.fee, 2),
        "slippage": round(trade.slippage, 2),
    }


def _rebalance_dict(event: Any) -> dict[str, Any]:
    return {
        "date": event.day.isoformat(),
        "cash": round(event.cash, 2),
        "turnover": round(event.turnover, 2),
        "fees": round(event.fees, 2),
        "trades": [_trade_dict(t) for t in event.trades],
        "holdings": [
            {
                "code": h.code,
                "shares": round(h.shares, 4),
                "price": round(h.price, 4),
                "value": round(h.value, 2),
            }
            for h in event.holdings
        ],
    }


def _ledger(events: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Newest-first rebalance events, capped on both event and trade counts."""
    kept: list[dict[str, Any]] = []
    trades = 0
    for event in reversed(events):
        if len(kept) >= MAX_REBALANCE_EVENTS:
            break
        # Always keep at least one event, even if it alone exceeds the trade cap.
        if trades and trades + len(event.trades) > MAX_REBALANCE_TRADES:
            break
        trades += len(event.trades)
        kept.append(_rebalance_dict(event))
    return kept, len(kept) < len(events)


def _metrics_dict(metrics: Metrics) -> dict[str, Any]:
    def r(v: float | None, digits: int = 6) -> float | None:
        return None if v is None else round(v, digits)

    return {
        "final_equity": round(metrics.final_equity, 2),
        "total_return": r(metrics.total_return),
        "annualized_return": r(metrics.annualized_return),
        "max_drawdown": r(metrics.max_drawdown),
        "volatility": r(metrics.volatility),
        "sharpe": r(metrics.sharpe),
    }


# ------------------------------------------------------------------ entrypoint


def run_backtest(
    *,
    strategy_id: str,
    codes: list[str],
    start: date | None = None,
    end: date | None = None,
    initial_cash: float = 100_000_000.0,
    params: dict[str, float] | None = None,
    costs: dict[str, float | None] | None = None,
    rebalance: str = DEFAULT_REBALANCE,
) -> dict[str, Any]:
    """Simulate one strategy over a universe and compare it to buy & hold.

    The result carries both the realistic (after-cost) metrics and the
    frictionless counterfactual, so the caller can see the cost drag.

    Raises ``ValueError`` with a user-facing message for anything invalid, so
    the HTTP layer can turn it into a 422.
    """
    from ..backtest import Backtester, CostModel, PITReader, RebalanceFreq
    from .config import get_settings

    spec = STRATEGIES.get(strategy_id)
    if spec is None:
        raise ValueError(f"strategi tidak dikenal: {strategy_id}")

    universe = list(dict.fromkeys(c.strip().upper() for c in codes if c and c.strip()))
    if not universe:
        raise ValueError("daftar emiten kosong — isi minimal satu kode")
    if len(universe) > MAX_CODES:
        raise ValueError(f"maksimal {MAX_CODES} emiten per backtest (diminta {len(universe)})")
    if initial_cash <= 0:
        raise ValueError("modal awal harus lebih besar dari 0")

    resolved = _resolve_params(spec, params)
    resolved_costs = _resolve_costs(costs)
    cost_model = CostModel(**resolved_costs)
    try:
        frequency = RebalanceFreq(rebalance)
    except ValueError:
        valid = ", ".join(o["id"] for o in REBALANCE_OPTIONS)
        raise ValueError(f"frekuensi rebalance tidak dikenal: {rebalance!r} ({valid})") from None

    settings = get_settings()
    if not settings.database_url:
        raise ValueError("DATABASE_URL belum diset — backtest membutuhkan Postgres")

    reader = PITReader(settings.database_url)
    try:
        all_days = reader.trading_days(date(2000, 1, 1), date(2100, 1, 1))
        if not all_days:
            raise ValueError("belum ada data harga di research.prices_pit")

        first, last = all_days[0], all_days[-1]
        end_day = end or last
        start_day = start or first
        if start_day > end_day:
            raise ValueError("tanggal mulai tidak boleh setelah tanggal akhir")

        days = [d for d in all_days if start_day <= d <= end_day]
        if len(days) > MAX_DAYS:
            days = days[-MAX_DAYS:]
        if len(days) < 2:
            raise ValueError("rentang terlalu pendek — butuh minimal 2 hari bursa")

        series = _build_series(reader, universe, days[-1])
        if not series:
            raise ValueError("tidak ada harga untuk emiten terpilih pada rentang ini")

        strategy = _make_strategy(spec.select, series, resolved)
        runner = Backtester(reader)
        result = runner.run(
            strategy,
            days[0],
            days[-1],
            initial_cash=initial_cash,
            costs=cost_model,
            rebalance=frequency,
        )
        # Frictionless counterfactual. The strategy closure is pure, so the extra
        # pass is deterministic; skip it entirely when there is nothing to model.
        gross = (
            result
            if cost_model.is_frictionless
            else runner.run(
                strategy,
                days[0],
                days[-1],
                initial_cash=initial_cash,
                rebalance=frequency,
                record_ledger=False,
            )
        )
        benchmark_curve = _buy_and_hold_curve(
            series,
            days,
            initial_cash,
            commission_pct=cost_model.commission_pct,
            slippage_pct=cost_model.slippage_pct,
        )
    finally:
        reader.close()

    benchmark_by_date = dict(benchmark_curve)
    equity_curve = [
        {
            "date": d.isoformat(),
            "equity": round(equity, 2),
            "benchmark": (
                round(benchmark_by_date[d], 2) if d in benchmark_by_date else None
            ),
        }
        for d, equity in result.equity_curve
    ]

    net_metrics = _compute_metrics(result.equity_curve, initial_cash)
    total_cost = result.total_fees + result.total_slippage
    ledger, ledger_truncated = _ledger(result.rebalances)

    return {
        "strategy": spec.id,
        "strategy_name": spec.name,
        "params": resolved,
        "codes": sorted(series),
        "start": days[0].isoformat(),
        "end": days[-1].isoformat(),
        "days": len(result.equity_curve),
        "initial_cash": initial_cash,
        "metrics": _metrics_dict(net_metrics),
        "gross_metrics": _metrics_dict(_compute_metrics(gross.equity_curve, initial_cash)),
        "benchmark": _metrics_dict(_compute_metrics(benchmark_curve, initial_cash)),
        "costs": resolved_costs,
        "rebalance": frequency.value,
        "rebalance_days": result.rebalance_days,
        "rebalances": ledger,
        "total_rebalances": result.rebalance_days,
        "rebalances_truncated": ledger_truncated,
        "cost_impact": {
            "total_fees": round(result.total_fees, 2),
            "total_slippage": round(result.total_slippage, 2),
            "total_cost": round(total_cost, 2),
            "turnover": round(result.turnover, 2),
            "cost_pct_of_equity": (
                round(total_cost / net_metrics.final_equity, 6)
                if net_metrics.final_equity > 0
                else None
            ),
        },
        "equity_curve": equity_curve,
        "disclaimer": DISCLAIMER,
    }
