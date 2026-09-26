"""Technical analysis utilities for IDX stocks."""

import numpy as np
import pandas as pd

# pandas_ta not available on Python 3.14; using pure pandas instead


def is_mechanical_sell(df: pd.DataFrame, div_cash: float | None) -> bool:
    """True bila SELL di bar terakhir hilang setelah dividen dikembalikan.

    Pada ex-date, close turun mekanis sebesar cash dividend (uang pindah ke
    pemegang saham, bukan hilang). Rule SMA/RSI bisa membaca drop itu sebagai
    breakdown -> SELL palsu. Pembandingan: hitung ulang sinyal pada basis
    total-return (close terakhir + div_cash). Hanya SELL yang bisa "palsu";
    BUY/HOLD tidak pernah dinegasi oleh kembalian dividen.

    Pure & testable — tidak menyentuh DB.
    """
    if div_cash is None or div_cash <= 0 or df.empty:
        return False
    if generate_signal(df) != "SELL":
        return False
    adj = df.copy()
    adj["close"] = pd.to_numeric(adj["close"], errors="coerce")
    adj.iloc[-1, adj.columns.get_loc("close")] += float(div_cash)
    return generate_signal(calculate_indicators(adj)) != "SELL"

# Regime thresholds (IHSG): ADX >= 25 tren kuat, < 20 ranging.
ADX_TREND = 25.0
ADX_RANGE = 20.0
# Realized vol ter-annualisasi (%): IDX ~240 hari bursa/tahun.
IDX_TRADING_DAYS = 240
VOL_HIGH = 30.0   # >= 30% ann = VOLATILE
VOL_LOW = 15.0    # <= 15% ann = QUIET


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX + DI+/DI- (Wilder) — pure pandas, tanpa dependensi baru.

    Butuh kolom ``high``/``low`` (dan ``close`` untuk TR prev-close).
    Smoothing Wilder via ``ewm(alpha=1/period, adjust=False)`` dengan
    ``min_periods`` supaya baris warmup tetap NaN. Semua backward-looking.
    """
    df = df.copy()
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)

    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    # +DM saat up_move > down_move dan positif; -DM sebaliknya.
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period
    smooth = lambda s: s.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    tr_s = smooth(tr)
    plus_di = 100.0 * smooth(plus_dm) / tr_s.where(tr_s > 0)
    minus_di = 100.0 * smooth(minus_dm) / tr_s.where(tr_s > 0)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).where(lambda s: s > 0)
    df["DI_Plus"] = plus_di
    df["DI_Minus"] = minus_di
    df["ADX"] = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return df


def classify_regime(df: pd.DataFrame) -> dict:
    """Klasifikasi regime pasar dari frame yang sudah punya ADX/DI.

    Returns dict: ``regime`` (TRENDING_UP | TRENDING_DOWN | TRANSITION |
    RANGING), ``adx``, ``plus_di``, ``minus_di``, ``realized_vol_annual``
    (% ann, rolling 20), ``vol_state`` (VOLATILE | NORMAL | QUIET).
    Data kurang -> ``regime=None``.
    """
    last = df.iloc[-1] if not df.empty else None
    if last is None or pd.isna(last.get("ADX")):
        return {
            "regime": None, "adx": None, "plus_di": None, "minus_di": None,
            "realized_vol_annual": None, "vol_state": None,
        }
    adx = float(last["ADX"])
    pdi = float(last["DI_Plus"]) if pd.notna(last.get("DI_Plus")) else None
    mdi = float(last["DI_Minus"]) if pd.notna(last.get("DI_Minus")) else None

    if adx >= ADX_TREND:
        if pdi is not None and mdi is not None and pdi < mdi:
            regime = "TRENDING_DOWN"
        else:
            regime = "TRENDING_UP"
    elif adx < ADX_RANGE:
        regime = "RANGING"
    else:
        regime = "TRANSITION"

    ret = df["close"].pct_change()
    rv = ret.rolling(20).std().iloc[-1]
    rv_ann = float(rv * np.sqrt(IDX_TRADING_DAYS) * 100) if pd.notna(rv) else None
    if rv_ann is None:
        vol_state = None
    elif rv_ann >= VOL_HIGH:
        vol_state = "VOLATILE"
    elif rv_ann <= VOL_LOW:
        vol_state = "QUIET"
    else:
        vol_state = "NORMAL"

    return {
        "regime": regime,
        "adx": round(adx, 1),
        "plus_di": round(pdi, 1) if pdi is not None else None,
        "minus_di": round(mdi, 1) if mdi is not None else None,
        "realized_vol_annual": round(rv_ann, 1) if rv_ann is not None else None,
        "vol_state": vol_state,
    }


def signal_series(df: pd.DataFrame) -> pd.Series:
    """BUY/SELL/HOLD **per baris** — rule yang sama dengan ``generate_signal``.

    Satu sumber kebenaran untuk rule sinyal: generate_signal memakai baris
    terakhir deret ini, screener memakai seluruh deret (hitung hari sejak
    sinyal terakhir). Input NaN -> HOLD untuk baris itu.
    """
    hold = pd.Series("HOLD", index=df.index, dtype=object)
    if df.empty:
        return hold
    rsi = df.get("RSI")
    ma_short = df.get("MA_Short")
    ma_long = df.get("MA_Long")
    if rsi is None or ma_short is None or ma_long is None:
        return hold
    buy = (ma_short > ma_long) & (rsi >= 50) & (rsi <= 80)
    sell = (ma_short < ma_long) & (rsi >= 20) & (rsi <= 50)
    valid = rsi.notna() & ma_short.notna() & ma_long.notna()
    out = hold.copy()
    out[buy & valid] = "BUY"
    out[sell & valid] = "SELL"
    return out


def calculate_indicators(df: pd.DataFrame, short_ma: int = 20, long_ma: int = 50, rsi_period: int = 14) -> pd.DataFrame:
    """Add technical indicators to a DataFrame using pure pandas/numpy (no pandas_ta)."""
    df = df.copy()
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)

    # Short and long MA
    df["MA_Short"] = df["close"].rolling(short_ma).mean()
    df["MA_Long"] = df["close"].rolling(long_ma).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # Bollinger Bands (20, 2)
    bb_mid = df["close"].rolling(20).mean()
    bb_std = df["close"].rolling(20).std()
    df["BB_Upper"] = bb_mid + 2 * bb_std
    df["BB_Lower"] = bb_mid - 2 * bb_std

    # MACD (12, 26, 9)
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

    return df


def generate_signal(df: pd.DataFrame) -> str:
    """Generate BUY/SELL/HOLD from trend (SMA20 vs SMA50) + RSI filter.

    BUY  : uptrend (SMA20 > SMA50) dengan RSI 50-80 (momentum naik, belum jenuh)
    SELL : downtrend (SMA20 < SMA50) dengan RSI 20-50 (melemah, belum oversold ekstrem)
    HOLD : data kurang / kondisi tidak jelas
    """
    if df.empty:
        return "HOLD"
    return str(signal_series(df).iloc[-1])
