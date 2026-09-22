"""Shared UI helpers untuk dashboard IDX (app.py & app_pro.py).

Berisi:
- toggle_dark / inject_theme : tema dark-light modern (CSS variables)
- fmt_price / fmt_pct / fmt_int / arrow : format angka konsisten
  (harga 4 digit: IHSG 6,315 · BBCA 6,300 — tanpa desimal)
- latest_rows : baris TERBARU per kode (anti data dobel antar snapshot)
- live_bar / merge_live_bar : gabungkan bar live hari ini ke deret EOD
- build_signals / signal_table : hitung & format trading signals
- snapshot_age : umur snapshot untuk badge freshness
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

DARK_KEY = "ui_dark"
WIB = timezone(timedelta(hours=7))
FMT_NONE = "—"
SIG_BADGE = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "⚪ HOLD"}


def _today() -> str:
    return datetime.now(WIB).strftime("%Y-%m-%d")


def _norm_date(value: str) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD'; selain itu dilewati apa adanya."""
    s = str(value)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


# --------------------------------------------------------------- format


def fmt_price(v, digits: int = 0) -> str:
    """Harga tanpa desimal: IHSG 6384.726 -> '6,385' | BBCA 6300 -> '6,300'."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return FMT_NONE
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return FMT_NONE


def fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return FMT_NONE
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return FMT_NONE


def fmt_int(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return FMT_NONE
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return FMT_NONE


def arrow(v) -> str:
    """Panah arah untuk sel tabel."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return "▲" if f > 0 else "▼" if f < 0 else "•"


# --------------------------------------------------------------- theming


def toggle_dark() -> bool:
    """Toggle dark/light di sidebar. Default: dark."""
    return st.toggle("🌙 Dark mode", value=True, key=DARK_KEY)


def inject_theme(dark: bool) -> None:
    st.markdown(f"<style>{_css(dark)}</style>", unsafe_allow_html=True)


def _css(dark: bool) -> str:
    if dark:
        palette = (
            "--bg:#0e1117;--panel:#151b26;--panel2:#1b2331;--text:#e8ecf2;--muted:#93a0b4;"
            "--border:#27304a;--accent:#5b9bff;--green:#2ecc71;--red:#ff5c5c;--amber:#f5a524;"
            "--shadow:0 2px 8px rgba(0,0,0,.35);"
        )
    else:
        palette = (
            "--bg:#f4f6fa;--panel:#ffffff;--panel2:#eef1f6;--text:#131a24;--muted:#5d6b80;"
            "--border:#e1e7ef;--accent:#2563eb;--green:#16a34a;--red:#dc2626;--amber:#d97706;"
            "--shadow:0 1px 3px rgba(16,24,40,.07);"
        )
    return f"""
    :root {{ {palette} }}
    html, body, [data-testid="stAppViewContainer"],
    section.stMain div.block-container {{ background: var(--bg); }}
    [data-testid="stHeader"] {{ background: transparent; }}
    h1, h2, h3, h4, label, p, span, li {{ color: var(--text); }}
    [data-testid="stMetric"] {{
      background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
      padding: 14px 18px; box-shadow: var(--shadow);
    }}
    [data-testid="stMetricLabel"] p {{ color: var(--muted); font-weight: 600; }}
    [data-testid="stMetricValue"] {{ color: var(--text); font-variant-numeric: tabular-nums; }}
    [data-testid="stDataFrame"], div[data-testid="stExpander"] {{
      border: 1px solid var(--border); border-radius: 12px; background: var(--panel);
    }}
    div[data-testid="stExpander"] details {{ border: none; background: transparent; }}
    hr {{ border-color: var(--border); }}
    a {{ color: var(--accent); }}
    div[data-testid="stSidebar"] {{
      background: var(--panel); border-right: 1px solid var(--border);
    }}
    /* badge sinyal (kalau mau dipakai di markdown) */
    .sig-badge {{
      display: inline-block; padding: 2px 10px; border-radius: 999px;
      font-weight: 700; font-size: .85rem;
    }}
    .sig-buy  {{ background: color-mix(in srgb, var(--green) 18%, transparent); color: var(--green); }}
    .sig-sell {{ background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }}
    .sig-hold {{ background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber); }}
    """


def plotly_theme(dark: bool) -> dict:
    """Warna dasar plotly mengikuti tema."""
    return dict(
        template="plotly_dark" if dark else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e8ecf2" if dark else "#131a24"),
        colorway=["#5b9bff", "#f5a524", "#2ecc71", "#ff5c5c", "#ab7dff"],
        margin=dict(l=40, r=30, t=40, b=30),
    )


def style_fig(fig, dark: bool, height: int = 420) -> None:
    """Terapkan tema + grid halus ke figure plotly."""
    fig.update_layout(**plotly_theme(dark), height=height)
    fig.update_xaxes(gridcolor="rgba(128,138,160,.18)")
    fig.update_yaxes(gridcolor="rgba(128,138,160,.18)")


# --------------------------------------------------------------- data shaping

# {table}/{cols} diisi internal (bukan input user) — bukan SQL injection.
_SQL_LATEST_PER_CODE = """
    select {cols}
    from {table} q
    where q.captured_at = (select max(captured_at) from {table} q2 where q2.code = q.code)
"""


def latest_rows(db: sqlite3.Connection, table: str, cols: str) -> list[tuple]:
    """Baris TERBARU per kode — snapshot lama tidak ikut (anti data dobel)."""
    return db.execute(_SQL_LATEST_PER_CODE.format(table=table, cols=cols)).fetchall()


def live_bar(db: sqlite3.Connection, code: str) -> dict | None:
    """Snapshot live HARI INI untuk satu kode; None kalau tidak ada."""
    rows = latest_rows(
        db, "stock_quotes", "code, open, high, low, close, volume, captured_at"
    )
    for r in rows:
        if r[0] == code and str(r[6])[:10] == _today() and r[4] is not None:
            return {
                "date": _today(),
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5] or 0,
            }
    return None


def merge_live_bar(df: pd.DataFrame, live: dict | None) -> pd.DataFrame:
    """Gabungkan bar live (hari ini) ke deret EOD; bar live menimpa tanggal sama."""
    if not live:
        return df
    d = pd.to_datetime(live["date"])
    df = df.copy()
    if "date" in df.columns and len(df):
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] != d]
    row = pd.DataFrame([{
        "date": d,
        "open": live["open"],
        "high": live["high"],
        "low": live["low"],
        "close": live["close"],
        "volume": live["volume"],
    }])
    return (
        pd.concat([df, row], ignore_index=True)
        .sort_values("date")
        .reset_index(drop=True)
    )


# --------------------------------------------------------------- signals


def build_signals(db: sqlite3.Connection, codes: list[str], min_days: int = 25) -> list[dict]:
    """Hitung signal per ticker: EOD + bar live hari ini (flag live=True).

    Metode (rule-based, bukan ML): trend SMA20 vs SMA50 + filter RSI(14),
    indikator dihitung oleh idx_scraper.analysis.calculate_indicators.
    """
    from idx_scraper.analysis import calculate_indicators, generate_signal

    out: list[dict] = []
    for code in codes:
        # EOD IDX dulu, gap diisi Yahoo (stock_daily) — sama dengan load_price_history
        by_date: dict[str, tuple] = {}
        for r in db.execute(
            "SELECT date, open, high, low, close, volume FROM stock_summary_daily "
            "WHERE code=? ORDER BY date",
            (code,),
        ).fetchall():
            by_date[_norm_date(r[0])] = r
        for r in db.execute(
            "SELECT date, open, high, low, close, volume FROM stock_daily "
            "WHERE ticker=? ORDER BY date",
            (code,),
        ).fetchall():
            by_date.setdefault(_norm_date(r[0]), r)
        if not by_date:
            continue
        rows = sorted(by_date.values(), key=lambda r: r[0])
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        bar = live_bar(db, code)
        is_live = bar is not None
        df = merge_live_bar(df, bar)
        if len(df) < min_days:
            continue
        df = calculate_indicators(df)
        signal = generate_signal(df)
        last, prev = df.iloc[-1], df.iloc[-2]
        pct = None
        if prev["close"]:
            pct = (last["close"] - prev["close"]) / prev["close"] * 100

        def _f(v):
            try:
                return float(v) if v is not None and pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        out.append({
            "code": code,
            "signal": signal,
            "close": last["close"],
            "pct": pct,
            "rsi": _f(last.get("RSI")),
            "sma20": _f(last.get("MA_Short")),
            "macd": _f(last.get("MACD")),
            "trend_up": bool((last.get("MA_Short") or 0) > (last.get("MA_Long") or 0)),
            "live": is_live,
        })
    return out


def signal_table(signals: list[dict], with_macd: bool = False) -> pd.DataFrame:
    """Format signals jadi tabel display (harga 4 digit, badge, flag LIVE)."""
    rows = []
    for s in signals:
        r = {
            "Ticker": s["code"],
            "Signal": SIG_BADGE.get(s["signal"], s["signal"]),
            "Harga": fmt_price(s["close"]),
            "Δ%": fmt_pct(s["pct"]),
            "RSI": f"{s['rsi']:.1f}" if s["rsi"] is not None else FMT_NONE,
            "SMA20": fmt_price(s["sma20"]),
            "Trend": "↑ Bullish" if s["trend_up"] else "↓ Bearish",
            "Sumber": "🟢 LIVE" if s["live"] else "EOD",
        }
        if with_macd:
            r["MACD"] = f"{s['macd']:.2f}" if s["macd"] is not None else FMT_NONE
        rows.append(r)
    return pd.DataFrame(rows)


# --------------------------------------------------------------- misc


def snapshot_age(ts: str | None) -> str | None:
    """'baru saja' / '12 mnt lalu' dari timestamp ISO snapshot."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WIB)
    secs = (datetime.now(WIB) - dt).total_seconds()
    if secs < 90:
        return "baru saja"
    m = int(secs // 60)
    if m < 60:
        return f"{m} mnt lalu"
    return f"{m // 60} jam {m % 60} mnt lalu"
