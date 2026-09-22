"""Dashboard IDX Stock Analysis - Full Featured."""

from __future__ import annotations

import os
import sqlite3

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from ui import (
    build_signals,
    fmt_price,
    inject_theme,
    live_bar,
    merge_live_bar,
    plotly_theme,
    signal_table,
    snapshot_age,
    toggle_dark,
)

st.set_page_config(
    page_title="IDX Dashboard Pro",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Tema & format (shared) ---
dark = toggle_dark()
inject_theme(dark)

DB_PATH = os.getenv("IDX_SQLITE_PATH", "data/idx.db")


def get_db():
    return sqlite3.connect(DB_PATH)


@st.cache_data(ttl=30)
def load_market_overview():
    db = get_db()

    # IHSG latest — `close` IDX = penutupan sesi sebelumnya, harga live di `current`
    idx_row = db.execute(
        "SELECT close, change, percent, current, captured_at FROM index_quotes "
        "WHERE code='COMPOSITE' ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()

    # Total volume & value (snapshot TERBARU saja, per kode — anti dobel)
    totals = db.execute(
        """SELECT SUM(volume), SUM(value), COUNT(DISTINCT code) FROM stock_quotes q
           WHERE q.captured_at = (SELECT MAX(captured_at) FROM stock_quotes q2
                                  WHERE q2.code = q.code)"""
    ).fetchone()

    # Top gainers/losers dari snapshot TERBARU per kode (unik, tidak double)
    top_gainers = db.execute(
        """SELECT code, close,
                  ROUND(change * 100.0 / NULLIF(previous, 0), 2) as pct
           FROM stock_quotes q
           WHERE q.captured_at = (SELECT MAX(captured_at) FROM stock_quotes q2
                                  WHERE q2.code = q.code)
             AND change IS NOT NULL AND previous > 0 AND change > 0
           ORDER BY pct DESC LIMIT 5"""
    ).fetchall()

    top_losers = db.execute(
        """SELECT code, close,
                  ROUND(change * 100.0 / NULLIF(previous, 0), 2) as pct
           FROM stock_quotes q
           WHERE q.captured_at = (SELECT MAX(captured_at) FROM stock_quotes q2
                                  WHERE q2.code = q.code)
             AND change IS NOT NULL AND previous > 0 AND change < 0
           ORDER BY pct ASC LIMIT 5"""
    ).fetchall()

    db.close()
    return idx_row, totals, top_gainers, top_losers


@st.cache_data(ttl=30)
def load_watchlist_data(codes: list[str]):
    if not codes:
        return pd.DataFrame()

    db = get_db()
    placeholders = ",".join(["?"] * len(codes))

    rows = db.execute(
        f"""SELECT code, close, change,
                   ROUND(change * 100.0 / NULLIF(previous, 0), 2) as pct,
                   volume, foreign_net,
                   (SELECT COUNT(*) FROM stock_summary_daily s
                    WHERE s.code = q.code) as hist_days
            FROM stock_quotes q
            WHERE q.captured_at = (SELECT MAX(captured_at) FROM stock_quotes q2
                                   WHERE q2.code = q.code)
              AND q.code IN ({placeholders})
            ORDER BY pct DESC""",
        codes,
    ).fetchall()

    db.close()

    df = pd.DataFrame(
        rows,
        columns=["Ticker", "Price", "Change", "% Change", "Volume", "Foreign Net", "Hist Days"],
    )
    return df


# --- Header ---
st.markdown('<div class="main-header">📊 IDX Dashboard Pro</div>', unsafe_allow_html=True)

# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ Configuration")

    watchlist_default = "BBCA,BBRI,BMRI,BBNI,BRIS,TLKM,EXCL,ASII,UNVR,ICBP,INDF,PGAS,ADRO,INDY,PTBA,INCO,ANTM,MDKA,HRUM,TINS,KKGI,SCCO,PSAB,MNCN,SIDO,JPFA,CPIN,AKRA,KLBF,MIKA,GOTO,WSKT,PTPP,ADHI,BSDE,SMRA,CTRA,SMGR,INTP,TOWR,DEWA,ERAA,UNTR,KOPI,BRPT,TPIA,TRUK,BBHI,PTIS"
    user_watchlist = st.text_input(
        "Watchlist Tickers",
        value=watchlist_default,
        help="Pisahkan dengan koma",
    )
    codes = [c.strip().upper() for c in user_watchlist.split(",") if c.strip()]
    codes = list(dict.fromkeys(codes))  # dedup, jaga urutan

    st.divider()

    auto_refresh = st.checkbox("Auto Refresh (30s)", value=False)
    # NOTE: auto refresh di-handle di akhir script (sleep + rerun),
    # BUKAN st.rerun() langsung di sini — itu bikin infinite rerun loop.

    st.divider()
    st.info(f"**{len(codes)}** tickers di watchlist")

# --- Market Overview ---
st.subheader("🌐 Market Overview")

idx_row, totals, top_gainers, top_losers = load_market_overview()

col1, col2, col3, col4 = st.columns(4)

if idx_row and idx_row[0]:
    live_price = idx_row[3] if idx_row[3] is not None else idx_row[0]
    delta = f"{idx_row[1]:+,.2f} ({idx_row[2]:+.2f}%)" if idx_row[1] else "N/A"
    age = snapshot_age(idx_row[4])
    col1.metric(
        "IHSG Composite",
        fmt_price(live_price),
        delta,
        help=f"Snapshot: {age}" if age else None,
    )
else:
    col1.metric("IHSG Composite", "N/A", "No data")

col2.metric("Total Volume", f"{totals[0]:,.0f}" if totals[0] else "0")
col3.metric("Total Value", f"Rp {(totals[1] / 1e9):,.2f} M" if totals[1] else "Rp 0")
col4.metric("Active Stocks", f"{totals[2]}" if totals[2] else "0")

# Top movers
st.subheader("🚀 Top Movers")
col1, col2 = st.columns(2)

with col1:
    st.markdown("**Top Gainers** 🟢")
    if top_gainers:
        df_gainers = pd.DataFrame(
            [
                {"Ticker": c, "Price": fmt_price(p), "% Change": f"{pct:+.2f}%"}
                for c, p, pct in top_gainers
            ]
        )
        st.dataframe(df_gainers, width="stretch", hide_index=True)
    else:
        st.write("No data")

with col2:
    st.markdown("**Top Losers** 🔴")
    if top_losers:
        df_losers = pd.DataFrame(
            [
                {"Ticker": c, "Price": fmt_price(p), "% Change": f"{pct:+.2f}%"}
                for c, p, pct in top_losers
            ]
        )
        st.dataframe(df_losers, width="stretch", hide_index=True)
    else:
        st.write("No data")

st.divider()

# --- Watchlist Table ---
st.subheader(f"👀 Watchlist ({len(codes)} saham)")

df_watchlist = load_watchlist_data(codes)

if not df_watchlist.empty:
    # Format harga 4 digit (tanpa desimal) + volume ringkas
    df_watchlist["Price"] = df_watchlist["Price"].map(fmt_price)
    df_watchlist["Change"] = pd.to_numeric(df_watchlist["Change"], errors="coerce").map(
        lambda v: f"{v:+,.0f}" if pd.notna(v) else "—"
    )
    df_watchlist["% Change"] = pd.to_numeric(df_watchlist["% Change"], errors="coerce").map(
        lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"
    )
    df_watchlist["Volume"] = pd.to_numeric(df_watchlist["Volume"], errors="coerce").map(
        lambda v: f"{v / 1e6:,.1f} jt" if pd.notna(v) and v >= 1e6 else (f"{v:,.0f}" if pd.notna(v) else "—")
    )
    df_watchlist["Foreign Net"] = pd.to_numeric(df_watchlist["Foreign Net"], errors="coerce").map(
        lambda v: f"{v / 1e9:,.2f} M" if pd.notna(v) else "—"
    )

    def color_change(val):
        if val is None or pd.isna(val) or val == "—":
            return ""
        try:
            num = float(str(val).replace("%", "").replace(",", "").replace("+", ""))
        except ValueError:
            return ""
        return "color: green" if num > 0 else "color: red" if num < 0 else ""

    styled_df = df_watchlist.style.map(color_change, subset=["Change", "% Change"])
    st.dataframe(styled_df, width="stretch", height=400, hide_index=True)
else:
    st.warning("Belum ada data watchlist. Jalankan `python -m idx_scraper.cli watchlist` dulu.")

st.divider()

# --- Trading Signals ---
st.subheader("🎯 Trading Signals")
st.caption(
    "Metode: rule-based (SMA20 vs SMA50 + RSI-14, BB, MACD) pada data EOD + harga live hari ini. "
    "Bukan model ML — lihat 📖 Methodology di bawah."
)

signals = build_signals(get_db(), codes[:50])  # sampai 50 saham

if signals:
    df_signals = signal_table(signals, with_macd=True)

    buy_count = sum(1 for s in signals if s["signal"] == "BUY")
    sell_count = sum(1 for s in signals if s["signal"] == "SELL")
    hold_count = sum(1 for s in signals if s["signal"] == "HOLD")

    col1, col2, col3 = st.columns(3)
    col1.metric("BUY Signals", f"{buy_count}", delta_color="normal")
    col2.metric("SELL Signals", f"{sell_count}", delta_color="inverse")
    col3.metric("HOLD", f"{hold_count}")

    filter_opt = st.radio(
        "Filter Signal:",
        ["All", "BUY Only", "SELL Only"],
        horizontal=True,
    )

    if filter_opt == "BUY Only":
        df_display = df_signals[df_signals["Signal"].str.contains("BUY")]
    elif filter_opt == "SELL Only":
        df_display = df_signals[df_signals["Signal"].str.contains("SELL")]
    else:
        df_display = df_signals

    def highlight_signal(row):
        if "BUY" in row["Signal"]:
            return ["background-color: rgba(46, 204, 113, 0.15)" for _ in row]
        elif "SELL" in row["Signal"]:
            return ["background-color: rgba(255, 92, 92, 0.15)" for _ in row]
        return ["" for _ in row]

    styled_signals = df_display.style.apply(highlight_signal, axis=1)
    st.dataframe(styled_signals, width="stretch", height=400, hide_index=True)
else:
    st.info("Tidak ada signal tersedia. Pastikan data EOD sudah di-fetch.")

with st.expander("📖 Methodology — cara kerja Trading Signals"):
    st.markdown(
        r"""
**Data**: EOD harian (tabel `stock_summary_daily`) — hari ini dipakai **harga live**
dari snapshot `stock_quotes` bila tersedia (kolom *Sumber*: 🟢 LIVE / EOD).

**Indikator** (dihitung `idx_scraper.analysis.calculate_indicators`):
- **SMA 20 / SMA 50** — arah trend jangka pendek vs menengah
- **RSI (14)** — momentum (0–100; >70 overbought, <30 oversold)
- **Bollinger Bands (20, 2σ)** & **MACD (12, 26, 9)** — volatility & momentum

**Aturan sinyal** (rule-based, bukan ML):
| Sinyal | Syarat |
|---|---|
| 🟢 **BUY** | SMA20 > SMA50 (uptrend) **dan** RSI 50–80 (momentum naik, belum jenuh) |
| 🔴 **SELL** | SMA20 < SMA50 (downtrend) **dan** RSI 20–50 (melemah, belum oversold ekstrem) |
| ⚪ **HOLD** | kondisi campur / data < 25 hari |

Minimal butuh **25 hari** data historis per ticker sebelum sinyal dihitung.
"""
    )

st.divider()

# --- Detailed Chart ---
st.subheader("📈 Technical Analysis Chart")
st.caption(
    "Candlestick EOD + bar live hari ini (gabungan). Indikator: SMA20/50, Bollinger Bands, RSI, MACD."
)

if codes:
    selected_code = st.selectbox("Pilih ticker untuk analisis detail:", codes)

    db = get_db()
    rows = db.execute(
        "SELECT date, open, high, low, close, volume FROM stock_summary_daily "
        "WHERE code=? ORDER BY date",
        (selected_code,),
    ).fetchall()
    bar = live_bar(db, selected_code)
    db.close()

    if rows:
        df_chart = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        df_chart = merge_live_bar(df_chart, bar)
        df_chart["date"] = pd.to_datetime(df_chart["date"])

        from idx_scraper.analysis import calculate_indicators, generate_signal

        df_chart = calculate_indicators(df_chart)
        signal = generate_signal(df_chart)
        last = df_chart.iloc[-1]

        # Metrics row — harga 4 digit
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Current Price", fmt_price(last["close"]))
        col2.metric("RSI (14)", f"{last.get('RSI', 0):.1f}")
        col3.metric("SMA 20", fmt_price(last.get("MA_Short")))
        col4.metric("MACD", f"{last.get('MACD', 0):.2f}")
        col5.metric(
            "Signal",
            signal,
            delta="Bullish" if signal == "BUY" else "Bearish" if signal == "SELL" else "Neutral",
        )

        # Subplot 3 baris: price, RSI, MACD
        fig = make_subplots(
            rows=3,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.03,
            subplot_titles=(f"{selected_code} Price Chart", "RSI (14)", "MACD"),
            row_heights=[0.6, 0.2, 0.2],
        )

        fig.add_trace(
            go.Candlestick(
                x=df_chart["date"],
                open=df_chart["open"],
                high=df_chart["high"],
                low=df_chart["low"],
                close=df_chart["close"],
                name="OHLC",
                increasing_line_color="#26a69a",
                decreasing_line_color="#ef5350",
            ),
            row=1,
            col=1,
        )

        if "MA_Short" in df_chart.columns:
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["MA_Short"],
                           line=dict(color="#ff9800", width=1.5), name="SMA 20"),
                row=1, col=1,
            )

        if "MA_Long" in df_chart.columns:
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["MA_Long"],
                           line=dict(color="#2196f3", width=1.5), name="SMA 50"),
                row=1, col=1,
            )

        if "BB_Upper" in df_chart.columns and "BB_Lower" in df_chart.columns:
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["BB_Upper"],
                           line=dict(color="rgba(158,158,158,0.3)", width=1), name="BB Upper"),
                row=1, col=1,
            )
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["BB_Lower"],
                           line=dict(color="rgba(158,158,158,0.3)", width=1), fill="tonexty",
                           fillcolor="rgba(158,158,158,0.1)", name="BB Lower"),
                row=1, col=1,
            )

        if "RSI" in df_chart.columns:
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["RSI"],
                           line=dict(color="#9c27b0", width=2), name="RSI"),
                row=2, col=1,
            )
            fig.add_hline(y=70, line_dash="dash", line_color="red", opacity=0.5, row=2, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="green", opacity=0.5, row=2, col=1)
            fig.update_yaxes(range=[0, 100], row=2, col=1, title_text="RSI")

        if "MACD" in df_chart.columns and "MACD_Signal" in df_chart.columns:
            hist = df_chart["MACD"] - df_chart["MACD_Signal"]
            fig.add_trace(
                go.Bar(x=df_chart["date"], y=hist,
                       name="Histogram",
                       marker_color=["#26a69a" if v >= 0 else "#ef5350" for v in hist]),
                row=3, col=1,
            )
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["MACD"],
                           line=dict(color="#2196f3", width=1.5), name="MACD"),
                row=3, col=1,
            )
            fig.add_trace(
                go.Scatter(x=df_chart["date"], y=df_chart["MACD_Signal"],
                           line=dict(color="#ff9800", width=1.5), name="Signal"),
                row=3, col=1,
            )

        fig.update_layout(
            **plotly_theme(dark),
            height=800,
            xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_layout(margin=dict(l=50, r=50, t=80, b=50))  # override margin tema
        fig.update_xaxes(gridcolor="rgba(128,138,160,.18)")
        fig.update_yaxes(gridcolor="rgba(128,138,160,.18)")

        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

        # Volume chart — pakai data yang sama (EOD + bar live hari ini)
        st.subheader("📊 Volume Analysis")
        vol_fig = go.Figure()
        vol_fig.add_trace(
            go.Bar(
                x=df_chart["date"],
                y=df_chart["volume"],
                marker_color=[
                    "#26a69a" if c >= o else "#ef5350"
                    for c, o in zip(df_chart["close"], df_chart["open"])
                ],
            )
        )
        vol_fig.update_layout(**plotly_theme(dark), height=220, yaxis_title="Volume")
        vol_fig.update_xaxes(gridcolor="rgba(128,138,160,.18)")
        vol_fig.update_yaxes(gridcolor="rgba(128,138,160,.18)")
        st.plotly_chart(vol_fig, width="stretch", config={"displayModeBar": False})

        with st.expander("📋 Raw Data"):
            st.dataframe(df_chart.tail(30), width="stretch")
    else:
        st.warning(f"Tidak ada data historis untuk {selected_code}. Fetch EOD data terlebih dahulu.")
else:
    st.warning("Tambahkan ticker ke watchlist di sidebar")

# --- Footer ---
st.divider()
st.caption("Data source: IDX Official API + Yahoo Finance | For personal research only")

# --- Auto refresh (rerun script tiap 30s selama aktif) ---
if auto_refresh:
    import time

    time.sleep(30)
    st.rerun()
