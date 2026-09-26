"""Faktor cross-sectional point-in-time untuk IC analysis.

Aturan yang dipegang modul ini (lihat juga skill technical-analysis-patterns):

1. **Pure pandas/numpy** — tanpa dependensi TA baru.
2. **No look-ahead.** Faktor di baris `(code, date)` hanya boleh pakai data
   `<= date`. Semua rolling backward; tidak ada `shift(-n)` / `bfill`.
3. **Warmup tetap NaN.** Baris warmup dibiarkan NaN dan nanti di-drop per
   tanggal, bukan diisi angka rekaan.
4. **Sort by date dulu** sebelum rolling/groupby time-series.
5. **Skala-invariant.** Faktor harga mentah (close) sengaja tidak dipakai;
   yang masuk rasio/return/percentile supaya cross-section antar emiten
   sebanding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Kolom minimum yang dibutuhkan panel (output FactorPanelLoader).
PANEL_COLUMNS = ["code", "date", "close", "volume", "value", "foreign_net"]

FACTOR_DEFINITIONS: dict[str, str] = {
    # --- momentum / reversal (return historis, backward-looking) ---
    "mom_5d": "Return 5 hari bursa (momentum jangka pendek)",
    "mom_10d": "Return 10 hari bursa",
    "mom_21d": "Return 1 bulan bursa (~21 hari)",
    "mom_63d": "Return 3 bulan bursa (~63 hari)",
    "mom_12_1": "Momentum klasik 12-1: return ~252h dikurangi bulan terakhir",
    "rev_1d": "Reversal 1 hari (kebalikan return kemarin)",
    # --- volatilitas (rendah = defensor) ---
    "vol_21d": "Volatilitas 21 hari (std return harian)",
    # --- likuiditas ---
    "amihud_21d": "Illiquidity Amihud 21 hari: |ret| per rupiah volume",
    "turnover_21d": "Rata-rata value transaksi 21 hari (log)",
    "rel_volume": "Volume hari ini relatif rata-rata 21 hari",
    # --- posisi harga ---
    "dist_52w_high": "Jarak dari puncak 52 minggu (<= 0)",
    "vol_rank_63d": "Percentile volume hari ini dalam 63 hari terakhir",
    # --- flow ---
    "foreign_net_pct": "Foreign net / value transaksi (hari yang sama)",
    "foreign_streak": "Hari berturut-turut foreign net positif (negatif = jual)",
}


def compute_factors(panel: pd.DataFrame, include_foreign: bool = True) -> pd.DataFrame:
    """Hitung semua faktor di atas panel `(code, date)` lama.

    Parameters
    ----------
    panel:
        Kolom wajib: ``code, date, close, volume``; opsional ``value``,
        ``foreign_net``. Satu baris per (code, date), boleh ada tanggal bolong
        (hari libur / emiten baru listing) — rolling dihitung per kode pada
        baris observasi yang ada.
    include_foreign:
        Skip faktor flow kalau panel tidak punya kolom ``foreign_net``.

    Returns
    -------
    DataFrame dengan kolom ``code, date`` + satu kolom per faktor.
    Faktor di baris `(code, date)` hanya memakai data `<= date`.
    """
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    # Return harian per emiten (groupby shift = backward-looking).
    df["_ret"] = df.groupby("code")["close"].transform(lambda s: s.pct_change())

    g = df.groupby("code", sort=False)

    # --- momentum / reversal ---
    for n, name in ((5, "mom_5d"), (10, "mom_10d"), (21, "mom_21d"), (63, "mom_63d")):
        df[name] = g["close"].transform(lambda s, n=n: s.pct_change(n))
    # Momentum klasik 12-1: return dari t-252 ke t-21 (bulan terakhir di-skip
    # karena didominasi reversal jangka pendek).
    df["mom_12_1"] = g["close"].shift(21) / g["close"].shift(252) - 1.0

    df["rev_1d"] = -df["_ret"]

    # --- volatilitas ---
    df["vol_21d"] = g["_ret"].transform(lambda s: s.rolling(21).std())

    # --- likuiditas ---
    # Amihud: mean(|ret| / value). Value 0/hari non-trading -> NaN agar tidak
    # memproduksi infinit.
    value = df["value"] if "value" in df.columns else df["close"] * df["volume"]
    illiq = df["_ret"].abs() / value.replace(0, np.nan)
    df["amihud_21d"] = illiq.groupby(df["code"]).transform(lambda s: s.rolling(21).mean())
    df["turnover_21d"] = np.log(
        value.where(value > 0).groupby(df["code"]).transform(lambda s: s.rolling(21).mean())
    )
    vol_mean_21 = df.groupby("code")["volume"].transform(lambda s: s.rolling(21).mean())
    df["rel_volume"] = df["volume"] / vol_mean_21.where(vol_mean_21 > 0)

    # --- posisi harga ---
    high_252 = df.groupby("code")["close"].transform(lambda s: s.rolling(252, min_periods=63).max())
    df["dist_52w_high"] = df["close"] / high_252 - 1.0
    df["vol_rank_63d"] = df.groupby("code")["volume"].transform(
        lambda s: s.rolling(63, min_periods=21).rank(pct=True)
    )

    # --- flow ---
    if include_foreign and "foreign_net" in df.columns:
        fn = df["foreign_net"].astype(float)
        df["foreign_net_pct"] = fn / value.replace(0, np.nan)
        # Streak bertanda: +n saat net positif n hari berturut, -n saat negatif.
        pos = (fn > 0).astype(int)
        neg = (fn < 0).astype(int)

        def _streak(flags: pd.Series) -> pd.Series:
            grp = flags.groupby((flags != flags.shift()).cumsum())
            runs = grp.cumsum() * flags
            return runs

        streak = np.where(fn > 0, _streak(pos), np.where(fn < 0, -_streak(neg), 0))
        # NaN input (hari tanpa data flow) -> NaN, bukan 0.
        df["foreign_streak"] = pd.Series(streak, index=df.index).where(fn.notna())
    else:
        df["foreign_net_pct"] = np.nan
        df["foreign_streak"] = np.nan

    factor_cols = [c for c in FACTOR_DEFINITIONS if c in df.columns]
    # close ikut dibawa: dibutuhkan forward_returns di ic.py.
    return df[["code", "date", "close", *factor_cols]]
