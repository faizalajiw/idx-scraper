"""Technical analysis utilities for IDX stocks."""

import pandas as pd

# pandas_ta not available on Python 3.14; using pure pandas instead


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
    last = df.iloc[-1]
    rsi = last.get("RSI")
    ma_short = last.get("MA_Short")
    ma_long = last.get("MA_Long")
    if any(v is None or pd.isna(v) for v in (rsi, ma_short, ma_long)):
        return "HOLD"
    if ma_short > ma_long and 50 <= rsi <= 80:
        return "BUY"
    if ma_short < ma_long and 20 <= rsi <= 50:
        return "SELL"
    return "HOLD"
