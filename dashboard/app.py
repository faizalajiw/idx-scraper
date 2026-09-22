"""Dashboard untuk analisis saham IDX (Streamlit)."""

from __future__ import annotations

import os
import sqlite3

import dotenv

dotenv.load_dotenv()

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from ui import (
    build_signals,
    fmt_price,
    inject_theme,
    live_bar,
    merge_live_bar,
    plotly_theme,
    signal_table,
    snapshot_age,
    style_fig,
    toggle_dark,
)

from idx_scraper.analysis import calculate_indicators, generate_signal

st.set_page_config(page_title="IDX Dashboard", layout="wide")

dark = toggle_dark()
inject_theme(dark)

st.title("📊 IDX Dashboard - Analisa Saham")

DB_PATH = os.getenv("IDX_SQLITE_PATH", "data/idx.db")


def get_db():
    return sqlite3.connect(DB_PATH)


# --- Sidebar ---
with st.sidebar:
    st.header("⚙️ Settings")
    watchlist_default = "BBCA,BBRI,BMRI,BBNI,BRIS,TLKM,EXCL,ASII,UNVR,ICBP,INDF,PGAS,ADRO,INDY,PTBA,INCO,ANTM,MDKA,HRUM,TINS,KKGI,SCCO,PSAB,MNCN,SIDO,JPFA,CPIN,AKRA,KLBF,MIKA,GOTO,WSKT,PTPP,ADHI,BSDE,SMRA,CTRA,SMGR,INTP,TOWR,DEWA,ERAA,UNTR,KOPI,BRPT,TPIA,TRUK,BBHI,PTIS"
    user_watchlist = st.text_input("Watchlist (pisahkan koma)", value=watchlist_default)
    codes = [c.strip().upper() for c in user_watchlist.split(",") if c.strip()]
    codes = list(dict.fromkeys(codes))  # dedup, jaga urutan
    auto_refresh = st.checkbox("Auto Refresh (30 detik)", value=False)

# --- Market Overview ---
st.header("📈 Market Overview")

col1, col2, col3 = st.columns(3)

db = get_db()

# IHSG
# NOTE: kolom `close` di GetIndexList IDX = penutupan SESI SEBELUMNYA;
# harga live intraday ada di kolom `current`. Fallback ke close utk data lama.
idx_row = db.execute(
    "SELECT close, change, percent, current, captured_at FROM index_quotes "
    "WHERE code='COMPOSITE' ORDER BY captured_at DESC LIMIT 1"
).fetchone()

if idx_row and idx_row[0] is not None:
    live_price = idx_row[3] if idx_row[3] is not None else idx_row[0]
    delta = f"{idx_row[1]:+,.2f}" if idx_row[1] is not None else None
    if idx_row[2] is not None:
        delta = f"{delta} ({idx_row[2]:+.2f}%)" if delta else f"{idx_row[2]:+.2f}%"
    age = snapshot_age(idx_row[4])
    col1.metric(
        "IHSG (COMPOSITE)",
        fmt_price(live_price),
        delta,
        help=f"Snapshot: {age}" if age else None,
    )
else:
    col1.metric("IHSG (COMPOSITE)", "N/A")

# Total Volume & Value (snapshot TERBARU per kode — anti dobel)
try:
    totals = db.execute(
        """SELECT SUM(volume), SUM(value) FROM stock_quotes q
           WHERE q.captured_at = (SELECT MAX(captured_at) FROM stock_quotes q2
                                  WHERE q2.code = q.code)"""
    ).fetchone()
    col2.metric("Total Volume", f"{totals[0]:,.0f}" if totals and totals[0] else "0")
    col3.metric("Total Value", f"Rp {totals[1]:,.0f}" if totals and totals[1] else "Rp 0")
except sqlite3.Error:
    col2.metric("Total Volume", "N/A")
    col3.metric("Total Value", "N/A")

db.close()

# --- Watchlist Real-time ---
st.header(f"👀 Watchlist ({len(codes)} saham)")

if codes:
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Watchlist Table")
        try:
            db = get_db()
            placeholders = ",".join(["?"] * len(codes))
            rows = db.execute(
                f"""SELECT q.code, q.close, q.change,
                           ROUND(q.change * 100.0 / NULLIF(q.previous, 0), 2) as pct,
                           q.volume, q.bid, q.offer,
                           (SELECT COUNT(*) FROM stock_summary_daily s
                            WHERE s.code = q.code) as hist_days
                    FROM stock_quotes q
                    WHERE q.captured_at = (
                        SELECT MAX(captured_at) FROM stock_quotes q2 WHERE q2.code = q.code
                    )
                    AND q.code IN ({placeholders})
                    ORDER BY pct DESC""",
                codes,
            ).fetchall()

            df = pd.DataFrame(
                rows,
                columns=["Ticker", "Price", "Change", "% Change", "Volume", "Bid", "Offer", "Hist Days"],
            )
            # Format harga 4 digit (tanpa desimal)
            for col in ("Price", "Bid", "Offer"):
                df[col] = df[col].map(fmt_price)
            df["Change"] = pd.to_numeric(df["Change"], errors="coerce").map(
                lambda v: f"{v:+,.0f}" if pd.notna(v) else "—"
            )
            df["% Change"] = pd.to_numeric(df["% Change"], errors="coerce").map(
                lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"
            )
            st.dataframe(df, width="stretch", height=400, hide_index=True)
            db.close()
        except Exception as e:
            st.write(f"Data tidak tersedia: {e}")

    with col2:
        st.subheader("📊 Trading Signals")
        try:
            signals = build_signals(get_db(), codes)  # semua kode (sampai 50)
            df_sig = signal_table(signals)
            non_hold = df_sig[df_sig["Signal"].str.contains("BUY|SELL")]
            if not non_hold.empty:
                st.dataframe(non_hold, width="stretch", hide_index=True)
            elif not df_sig.empty:
                st.info("Semua HOLD saat ini")
            else:
                st.info("Butuh minimal 25 hari data EOD. Jalankan `idx eod` / `idx seed` dulu.")
        except Exception as e:
            st.write(f"Error: {e}")

else:
    st.warning("Masukkan ticker di sidebar")

# --- Chart View ---
st.header("📈 Detail Chart & Analysis")

if codes:
    selected = st.selectbox("Pilih saham untuk dilihat", codes)
    try:
        db = get_db()
        rows = db.execute(
            "SELECT date, open, high, low, close, volume FROM stock_summary_daily "
            "WHERE code=? ORDER BY date",
            (selected,),
        ).fetchall()
        bar = live_bar(db, selected)
        db.close()

        if rows:
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
            df = merge_live_bar(df, bar)  # bar live hari ini (kalau ada)
            df["date"] = pd.to_datetime(df["date"])

            df = calculate_indicators(df)
            signal = generate_signal(df)
            last = df.iloc[-1]
            rsi = last.get("RSI")

            # Signal badge — harga 4 digit
            col1, col2, col3 = st.columns(3)
            col1.metric("Ticker", selected)
            col2.metric("Latest Price", fmt_price(last["close"]))
            col3.metric("Signal", signal)

            # Main price chart with SMA
            fig = go.Figure()
            fig.add_trace(
                go.Candlestick(
                    x=df["date"],
                    open=df["open"],
                    high=df["high"],
                    low=df["low"],
                    close=df["close"],
                    name="OHLC",
                )
            )
            fig.add_trace(
                go.Scatter(x=df["date"], y=df["MA_Short"], line=dict(color="red", width=1), name="SMA20")
            )
            fig.add_trace(
                go.Scatter(x=df["date"], y=df["MA_Long"], line=dict(color="blue", width=1), name="SMA50")
            )
            fig.update_layout(
                title=f"{selected} - OHLC dengan SMA20/SMA50",
                yaxis_title="Harga",
                **plotly_theme(dark),
                height=500,
                xaxis_rangeslider_visible=False,
            )
            style_fig(fig, dark, height=500)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

            # Technical indicators
            st.subheader("Indikator Teknis")
            col1, col2 = st.columns(2)
            with col1:
                st.metric("RSI(14)", f"{float(rsi):.2f}" if pd.notna(rsi) else "—")
                st.metric("SMA20", fmt_price(last.get("MA_Short")))
            with col2:
                st.metric("BB Upper", fmt_price(last.get("BB_Upper")))
                st.metric("BB Lower", fmt_price(last.get("BB_Lower")))
        else:
            st.warning("Data historis belum tersedia")
    except Exception as e:
        st.error(f"Error: {e}")
else:
    st.warning("Pilih ticker untuk lihat detail")

# --- Auto refresh (rerun the script every 30s while enabled) ---
if auto_refresh:
    import time

    time.sleep(30)
    st.rerun()
