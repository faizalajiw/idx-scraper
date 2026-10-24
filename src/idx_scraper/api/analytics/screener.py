"""screener: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from ... import db
from ...analysis import calculate_indicators, generate_signal, signal_series
from ..database import get_cursor
from ._common import _f, _research_cache_get, _research_cache_put, logger
from .broker_activity import broker_scores_by_code
from .market import _frame_from_rows


@dataclass(frozen=True)
class _FilterScreener:
    """Kriteria opsional screener; semua kriteria aktif digabung dengan DAN.

    Satu dataclass murni data — predikatnya dipisah supaya tiap kriteria bisa
    diuji sendiri-sendiri dan ``get_screener`` tinggal menerapkannya.
    """

    signal: str | None = None
    rsi_min: float | None = None
    rsi_max: float | None = None
    min_momentum: float | None = None
    max_momentum: float | None = None
    min_value: float | None = None
    foreign_in_only: bool = False
    min_vol_ratio: float | None = None
    min_broker_score: float | None = None
    min_days: int = 30


def _tidak_di_bawah(nilai: float | None, batas: float | None) -> bool:
    """Batas aktif + nilai tidak diketahui -> selalu gagal, bukan lolos diam-diam."""
    return batas is None or (nilai is not None and nilai >= batas)


def _tidak_di_atas(nilai: float | None, batas: float | None) -> bool:
    return batas is None or (nilai is not None and nilai <= batas)


def _lolos_hari(row: dict[str, Any], f: _FilterScreener) -> bool:
    """``min_days`` hanya memfilter bila di atas bawaan 30 (perilaku lama)."""
    return f.min_days <= 30 or row["hist_days"] >= f.min_days


def _lolos_signal(row: dict[str, Any], f: _FilterScreener) -> bool:
    return not f.signal or row["signal"] == f.signal


def _lolos_rsi_min(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_bawah(row["rsi"], f.rsi_min)


def _lolos_rsi_max(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_atas(row["rsi"], f.rsi_max)


def _lolos_momentum_min(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_bawah(row["momentum_20d"], f.min_momentum)


def _lolos_momentum_max(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_atas(row["momentum_20d"], f.max_momentum)


def _lolos_nilai(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_bawah(row["value"], f.min_value)


def _lolos_asing_masuk(row: dict[str, Any], f: _FilterScreener) -> bool:
    fnet = row["foreign_net"]
    return not f.foreign_in_only or (fnet is not None and fnet > 0)


def _lolos_vol_ratio(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_bawah(row["vol_ratio"], f.min_vol_ratio)


def _lolos_broker(row: dict[str, Any], f: _FilterScreener) -> bool:
    return _tidak_di_bawah(row["broker_score"], f.min_broker_score)


#: Urutan kriteria bebas — semuanya AND — tetapi daftarnya eksplisit supaya
#: filter baru wajib masuk ke tabel ini, bukan terselip di fungsi utama.
_KRITERIA_SCREENER = (
    _lolos_hari,
    _lolos_signal,
    _lolos_rsi_min,
    _lolos_rsi_max,
    _lolos_momentum_min,
    _lolos_momentum_max,
    _lolos_nilai,
    _lolos_asing_masuk,
    _lolos_vol_ratio,
    _lolos_broker,
)


def _lolos_screener(row: dict[str, Any], f: _FilterScreener) -> bool:
    return all(kriteria(row, f) for kriteria in _KRITERIA_SCREENER)


def get_screener(
    signal: str | None = None,
    rsi_min: float | None = None,
    rsi_max: float | None = None,
    min_momentum: float | None = None,
    max_momentum: float | None = None,
    min_value: float | None = None,
    foreign_in_only: bool = False,
    min_vol_ratio: float | None = None,
    min_broker_score: float | None = None,
    min_days: int = 30,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Rule-based multi-factor screener ("AI screening" v1: transparent rules).

    Factors per stock: technical signal (SMA20/50+RSI rule), 20-day momentum,
    RSI, volume ratio (today vs 20d avg — detects unusual activity), foreign
    net, liquidity (traded value), order-book flow (ob_imbalance / ob_absorption
    dari snapshot intraday), plus skor aktivitas broker (proksi aliran, bobot
    dari IC bulanan). All filters are optional; combining them is the "strategy".

    ``min_broker_score`` hanya bermakna kalau skor broker sudah tervalidasi. Kalau
    belum, tidak ada emiten yang bisa dinyatakan lolos kriteria yang tidak bisa
    dihitung -> hasilnya kosong, bukan filter yang diam-diam diabaikan.

    Baris lengkap (tanpa filter) di-cache; filter hanya menyeleksi baris, jadi
    bisa diterapkan ke cache tanpa mengubah hasil.
    """
    spec = _FilterScreener(
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
    )
    return [row for row in _screener_rows() if _lolos_screener(row, spec)][:limit]


def search_stocks(q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Cari emiten by kode atau nama — dipakai command palette frontend.

    Query nama langsung dari research.latest_pit (bukan lewat _screener_rows):
    cukup kode + nama, tanpa menghitung indikator tiap emiten, jadi murah dan
    aman dipanggil saat user mengetik. Urutan: kecocokan awalan kode dulu,
    baru kecocokan substring nama.
    """
    needle = q.strip()
    if not needle:
        return []
    with get_cursor() as cur:
        cur.execute(
            """with latest as (
                 select distinct on (code) code, name, trade_date
                 from research.latest_pit
                 order by code, trade_date desc
               )
               select code, name from latest
               where upper(code) like %(prefix)s
                  or coalesce(name, '') ilike %(substr)s
               order by
                 case when upper(code) like %(prefix)s then 0 else 1 end,
                 length(code), code
               limit %(limit)s""",
            {
                "prefix": f"{needle.upper()}%",
                "substr": f"%{needle}%",
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def _screener_rows() -> list[dict[str, Any]]:
    """Baris screener penuh (semua emiten, semua metrik) — cache 10 menit.

    Tanpa cache ini tiap permintaan screener memuat ulang dan menghitung indikator
    setiap emiten (~15-20s). Data hanya berubah tiap EOD, dan serve loop memanggil
    /api/cache/clear setiap pipeline refresh.
    """
    cached = _research_cache_get("screener_rows")
    if cached is not None:
        return cached

    ob = _latest_orderbook_factors()
    # Skor broker diambil di luar blok cursor: snapshot-nya membuka koneksi
    # sendiri untuk query panel faktor, jadi jangan tahan koneksi pool menunggu.
    broker_by_code, _broker_validated = broker_scores_by_code()
    # Satu query bulk untuk semua emiten (dulu: ~3 query per emiten, N+1).
    # Diurutkan per kode lalu tanggal, jadi baris terakhir tiap kode = data terbaru.
    with get_cursor() as cur:
        cur.execute(
            """select code, name, trade_date, close, high, low, volume, value, foreign_net
               from research.latest_pit
               order by code, trade_date"""
        )
        all_rows = cur.fetchall()
    history: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        history.setdefault(r["code"], []).append(r)

    out: list[dict[str, Any]] = []
    for code, hist in history.items():
        if len(hist) < 30:
            continue
        latest = hist[-1]
        df = _frame_from_rows(hist)
        df = calculate_indicators(df.assign(open=df["close"], high=df["close"], low=df["close"]))
        sig = generate_signal(df)
        close = df["close"]
        momo = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
        rsi = _f(df["RSI"].iloc[-1])
        vol = df["volume"].fillna(0)
        vol_ratio = float(vol.iloc[-1] / vol.tail(20).mean()) if vol.tail(20).mean() > 0 else None

        # --- metrik riset tambahan (ATR%, 52w high, hari sejak sinyal) ---
        # ATR(14) Wilder-style rolling, % dari close (volatilitas komparabel
        # antar emiten). High/low NULL (hari non-trading) -> diabaikan.
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = tr.rolling(14).mean()
        atr_pct = (
            float(atr.iloc[-1] / close.iloc[-1] * 100)
            if pd.notna(atr.iloc[-1]) and close.iloc[-1] > 0
            else None
        )
        # Jarak dari puncak 52 minggu (<= 0; min_periods 63 agar emiten baru
        # tidak selalu -100%).
        hi_252 = close.rolling(252, min_periods=63).max()
        dist_52w = (
            float(close.iloc[-1] / hi_252.iloc[-1] - 1.0) if pd.notna(hi_252.iloc[-1]) else None
        )
        # Hari sejak sinyal BUY/SELL terakhir (rule = signal_series, sama
        # dengan generate_signal). None = belum pernah bersinyal non-HOLD.
        sigs = signal_series(df)
        non_hold = sigs[sigs != "HOLD"]
        days_since_signal = int(len(sigs) - 1 - non_hold.index[-1]) if len(non_hold) else None

        row = {
            "code": code,
            "name": latest["name"],
            "close": _f(latest["close"]),
            "percent": None,
            "rsi": rsi,
            "signal": sig,
            "trend_up": bool((df["MA_Short"].iloc[-1] or 0) > (df["MA_Long"].iloc[-1] or 0)),
            "momentum_20d": round(momo, 2) if momo is not None else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
            "dist_52w": round(dist_52w * 100, 2) if dist_52w is not None else None,
            "days_since_signal": days_since_signal,
            "ob_imbalance": ob.get(code, {}).get("ob_imbalance"),
            "ob_absorption": ob.get(code, {}).get("ob_absorption"),
            "broker_score": (broker_by_code.get(code) or {}).get("score"),
            "foreign_net": _f(latest["foreign_net"]),
            "value": _f(latest["value"]),
            "hist_days": len(df),
        }
        out.append(row)

    out.sort(
        key=lambda r: r["momentum_20d"] if r["momentum_20d"] is not None else -999, reverse=True
    )
    _research_cache_put("screener_rows", out, ttl=600.0)
    return out


def _latest_orderbook_factors() -> dict[str, dict[str, float | None]]:
    """Faktor order-book terakhir per emiten (cache 30 menit).

    Dihitung dari snapshot intraday via research.orderbook; None bila emiten
    tidak tercakup capture / snapshot terlalu tipis / sumber bermasalah —
    screener tetap jalan tanpa kolom ini.
    """
    cached = _research_cache_get("ob_latest")
    if cached is not None:
        return cached
    out: dict[str, dict[str, float | None]] = {}
    dsn = db.dsn() if db.tersedia() else None
    if dsn:
        try:
            from ...research.orderbook import compute_daily, load_snapshots

            ob_daily = compute_daily(load_snapshots(dsn))
            for r in ob_daily.itertuples():
                # groupby mengurutkan (code, date) asc -> okrurrence terakhir
                # per kode = tanggal terbaru.
                out[r.code] = {
                    "ob_imbalance": None
                    if pd.isna(r.ob_imbalance)
                    else round(float(r.ob_imbalance), 4),
                    "ob_absorption": None
                    if pd.isna(r.ob_absorption)
                    else round(float(r.ob_absorption), 4),
                    "ob_date": str(pd.Timestamp(r.date).date()),
                }
        except Exception as e:  # graceful degradation — screener tetap hidup
            logger.warning(f"[warn] faktor order-book dilewati: {e}")
    _research_cache_put("ob_latest", out)
    return out


def _event_study_bundle() -> dict[str, Any]:
    """Jalankan event study semua preset sekali, cache 1 jam, share antar kode."""
    cached = _research_cache_get("event_bundle")
    if cached is not None:
        return cached

    from ...research import events as ev

    dsn = db.dsn()

    # Panel adjusted utk return emiten; market_daily (raw close rata-rata
    # pasar) utk konteks kurva event-time.
    panel = ev.load_panel_from_db(dsn)
    with get_cursor() as cur:
        cur.execute(
            """select trade_date as date, avg(close) as close
               from research.latest_pit group by trade_date order by trade_date"""
        )
        md = pd.Series(
            {pd.Timestamp(r["date"]): _f(r["close"]) for r in cur.fetchall()},
            dtype=float,
        ).dropna()
    ev.set_market_daily(md)

    bundle: dict[str, Any] = {"events": {}, "panel": panel}
    for name in sorted(ev.EVENT_PRESETS):
        bundle["events"][name] = ev.run_event_study(panel, name, horizon=21, min_gap=10)
    # Hitungannya mahal (semua preset × seluruh panel, ~2 menit saat dingin)
    # dan datanya harian — TTL 6 jam, bukan 1 jam default.
    _research_cache_put("event_bundle", bundle, ttl=6 * 3600.0)
    return bundle


def get_stock_events(code: str) -> dict[str, Any] | None:
    """Statistik event study utk satu emiten: riwayat + agregat pasar.

    Returns None bila emiten tidak ditemukan di panel (kode salah / terlalu
    pendek histori).
    """
    from ...research import events as ev  # ev dipakai utk PRESETS di bawah

    code = code.strip().upper()
    bundle = _event_study_bundle()
    panel: pd.DataFrame = bundle["panel"]
    if panel.empty or code not in set(panel["code"]):
        return None

    out_events: list[dict[str, Any]] = []
    for name, res in bundle["events"].items():
        ev_rows = res.events
        mine = ev_rows[ev_rows["code"] == code] if not ev_rows.empty else pd.DataFrame()
        out_events.append(
            {
                "event": name,
                "description": ev.EVENT_PRESETS[name]["description"],
                "my_count": len(mine),
                "my_last_date": (
                    str(pd.Timestamp(mine["date"].max()).date()) if not mine.empty else None
                ),
                "my_median_fwd": (float(mine["fwd"].median()) if not mine.empty else None),
                "my_median_abnormal": (
                    float(mine["abnormal"].median()) if not mine.empty else None
                ),
                # baseline pasar (semua emiten, semua event)
                "market_count": int(res.n_events),
                "market_hit_rate": res.hit_rate,
                "market_median_fwd": res.median_fwd,
                "market_mean_abnormal": res.mean_abnormal,
            }
        )
    last_date = str(pd.Timestamp(panel["date"].max()).date())
    return {"code": code, "as_of": last_date, "events": out_events}
