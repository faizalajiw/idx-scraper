"""FastAPI application entrypoint.

Run locally:
    uvicorn idx_scraper.api.app:app --reload --port 8000

This API is READ-ONLY. It does not ingest or mutate data — the APScheduler CLI
worker remains the sole writer. There is currently NO authentication on these
endpoints; they are intended for local/trusted-network use. Before exposing this
service publicly, add authentication/authorization and rate limiting.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from idx_scraper import watchlist_store

from . import alerts, analytics, dividends, quality, services, simulation
from .config import get_settings
from .database import close_pool, get_cursor, init_pool
from .schemas import (
    AlertRuleCreate,
    AlertStatus,
    AlertTestResult,
    BacktestConfig,
    BacktestRequest,
    BacktestResult,
    BrokerActivity,
    BrokerFlow,
    CorpActionRow,
    CorpActionSummary,
    CoverageGaps,
    DividendDetail,
    DividendOverview,
    DividendStock,
    ForeignFlow,
    HoldCheckResponse,
    MarketLeaders,
    MarketNarration,
    MarketOverview,
    MarketRegime,
    QualityOverview,
    QuarantineReason,
    QuarantineRow,
    ScreenerRow,
    SectorAnalysis,
    SectorRotation,
    SectorRRG,
    SentimentResponse,
    SessionMovers,
    Signal,
    SignalTrack,
    SmartMoneyRadar,
    SmartMoneyStock,
    SmartMoneyTrackRecord,
    SmartMoneyWatchList,
    StockBrokerActivity,
    StockBrokerSummary,
    StockDecision,
    StockForeignFlow,
    TechnicalChart,
    ThinDay,
    TopBrokers,
    ValuationResponse,
    WatchlistRow,
    WatchlistUpdate,
)


@asynccontextmanager
def _warm_heavy_caches() -> None:
    """Hitung cache berat sekali saat startup (background thread, fire-and-forget).

    Event study butuh ~2 menit saat dingin — tanpa warm-up, permintaan pertama
    ke Ruang Keputusan/Event Study menggantung sampai selesai hitung. Kalau
    gagal (DB belum siap dsb.), cache terisi otomatis di permintaan berikutnya.
    """
    import threading

    def _run_events() -> None:
        try:
            analytics._event_study_bundle()
            print("[warmup] event bundle siap")
        except Exception as e:
            print(f"[warmup] event bundle dilewati: {e}")

    def _run_broker_snapshot() -> None:
        # Snapshot skor broker (~15s saat dingin) dipakai Hold Check & Ruang
        # Keputusan; hangatkan supaya request pertama tidak menanggung biaya.
        try:
            analytics.broker_activity_snapshot()
            print("[warmup] broker snapshot siap")
        except Exception as e:
            print(f"[warmup] broker snapshot dilewati: {e}")

    threading.Thread(target=_run_events, daemon=True, name="warmup-event-bundle").start()
    threading.Thread(target=_run_broker_snapshot, daemon=True, name="warmup-broker-snapshot").start()


async def lifespan(app: FastAPI):
    init_pool()
    _warm_heavy_caches()
    yield
    close_pool()


app = FastAPI(
    title="IDX Scraper API",
    version="0.1.0",
    description="Read-only market data & analytics for the IDX dashboard.",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _resolve_codes(codes: str | None) -> list[str]:
    if codes:
        return list(dict.fromkeys(c.strip().upper() for c in codes.split(",") if c.strip()))
    return settings.watchlist


def _parse_optional_date(value: str | None, field: str) -> date | None:
    """Parse a YYYY-MM-DD query/body value, or None when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"{field} harus format YYYY-MM-DD"
        ) from None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/market/overview", response_model=MarketOverview)
def market_overview() -> dict:
    return services.get_market_overview()


@app.get("/api/market/regime", response_model=MarketRegime)
def market_regime() -> dict:
    """Regime IHSG (ADX + realized vol) — konteks untuk semua halaman analisis."""
    return analytics.get_market_regime()


@app.get("/api/market/session-movers", response_model=SessionMovers)
def session_movers() -> dict:
    return services.get_session_movers()

@app.get("/api/market/leaders", response_model=MarketLeaders)
def market_leaders(
    metric: str = Query(default="volume", description="volume | value | frequency"),
    limit: int = Query(default=5, ge=1, le=50),
) -> dict:
    """Top emiten by volume/value/frequency — realtime saat jam bursa, else EOD."""
    return services.get_market_leaders(metric, limit)

@app.get("/api/market/top-brokers", response_model=TopBrokers)
def top_brokers(limit: int = Query(default=5, ge=1, le=50)) -> dict:
    """Top broker firm by traded value (EOD GetBrokerSummary)."""
    return services.get_top_brokers(limit)


@app.get("/api/broker-flow", response_model=BrokerFlow)
def broker_flow(
    days: int = Query(default=20, ge=1, le=120, description="Jendela tren riwayat (hari kalender)"),
    top_n: int = Query(default=5, ge=1, le=20, description="Firma teratas per kategori"),
) -> dict:
    """Komposisi nilai transaksi broker per kategori: asing / lokal / BUMN.

    Komposisi turnover, bukan net buy/sell (IDX tidak menyediakan split
    beli/jual per firma). Sumber: research.broker_daily.
    """
    return analytics.get_broker_flow(days=days, top_n=top_n)


@app.get("/api/broker-activity", response_model=BrokerActivity)
def broker_activity(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    """Skor aktivitas broker per emiten (proksi aliran, bobot dari IC) + struktur broker pasar.

    Skor hanya dibentuk dari faktor yang lolos gate IC; kalau tidak ada yang
    lolos, ``validated`` False dan ``rows`` kosong (lihat research.broker_activity).
    """
    return analytics.get_broker_activity(limit)


@app.get("/api/watchlist", response_model=list[WatchlistRow])
def watchlist(codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST")) -> list[dict]:
    return services.get_watchlist(_resolve_codes(codes))


@app.post("/api/watchlist", response_model=list[WatchlistRow])
def add_to_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Add tickers to the .env-backed watchlist and return the updated rows."""
    try:
        codes = watchlist_store.update_watchlist(add=payload.codes, remove=[])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes  # keep the cached settings in sync
    return services.get_watchlist(codes)


@app.put("/api/watchlist", response_model=list[WatchlistRow])
def replace_watchlist(payload: WatchlistUpdate) -> list[dict]:
    """Replace the whole watchlist (persisted to IDX_WATCHLIST in .env)."""
    codes = list(dict.fromkeys(c.strip().upper() for c in payload.codes if c.strip()))
    if not codes:
        raise HTTPException(status_code=422, detail="codes must not be empty")
    try:
        codes = watchlist_store.write_watchlist(codes)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes
    return services.get_watchlist(codes)


@app.delete("/api/watchlist/{code}", response_model=list[WatchlistRow])
def remove_from_watchlist(code: str) -> list[dict]:
    """Remove one ticker from the .env-backed watchlist."""
    symbol = code.strip().upper()
    if symbol not in watchlist_store.read_watchlist():
        raise HTTPException(status_code=404, detail=f"{symbol} is not in the watchlist")
    try:
        codes = watchlist_store.update_watchlist(add=[], remove=[symbol])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Cannot persist watchlist: {e}")
    settings.watchlist = codes
    return services.get_watchlist(codes)


@app.get("/api/hold-check", response_model=HoldCheckResponse)
def hold_check(
    codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"),
    min_days: int = Query(default=40, ge=1, le=500),
) -> dict:
    """Combined technical + valuation "still worth holding" verdict per ticker."""
    resolved = _resolve_codes(codes)
    items = services.get_hold_check(resolved, min_days=min_days)
    from . import analytics

    with get_cursor() as cur:
        date = analytics._latest_eod_date(cur)
    return {"date": date, "items": items,
            "generated_at": datetime.now(timezone(timedelta(hours=7))).isoformat(timespec="seconds")}


@app.get("/api/signals", response_model=list[Signal])
def signals(
    codes: str | None = Query(default=None, description="Comma-separated tickers; defaults to IDX_WATCHLIST"),
    min_days: int = Query(default=25, ge=1, le=500),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    resolved = _resolve_codes(codes)[:limit]
    return services.get_signals(resolved, min_days=min_days)


@app.get("/api/stocks/{code}/history")
def price_history(
    code: str,
    limit: int = Query(default=60, ge=1, le=1000),
) -> list[dict]:
    rows = services.get_price_history(code.upper(), limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No price history for {code.upper()}")
    return rows


@app.get("/api/stocks/{code}/technical", response_model=TechnicalChart)
def technical_chart(code: str) -> dict:
    result = services.get_technical_chart(code.upper())
    if not result["bars"]:
        raise HTTPException(status_code=404, detail=f"No chart data for {code.upper()}")
    return result


# --------------------------------------------------------------- analytics v2


@app.get("/api/stocks/{code}/brokers", response_model=StockBrokerSummary)
def stock_brokers(code: str, date: str | None = Query(default=None)) -> dict:
    return analytics.get_broker_summary(code.upper(), date=date)


@app.get("/api/stocks/{code}/decision", response_model=StockDecision)
def stock_decision(code: str) -> dict:
    """Ruang Keputusan satu emiten: verdict + konteks + level invalidasi.

    Menggabungkan hold-check (verdict dasar), regime IHSG, sentimen aliran,
    jejak asing 10 sesi, base rate event study, dan level pembatalan teknikal
    ke dalam satu respons — semua dari sumber yang sudah ada, cache 30 menit.
    """
    return analytics.get_stock_decision(code.upper())


@app.get("/api/stocks/{code}/broker-activity", response_model=StockBrokerActivity)
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


@app.get("/api/foreign-flow", response_model=ForeignFlow)
def foreign_flow(days: int = Query(default=20, ge=1, le=120)) -> dict:
    return analytics.get_foreign_flow(days=days)


@app.get("/api/stocks/{code}/foreign-flow", response_model=StockForeignFlow)
def stock_foreign_flow(
    code: str,
    days: int = Query(default=30, ge=5, le=120, description="Jendela tren harian emiten (hari bursa)"),
    peer_days: int = Query(default=10, ge=1, le=60, description="Jendela pembanding sektor"),
    peer_limit: int = Query(default=10, ge=3, le=30, description="Maks peer yang ditampilkan"),
) -> dict:
    """Aliran asing satu emiten: tren harian, flip arah, dan peer sektor.

    Net flow dalam rupiah (``foreign_net`` saham * close). Flip = hari tanda
    net berubah vs hari non-nol sebelumnya. Peer sektor dari map kurasi;
    ``comparable`` False menandakan bucket fallback "Lainnya".
    """
    return analytics.get_stock_foreign_flow(code.upper(), days=days, peer_days=peer_days, peer_limit=peer_limit)


@app.get("/api/smart-money/radar", response_model=SmartMoneyRadar)
def smart_money_radar(
    days: int = Query(default=10, ge=2, le=60, description="Jendela agregasi (hari bursa)"),
) -> dict:
    """Radar jejak smart money: emiten dengan aliran asing terkuat di pasar.

    Dua daftar (akumulasi & distribusi) top-N berdasar |net rupiah| di
    jendela N sesi terakhir, plus lantai likuiditas (nilai transaksi jendela
    >= Rp 500 Jt) supaya emiten yang nyaris tak diperdagangkan tidak masuk
    papan. Logika peringkat di modul pure ``smart_money.build_radar``.
    """
    return analytics.get_smart_money_radar(days=days)


@app.get("/api/stocks/{code}/smart-money", response_model=SmartMoneyStock)
def stock_smart_money(code: str) -> dict:
    """Jejak smart money satu emiten: verdict, pola klasik, level, dan narasi.

    Jawaban plain-language atas "pemain besar lagi masuk/keluar sejak kapan
    dan di level mana". Verdict dari porsi nilai transaksi yang dibelani
    asing (bukan skor kuantitatif); level = rentang konsolidasi 20 sesi;
    konteks sektor = berapa emiten se-sektor yang searah. Cache 30 menit.
    """
    return analytics.get_stock_smart_money(code.upper())


@app.get("/api/smart-money/track-record", response_model=SmartMoneyTrackRecord)
def smart_money_track_record() -> dict:
    """Track record pola klasik jejak smart money (120 sesi terakhir).

    Berapa kali tiap pola muncul, dan forward return-nya (entry T+1) plus
    alpha vs pasar — menjawab "pola ini terbukti tidak". Agregasi di modul
    pure ``smart_money``; forward return + alpha dihitung di service
    ``analytics``. Cache 1 jam.
    """
    return analytics.get_smart_money_track_record()


@app.get("/api/smart-money/verdicts", response_model=SmartMoneyWatchList)
def smart_money_verdicts(
    codes: str | None = Query(
        default=None, description="Comma-separated tickers; default = watchlist"
    ),
) -> dict:
    """Verdict jejak smart money per emiten watchlist (kartu halaman Pantau).

    Menampilkan kondisi sekarang (ditimbun / dibuang / seimbang) supaya user
    tahu baseline sebelum alert Telegram menyala saat verdict berubah.
    """
    data = analytics.get_smart_money_watchlist(_resolve_codes(codes))
    return {**data, "telegram_enabled": alerts.telegram_enabled()}


@app.get("/api/sectors/rrg", response_model=SectorRRG)
def sectors_rrg(
    benchmark: str = Query(default="COMPOSITE", description="Benchmark index code from index_quotes"),
    window: int = Query(default=21, ge=10, le=120, description="Trailing weekly window for normalization"),
    tail_weeks: int = Query(default=8, ge=1, le=52, description="Weekly trail points per sector"),
) -> dict:
    """RRG (Relative Rotation Graph) positions per sector vs the benchmark."""
    return analytics.get_sector_rrg(benchmark=benchmark, window=window, tail_weeks=tail_weeks)


@app.get("/api/sectors/rotation", response_model=SectorRotation)
def sectors_rotation(
    lookback: int = Query(default=60, ge=5, le=250, description="Hari bursa riwayat median skor sektor"),
    min_names: int = Query(default=3, ge=2, le=50, description="Minimum emiten berskor per sektor"),
) -> dict:
    """Rotasi sektor dari skor aktivitas broker (proksi aliran dana).

    Beda dari RRG (rotasi harga relatif): ini mengukur sektor mana yang jejak
    akumulasi alirannya sedang naik. Sektor dengan emiten berskor kurang dari
    ``min_names`` dibuang; emiten tanpa sektor sebenarnya tidak diikutkan.
    """
    return analytics.get_sector_rotation(lookback=lookback, min_names=min_names)


@app.get("/api/sectors", response_model=SectorAnalysis)
def sectors(date: str | None = Query(default=None)) -> dict:
    return analytics.get_sector_analysis(date=date)


@app.get("/api/market/narration", response_model=MarketNarration)
def market_narration() -> dict:
    return analytics.get_market_narration()

@app.post("/api/cache/clear")
def cache_clear() -> dict:
    """Kosongkan cache analitik in-process (dipakai serve loop tiap refresh)."""
    return {"cleared": analytics.clear_research_cache()}


@app.get("/api/valuation", response_model=ValuationResponse)
def valuation() -> dict:
    return analytics.get_valuation()


@app.get("/api/screener", response_model=list[ScreenerRow])
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

# --------------------------------------------------------------- data quality

@app.get("/api/quality/overview", response_model=QualityOverview)
def quality_overview() -> dict:
    """Headline health of the research.* layer: coverage, freshness, counts."""
    return quality.get_quality_overview()

@app.get("/api/quality/quarantine", response_model=list[QuarantineRow])
def quality_quarantine(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    """Most recent rows rejected by the quality gate, with reason + raw payload."""
    return quality.get_quarantine(limit=limit)

@app.get("/api/quality/quarantine-reasons", response_model=list[QuarantineReason])
def quality_quarantine_reasons() -> list[dict]:
    """Quarantine counts grouped by rejection reason."""
    return quality.get_quarantine_reasons()

@app.get("/api/quality/coverage-gaps", response_model=CoverageGaps)
def quality_coverage_gaps() -> dict:
    """Weekdays in the covered window with no EOD rows (holidays or scrape misses)."""
    return quality.get_coverage_gaps()

@app.get("/api/quality/thin-days", response_model=list[ThinDay])
def quality_thin_days(
    min_codes: int = Query(default=100, ge=1, le=2000),
    limit: int = Query(default=30, ge=1, le=200),
) -> list[dict]:
    """Trading days with unusually few emiten reported (possible partial scrape)."""
    return quality.get_thin_days(min_codes=min_codes, limit=limit)

@app.get("/api/quality/corp-actions", response_model=CorpActionSummary)
def quality_corp_actions() -> dict:
    """Corporate-action counts broken down by type and source."""
    return quality.get_corp_action_summary()

@app.get("/api/quality/duplicates")
def quality_duplicates() -> dict:
    """Count of (code, trade_date) bars with more than one knowledge_date."""
    return quality.get_duplicate_pit()


# --------------------------------------------------------------- research UI


@app.get("/api/signals/track", response_model=SignalTrack)
def signals_track() -> dict:
    """Track record sinyal BUY/SELL: hit-rate, forward return, abnormal vs pasar.

    Statistik dipecah per jenis sinyal, horizon, dan regime IHSG saat sinyal
    terbentuk — supaya "sinyal ini benar-benar berguna?" terjawab dengan angka,
    bukan opini. Log diisi job harian (`idx signal-log`) atau backfill otomatis.
    """
    return analytics.get_signal_track()


@app.get("/api/sentiment", response_model=SentimentResponse)
def sentiment(limit: int = Query(default=15, ge=1, le=50)) -> dict:
    """Sentimen posisi & aliran dari data tersimpan (arus asing + buku intraday).

    Bukan sentimen berita: skornya dihitung dari jejak transaksi yang sudah kita
    punya, jadi bisa diverifikasi dan tidak butuh sumber baru.
    """
    return analytics.get_sentiment(limit=limit)


@app.get("/api/stocks/{code}/events")
def stock_events(code: str) -> dict:
    """Event study utk satu emiten: riwayat event + baseline pasar (cache 1 jam)."""
    result = analytics.get_stock_events(code.upper())
    if result is None:
        raise HTTPException(status_code=404, detail=f"No event study data for {code.upper()}")
    return result


@app.get("/api/factors/overview")
def factors_overview() -> dict:
    """Kalibrasi faktor: run IC terbaru, bobot aktif, histori rekalibrasi."""
    return analytics.get_factors_overview()


@app.get("/api/market/regime/history")
def regime_history(days: int = Query(default=90, ge=30, le=500)) -> dict:
    """Histori regime harian + agregat (% waktu per regime, transisi)."""
    return analytics.get_regime_history(days=days)


# ------------------------------------------------ dividends & corp actions


@app.get("/api/dividends/overview", response_model=DividendOverview)
def dividends_overview() -> dict:
    """Dividend totals, history by year, recent payouts and the top trailing yields."""
    return dividends.get_overview()


@app.get("/api/dividends/stocks", response_model=list[DividendStock])
def dividends_stocks(
    min_yield: float | None = Query(default=None, ge=0, description="Yield TTM minimum (%)"),
    sort: str = Query(default="yield", pattern="^(yield|cash|recent)$"),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    """Every emiten that has ever paid cash, with its trailing-12-month yield."""
    return dividends.get_stocks(min_yield=min_yield, sort=sort, limit=limit)


@app.get("/api/stocks/{code}/dividends", response_model=DividendDetail)
def stock_dividends(code: str) -> dict:
    """One emiten's cash-dividend history, annual totals and split history."""
    result = dividends.get_stock(code)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Tidak ada data untuk {code.strip().upper()}"
        )
    return result


@app.get("/api/corporate-actions", response_model=list[CorpActionRow])
def corporate_actions(
    code: str | None = Query(default=None, description="Filter satu emiten"),
    action_type: str | None = Query(
        default=None, pattern="^(dividend|split|reverse_split|bonus|rights)$"
    ),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    """Raw corporate-action ledger (splits, reverse splits, dividends), newest first."""
    return dividends.get_corporate_actions(code=code, action_type=action_type, limit=limit)


# ------------------------------------------------------ alerts & backtest


@app.get("/api/alerts", response_model=AlertStatus)
def alerts_status() -> dict:
    """Stored alert rules evaluated against the latest data (no side effects)."""
    return alerts.status()


@app.post("/api/alerts", response_model=AlertStatus)
def alerts_create(payload: AlertRuleCreate) -> dict:
    """Add a watch condition and return the refreshed status."""
    try:
        alerts.create_rule(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return alerts.status()


@app.delete("/api/alerts/{rule_id}", response_model=AlertStatus)
def alerts_delete(rule_id: str) -> dict:
    """Remove a watch condition and return the refreshed status."""
    if not alerts.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"aturan {rule_id} tidak ditemukan")
    return alerts.status()


@app.post("/api/alerts/test", response_model=AlertTestResult)
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


@app.get("/api/backtest/config", response_model=BacktestConfig)
def backtest_config() -> dict:
    """Strategies, cost defaults, and limits for the simulator UI form."""
    return simulation.get_config()


@app.post("/api/backtest/run", response_model=BacktestResult)
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
