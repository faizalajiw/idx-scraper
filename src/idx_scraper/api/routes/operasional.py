"""Rute operasional: alert Telegram, buku klaim, backtest."""

from __future__ import annotations

from fastapi import HTTPException, Query

from .. import alerts, journal, simulation
from ..schemas import (
    AlertRuleCreate,
    AlertStatus,
    AlertTestResult,
    BacktestConfig,
    BacktestRequest,
    BacktestResult,
    JournalClaim,
    JournalGateState,
    JournalHaltRequest,
    JournalResumeRequest,
    JournalSummary,
)
from ._shared import Router, _parse_optional_date, settings

route = Router()


@route.get("/api/alerts", response_model=AlertStatus)
def alerts_status() -> dict:
    """Stored alert rules evaluated against the latest data (no side effects)."""
    return alerts.status()


@route.post("/api/alerts", response_model=AlertStatus)
def alerts_create(payload: AlertRuleCreate) -> dict:
    """Add a watch condition and return the refreshed status."""
    try:
        alerts.create_rule(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return alerts.status()


@route.delete("/api/alerts/{rule_id}", response_model=AlertStatus)
def alerts_delete(rule_id: str) -> dict:
    """Remove a watch condition and return the refreshed status."""
    if not alerts.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"aturan {rule_id} tidak ditemukan")
    return alerts.status()


@route.post("/api/alerts/test", response_model=AlertTestResult)
def alerts_test() -> dict:
    """Send a one-off Telegram message so the user can verify the wiring."""
    if not alerts.telegram_enabled():
        raise HTTPException(
            status_code=409,
            detail="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID belum diisi di .env",
        )
    sent = alerts.send_test_message()
    return {
        "sent": sent,
        "detail": (
            "Pesan uji terkirim — cek chat Telegram kamu."
            if sent
            else "Gagal mengirim pesan uji. Cek token bot dan chat id."
        ),
    }


@route.get("/api/journal/summary", response_model=JournalSummary)
def journal_summary(rule: str | None = None, code: str | None = None) -> dict:
    """Rekaman aturan buku klaim + kalimat rekaman (buku kosong ditulis apa adanya)."""
    return journal.summary(rule=rule, code=code)


@route.get("/api/journal/claims", response_model=list[JournalClaim])
def journal_claims(
    trade_date: str | None = None,
    code: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """Baris buku klaim; tanpa `trade_date` dipakai sesi bursa terakhir.

    Klaim yang belum selesai ikut tampil dengan ``outcome_abn`` kosong: hari yang
    belum teramati bukan hari yang sudah lewat.
    """
    tanggal = _parse_optional_date(trade_date, "trade_date")
    return journal.claims(
        trade_date=tanggal.isoformat() if tanggal else None, code=code, limit=limit
    )


@route.get("/api/journal/state", response_model=JournalGateState)
def journal_state() -> dict:
    """Boleh atau tidak menulis klaim baru sekarang, dengan sebab pertamanya."""
    return journal.state()


@route.post("/api/journal/halt", response_model=JournalGateState)
def journal_halt(payload: JournalHaltRequest) -> dict:
    """Hentikan penulisan klaim baru. Sebab kosong ditolak, bukan disimpan."""
    try:
        return journal.halt(reason=payload.reason, by=payload.by)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@route.post("/api/journal/resume", response_model=JournalGateState)
def journal_resume(payload: JournalResumeRequest) -> dict:
    """Buka lagi penulisan klaim; jejak waktu dan pelakunya tetap tercatat."""
    return journal.resume(by=payload.by)


@route.get("/api/backtest/config", response_model=BacktestConfig)
def backtest_config() -> dict:
    """Strategies, cost defaults, and limits for the simulator UI form."""
    return simulation.get_config()


@route.post("/api/backtest/run", response_model=BacktestResult)
def backtest_run(payload: BacktestRequest) -> dict:
    """Run one point-in-time backtest and return its equity curve + metrics.

    Computation only — nothing is written to the database. The response is a
    simulation over historical data, not a prediction (see `disclaimer`).
    """
    codes = payload.codes or settings.watchlist
    try:
        return simulation.run_backtest(
            strategy_id=payload.strategy,
            codes=codes,
            start=_parse_optional_date(payload.start, "start"),
            end=_parse_optional_date(payload.end, "end"),
            initial_cash=payload.initial_cash,
            params=payload.params,
            costs=payload.costs.model_dump(),
            rebalance=payload.rebalance,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
