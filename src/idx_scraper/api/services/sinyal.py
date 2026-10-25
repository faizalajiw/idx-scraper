"""Service layer — sinyal teknikal, hold-check, dan data grafik teknikal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ...analysis import calculate_indicators, generate_signal, is_mechanical_sell
from ..database import get_cursor
from .inti import WIB, _f, _norm_date, logger
from .pasar import _live_bar, _load_ohlc_by_date, _merge_live_bar

# --------------------------------------------------------------- signals


def _build_frame(cur: Any, code: str) -> tuple[pd.DataFrame, bool]:
    by_date = _load_ohlc_by_date(cur, code)
    if not by_date:
        return pd.DataFrame(), False
    rows = sorted(by_date.values(), key=lambda r: _norm_date(r[0]))
    df = pd.DataFrame(
        [(_norm_date(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    # Numeric coercion (psycopg returns Decimal for numeric columns).
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    bar = _live_bar(cur, code)
    if bar is not None:
        bar = {
            k: (float(v) if v is not None else None) if k != "date" else v for k, v in bar.items()
        }
    is_live = bar is not None
    df = _merge_live_bar(df, bar)
    return df, is_live


def _zscore_context(df: pd.DataFrame) -> dict[str, Any]:
    """60-day mean-reversion context: z-score of last close vs 60d mean/std.

    Mirrors the definition used by analytics.get_valuation so both screens
    agree on what "stretched vs own band" means.
    """
    close = df["close"]
    window = close.tail(60)
    mean = window.mean()
    std = window.std()
    if not std or pd.isna(std) or mean <= 0:
        return {"z": None, "mean": None}
    z = float((close.iloc[-1] - mean) / std)
    return {"z": round(z, 2), "mean": round(float(mean), 2)}


def _hold_layer_broker() -> tuple[dict[str, dict[str, Any]], bool]:
    """Skor aktivitas broker per kode; gagal -> lapisan nonaktif.

    Dihitung sebelum blok cursor: snapshot-nya membuka koneksi sendiri untuk
    query panel faktor yang berat, jadi jangan tahan koneksi pool sambil
    menunggu. Gagal atau belum tervalidasi -> lapisan tidak aktif (adj 0) dan
    hold-check berperilaku persis seperti sebelum fitur ini ada.
    """
    from ..analytics import broker_scores_by_code

    try:
        return broker_scores_by_code()
    except Exception as e:
        logger.warning(f"[warn] skor aktivitas broker dilewati: {e}")
        return {}, False


def _hold_layer_factor(cur: Any) -> tuple[dict[str, Any], dict[str, float] | None]:
    """Percentile faktor cross-sectional + bobot IC; gagal -> lapisan kosong.

    Percentile dihitung terhadap SELURUH pasar — bukan hanya watchlist — supaya
    ranking bermakna statistik. Bobot dibaca dari research.factor_ic_history
    (run `idx ic` terbaru); fallback konstanta bila tabel kosong.
    """
    from ...research.composite import latest_weights, market_factor_ranks

    try:
        return market_factor_ranks(cur), latest_weights(cur)
    except Exception as e:
        # Lapisan faktor adalah enhancement — DB query gagal tidak boleh
        # mematikan hold-check teknikal yang sudah jalan.
        logger.warning(f"[warn] composite factor ranks dilewati: {e}")
        return {}, None


def _hold_names(cur: Any) -> dict[str, str | None]:
    """Nama emiten terbaru per kode (untuk label hasil)."""
    cur.execute(
        """select distinct on (code) code, name from research.latest_pit
           order by code, trade_date desc"""
    )
    return {r["code"]: r["name"] for r in cur.fetchall()}


def _hold_metrics(cur: Any, code: str, min_days: int) -> dict[str, Any] | None:
    """Indikator + konteks harga satu emiten; ``None`` bila riwayat kurang."""
    df, _ = _build_frame(cur, code)
    if df.empty or len(df) < min_days:
        return None
    df = calculate_indicators(df)
    raw_signal = generate_signal(df)
    # Corp-action filter: SELL yang hanya terbentuk karena drop mekanis
    # ex-dividend dinetralkan jadi HOLD juga di hold-check, supaya skor tidak
    # kena penalti -25 untuk hal yang bukan tekanan jual.
    signal, div_cash, div_mech = _apply_dividend_guard(cur, code, df, raw_signal)
    last = df.iloc[-1]
    close = float(last["close"])
    ma_s = _f(last.get("MA_Short"))
    ma_l = _f(last.get("MA_Long"))
    macd = _f(last.get("MACD"))
    macd_sig = _f(last.get("MACD_Signal"))
    bb_u = _f(last.get("BB_Upper"))
    bb_l = _f(last.get("BB_Lower"))
    zc = _zscore_context(df)
    z = zc["z"]
    mean = zc["mean"]
    return {
        "signal": signal,
        "div_cash": div_cash,
        "div_mech": div_mech,
        "trend_up": bool(ma_s is not None and ma_l is not None and ma_s > ma_l),
        "rsi": _f(last.get("RSI")),
        "macd_bullish": bool(macd is not None and macd_sig is not None and macd > macd_sig),
        "bb_pos": (
            ("above" if close > bb_u else "below" if close < bb_l else "inside")
            if bb_u is not None and bb_l is not None and bb_u > bb_l
            else None
        ),
        "z": z,
        "below_target": (
            round((mean - close) / mean * 100, 1) if mean is not None and mean > 0 else None
        ),
    }


def _hold_skor_sinyal(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    """Sinyal teknikal — faktor dominan."""
    if m["signal"] == "BUY":
        return sk + 20, [*al, "Sinyal teknikal BUY (SMA20 > SMA50, RSI sehat)"]
    if m["signal"] == "SELL":
        return sk - 25, [*al, "Sinyal teknikal SELL (tren jangka pendek menembus ke bawah)"]
    if m["div_mech"]:
        return sk, [
            *al,
            f"Sinyal SELL dibatalkan: turun mekanis ex-dividend "
            f"(Rp {m['div_cash']:.0f}/saham) — bukan tekanan jual",
        ]
    return sk, [*al, "Sinyal teknikal HOLD (tidak ada konfirmasi kuat)"]


def _hold_skor_tren(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    if m["trend_up"]:
        return sk + 10, [*al, "Tren menengah masih naik (SMA20 di atas SMA50)"]
    return sk - 5, [*al, "Tren menengah melemah (SMA20 di bawah SMA50)"]


def _hold_skor_macd(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    if m["macd_bullish"]:
        return sk + 5, [*al, "MACD masih bullish (di atas garis sinyal)"]
    return sk - 5, [*al, "MACD melemah (di bawah garis sinyal)"]


def _hold_skor_rsi(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    rsi = m["rsi"]
    if rsi is None:
        return sk, al
    if rsi >= 75:
        return sk - 10, [*al, f"RSI {rsi:.0f} overbought — rentan koreksi"]
    if rsi <= 25:
        return sk - 10, [*al, f"RSI {rsi:.0f} oversold — tekanan jual kuat"]
    if rsi >= 65:
        return sk - 3, [*al, f"RSI {rsi:.0f} mendekati overbought"]
    return sk, al


def _hold_skor_valuasi(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    """Konteks valuasi (skor-z vs band 60 hari sendiri)."""
    z = m["z"]
    if z is None:
        return sk, al
    if z >= 2.0:
        return sk - 15, [*al, f"Harga jauh di atas band 60-hari (z {z:+.1f}) — overextended"]
    if z >= 1.5:
        return sk - 8, [*al, f"Harga di atas band 60-hari (z {z:+.1f}) — waspada"]
    if z <= -1.0:
        # Penurunan hanya sehat kalau trennya masih naik.
        if m["trend_up"]:
            return sk + 5, [
                *al,
                f"Harga di bawah band 60-hari (z {z:+.1f}) tapi tren masih naik — kualitas dip",
            ]
        return sk - 5, [*al, f"Harga di bawah band 60-hari (z {z:+.1f}) dengan tren melemah"]
    return sk, [*al, "Harga masih dalam band wajar 60-hari"]


def _hold_skor_bb(sk: float, al: list[str], m: dict[str, Any]) -> tuple[float, list[str]]:
    if m["bb_pos"] == "above":
        return sk - 5, [*al, "Harga menembus Bollinger atas — ekstensi jangka pendek"]
    if m["bb_pos"] == "below":
        return sk - 10, [*al, "Harga di bawah Bollinger bawah — tekanan jual"]
    return sk, al


def _hold_skor_dasar(m: dict[str, Any]) -> tuple[float, list[str]]:
    """Skor 0-100 dari komponen teknikal murni, sebelum lapisan faktor/broker.

    Urutan pemanggilan menentukan urutan alasan di keluaran; skornya sendiri
    penjumlahan sehingga urutan tidak mengubah hasil.
    """
    sk = 50.0
    al: list[str] = []
    sk, al = _hold_skor_sinyal(sk, al, m)
    sk, al = _hold_skor_tren(sk, al, m)
    sk, al = _hold_skor_macd(sk, al, m)
    sk, al = _hold_skor_rsi(sk, al, m)
    sk, al = _hold_skor_valuasi(sk, al, m)
    sk, al = _hold_skor_bb(sk, al, m)
    return max(0.0, min(100.0, sk)), al


def _hold_verdict(sk: float) -> str:
    if sk >= 75:
        return "STRONG HOLD"
    if sk >= 55:
        return "HOLD"
    if sk >= 35:
        return "TRIM"
    return "EXIT"


def _hold_baris(
    cur: Any,
    code: str,
    nama: dict[str, str | None],
    market_ranks: dict[str, Any],
    weights: dict[str, float] | None,
    broker_by_code: dict[str, dict[str, Any]],
    broker_validated: bool,
    min_days: int,
) -> dict[str, Any] | None:
    """Satu baris hold-check: skor dasar, lapisan faktor, lapisan broker."""
    from ...research.broker_activity import apply_broker_layer
    from ...research.composite import apply_factor_layer

    m = _hold_metrics(cur, code, min_days)
    if m is None:
        return None

    base_score, reasons = _hold_skor_dasar(m)
    verdict = _hold_verdict(base_score)

    base = market_ranks.get(code)
    score, verdict, reasons, factor_adj = apply_factor_layer(
        base_score, verdict, reasons, base, weights
    )
    broker_row = broker_by_code.get(code)
    score, verdict, reasons, broker_adj = apply_broker_layer(
        score,
        verdict,
        reasons,
        broker_row["score"] if broker_row else None,
        broker_validated,
    )

    return {
        "code": code,
        "name": nama.get(code),
        "signal": m["signal"],
        "trend_up": m["trend_up"],
        "rsi": m["rsi"],
        "macd_bullish": m["macd_bullish"],
        "bb_position": m["bb_pos"],
        "z_score": m["z"],
        "below_target_pct": m["below_target"],
        "foreign_net": None,
        "score": round(score),  # type: ignore[arg-type]
        "base_score": round(base_score),  # type: ignore[arg-type]
        "factor_adj": round(factor_adj, 1),
        "factor_pct": base if base is not None else None,
        "broker_score": broker_row["score"] if broker_row else None,
        "broker_adj": round(broker_adj, 1),
        "verdict": verdict,
        "reasons": reasons,
    }


def _hold_lengkapi_foreign_net(out: list[dict[str, Any]]) -> None:
    """Isi ``foreign_net`` EOD sebagai konteks; pass terpisah, loop utama tetap ramping."""
    with get_cursor() as cur:
        for row in out:
            cur.execute(
                """select foreign_net from research.latest_pit
                   where code = %s order by trade_date desc limit 1""",
                (row["code"],),
            )
            r = cur.fetchone()
            row["foreign_net"] = _f(r["foreign_net"]) if r else None


def get_hold_check(codes: list[str], min_days: int = 40) -> list[dict[str, Any]]:
    """ "Hold Check" verdicts: is each watched stock still worth holding?

    Combines the technical signal (SMA20/50 + RSI rule) with the mean-reversion
    valuation context from get_valuation into one 0-100 score and a verdict:

    - STRONG HOLD (>=75): technicals intact and valuation not stretched.
    - HOLD (55-74): mostly intact, some caution.
    - TRIM (35-54): warning signs — overextended above band, weakening trend,
      or momentum loss with stretched price.
    - EXIT (<35): technical breakdown (SELL signal) and/or deep weakness.

    This is a research aid, not a recommendation to buy/sell.
    """
    # Skor per emiten tidak berubah intraday (sumbernya EOD/IC/broker snapshot),
    # dan perhitungannya mahal (~4-5s untuk seluruh watchlist). Cache 10 menit
    # di kunci (codes, min_days); serve loop mengosongkannya tiap refresh via
    # POST /api/cache/clear.
    from ..analytics import _research_cache_get, _research_cache_put

    cache_key = f"hold_check:{','.join(sorted(codes))}:{min_days}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    broker_by_code, broker_validated = _hold_layer_broker()

    out: list[dict[str, Any]] = []
    with get_cursor() as cur:
        nama = _hold_names(cur)
        market_ranks, weights = _hold_layer_factor(cur)
        for code in codes:
            baris = _hold_baris(
                cur,
                code,
                nama,
                market_ranks,
                weights,
                broker_by_code,
                broker_validated,
                min_days,
            )
            if baris is not None:
                out.append(baris)

    # EOD foreign net for context (separate pass, keeps the main loop simple).
    _hold_lengkapi_foreign_net(out)

    out.sort(key=lambda r: r["score"], reverse=True)
    _research_cache_put(cache_key, out, ttl=600.0)
    return out


def _ex_div_cash(cur: Any, code: str, lookback_days: int = 7) -> float | None:
    """Cash dividend ex-date terakhir dalam `lookback_days` hari kalender.

    Window ketat: drop mekanis ex-dividend hanya relevan untuk sinyal yang
    terbentuk sekitar ex-date. None bila tidak ada.
    """
    cur.execute(
        """select cash_amount from research.corporate_actions
           where code = %s and action_type = 'dividend'
             and cash_amount is not null and cash_amount > 0
             and ex_date between current_date - %s::int and current_date
           order by ex_date desc limit 1""",
        (code, lookback_days),
    )
    row = cur.fetchone()
    return float(row["cash_amount"]) if row else None


def _apply_dividend_guard(
    cur: Any, code: str, df: pd.DataFrame, signal: str
) -> tuple[str, float | None, bool]:
    """Netralkan SELL palsu akibat ex-dividend (corp-action filter #4).

    Returns (signal_efektif, div_cash, was_mechanical). Hanya dipanggil bila
    sinyal mentah SELL — tidak menyentuh BUY/HOLD.
    """
    if signal != "SELL":
        return signal, None, False
    div = _ex_div_cash(cur, code)
    if div is None:
        return signal, None, False
    if is_mechanical_sell(df, div):
        return "HOLD", div, True
    return signal, div, False


def get_signals(codes: list[str], min_days: int = 25) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    generated_at = datetime.now(WIB).isoformat(timespec="seconds")
    with get_cursor() as cur:
        for code in codes:
            df, is_live = _build_frame(cur, code)
            if df.empty or len(df) < min_days:
                continue
            df = calculate_indicators(df)
            signal = generate_signal(df)
            last, prev = df.iloc[-1], df.iloc[-2]
            pct = None
            if prev["close"]:
                pct = (last["close"] - prev["close"]) / prev["close"] * 100
            # Corp-action filter: SELL yang hanya terbentuk karena drop
            # mekanis ex-dividend -> HOLD (bukan tekanan jual sesungguhnya).
            eff_signal, div_cash, mech = _apply_dividend_guard(cur, code, df, signal)
            if mech:
                pct = pct + (div_cash / prev["close"] * 100) if prev["close"] else pct
            last_date = last["date"]
            as_of = (
                last_date.strftime("%Y-%m-%d")
                if hasattr(last_date, "strftime")
                else _norm_date(last_date)
            )
            out.append(
                {
                    "code": code,
                    "signal": eff_signal,
                    "raw_signal": signal,
                    "div_cash": div_cash,
                    "div_adjusted": mech,
                    "close": _f(last["close"]),
                    "pct": _f(pct),
                    "rsi": _f(last.get("RSI")),
                    "sma20": _f(last.get("MA_Short")),
                    "macd": _f(last.get("MACD")),
                    "trend_up": bool((last.get("MA_Short") or 0) > (last.get("MA_Long") or 0)),
                    "live": is_live,
                    "as_of": as_of,
                    "generated_at": generated_at,
                }
            )
    return out


# --------------------------------------------------------------- technical chart


def get_technical_chart(code: str) -> dict[str, Any]:
    with get_cursor() as cur:
        df, _ = _build_frame(cur, code)
    if df.empty:
        return {"code": code, "signal": "HOLD", "bars": []}
    df = calculate_indicators(df)
    signal = generate_signal(df)
    bars: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        d = row["date"]
        bars.append(
            {
                "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else _norm_date(d),
                "open": _f(row.get("open")),
                "high": _f(row.get("high")),
                "low": _f(row.get("low")),
                "close": _f(row.get("close")),
                "volume": _f(row.get("volume")),
                "ma_short": _f(row.get("MA_Short")),
                "ma_long": _f(row.get("MA_Long")),
                "rsi": _f(row.get("RSI")),
                "bb_upper": _f(row.get("BB_Upper")),
                "bb_lower": _f(row.get("BB_Lower")),
                "macd": _f(row.get("MACD")),
                "macd_signal": _f(row.get("MACD_Signal")),
            }
        )
    return {"code": code, "signal": signal, "bars": bars}
