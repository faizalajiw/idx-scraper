"""Engine Information Coefficient (IC) analysis.

IC = korelasi rank (Spearman) antara nilai faktor di hari `T` dan forward
return di horizon `k` hari bursa, dihitung per tanggal lalu diagregasi.

Interpretasi (rule of thumb kuant):
- |mean IC| >= 0.05 dengan ICIR = mean/std > 0.5  : faktor layak dipakai.
- IC decay (k=1,5,10,21): seberapa cepat sinyal terurai.
- IC per kuintil: cek monotonicity — faktor bervalidasi kalau kuintil teratas
  vs terbawah benar-benar berbeda returnnya.

Anti look-ahead:
- Faktor di (code, T) hanya dari data <= T (lihat factors.py).
- Forward return T->T+k dibangun dari **lagged price** dengan lag `nlags`
  hari bursa (default 1 = fill di close T+1, standar disiplin backtest).
- Semua join antar tanggal strict as-of: nilai di tanggal T diambil dari baris
  dengan date <= T terakhir yang tersedia, tanpa interpolasi masa depan.

Contoh pakai:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.ic --k 5 10 --nlags 1
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd
import psycopg

from .factors import FACTOR_DEFINITIONS, compute_factors

# --------------------------------------------------------------------------- #
# Panel loader
# --------------------------------------------------------------------------- #


def load_panel(
    dsn: str,
    start: str | None = None,
    end: str | None = None,
    min_history: int = 60,
) -> pd.DataFrame:
    """Muat panel harga adjusted (PIT, look-ahead-free) dari research.prices_asof_adj.

    - ``min_history``: buang emiten dengan observasi lebih sedikit dari itu
      (faktor warmup 252 hari butuh sejarah panjang; emiten baru listing
      hanya menambah noise).
    - Harga yang dipakai FAKTOR adalah adj_close (raw boleh loncat karena
      split/bonus); value tetap dari raw layer untuk likuiditas.
    """
    params: list = []
    where = []
    if start:
        where.append("trade_date >= %s")
        params.append(start)
    if end:
        where.append("trade_date <= %s")
        params.append(end)
    where_sql = ("where " + " and ".join(where)) if where else ""

    query = f"""
        with px as (
            select code, trade_date, adj_close, raw_close, volume
            from research.prices_asof_adj(now())
            {where_sql}
        )
        select
            px.code,
            px.trade_date  as date,
            px.adj_close   as close,
            px.volume,
            e.value,
            e.foreign_net
        from px
        left join research.latest_pit e
          on e.code = px.code and e.trade_date = px.trade_date
    """
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(
            columns=["code", "date", "close", "volume", "value", "foreign_net"]
        )

    df = pd.DataFrame(
        rows, columns=["code", "date", "close", "volume", "value", "foreign_net"]
    )
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["foreign_net"] = pd.to_numeric(df["foreign_net"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]

    if min_history > 1:
        counts = df.groupby("code")["code"].transform("size")
        df = df[counts >= min_history]
    return df.sort_values(["code", "date"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Forward returns + IC computation
# --------------------------------------------------------------------------- #
def forward_returns(panel: pd.DataFrame, horizons: list[int], nlags: int = 1) -> pd.DataFrame:
    """Tambahkan kolom ``fwd_{k}`` = return close T -> T+k dengan fill T+`nlags`.

    Lag `nlags` hari bursa (bukan kalender): t_conf = trading-day index T + nlags,
    t_exit = T + nlags + k. Index dihitung per emiten pada baris observasinya,
    jadi hari libur otomatis ter-lewati.

    ``shift(-n)`` di sini sah (beda dengan aturan "no shift(-n)" untuk
    indikator): yang dibangun adalah LABEL return masa depan untuk mengukur
    faktor, bukan sinyal yang dikonsumsi strategi. Look-ahead yang dilarang
    tetap terjaga karena faktor sendiri backward-looking dan eksekusi sudah
    di-lag `nlags` hari setelah tanggal faktor.
    """
    df = panel.sort_values(["code", "date"]).copy()
    g = df.groupby("code")
    n_in_code = g["code"].transform("size")

    for k in horizons:
        entry_pos = df.groupby("code").cumcount() + nlags
        exit_pos = entry_pos + k
        # Di luar sejarah emiten -> NaN (tidak ada harga exit).
        valid = (entry_pos < n_in_code) & (exit_pos < n_in_code)
        close_entry = _shift_by_pos(df, "close", entry_pos)
        close_exit = _shift_by_pos(df, "close", exit_pos)
        fwd = close_exit / close_entry - 1.0
        df[f"fwd_{k}"] = fwd.where(valid)
    return df


def _shift_by_pos(df: pd.DataFrame, col: str, target_pos: pd.Series) -> pd.Series:
    """Ambil nilai `col` dari baris dengan cumcount == target_pos (per code).

    Baris tanpa padanannya (menembus akhir sejarah emiten) -> NaN.
    """
    tmp = df[["code"]].copy()
    tmp["pos"] = tmp.groupby("code").cumcount()
    tmp["val"] = df[col].astype(float)
    # join strict on (code, pos == target_pos) tanpa melihat ke masa depan
    # di luar emiten masing-masing.
    lookup = tmp.set_index(["code", "pos"])["val"]
    keys = list(zip(tmp["code"], target_pos.astype(int)))
    vals = [lookup.get(k, np.nan) for k in keys]
    return pd.Series(vals, index=df.index, dtype=float)


def _spearman(x: pd.Series, y: pd.Series) -> float | None:
    """Korelasi rank Spearman dengan scipy-free fallback (pure numpy)."""
    pair = pd.concat([x, y], axis=1, keys=["x", "y"]).dropna()
    if len(pair) < 10:
        return None
    rx = pair["x"].rank()
    ry = pair["y"].rank()
    dx = rx - rx.mean()
    dy = ry - ry.mean()
    denom = math.sqrt((dx * dx).sum() * (dy * dy).sum())
    if denom == 0:
        return None
    return float((dx * dy).sum() / denom)


def rank_ic(
    merged: pd.DataFrame,
    factors: list[str],
    horizon: int,
    min_names: int = 10,
) -> pd.DataFrame:
    """IC harian per faktor: Spearman(factor_T, fwd_k) per tanggal."""
    out_rows = []
    fwd = f"fwd_{horizon}"
    for d, day_df in merged.groupby("date"):
        row = {"date": d, "n": int(day_df[fwd].notna().sum())}
        for f in factors:
            row[f] = _spearman(day_df[f], day_df[fwd])
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def summarize(ic_df: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """Agregat IC harian: mean, std, ICIR, hit-rate, t-stat (Newey-West lag 5)."""
    rows = []
    for f in factors:
        s = ic_df[f].dropna()
        if len(s) < 5:
            rows.append({"factor": f, "n_days": len(s), "mean_ic": np.nan})
            continue
        mean = s.mean()
        std = s.std(ddof=1)
        # Newey-West std of mean, lag = 5 hari (autokorelasi return tumpang tindih)
        demeaned = s - mean
        T = len(demeaned)
        lag = min(5, T - 1)
        gamma0 = (demeaned * demeaned).mean()
        nw_var = gamma0
        for l in range(1, lag + 1):
            cov = (demeaned.iloc[l:] * demeaned.iloc[:-l].values).mean()
            w = 1.0 - l / (lag + 1.0)  # Bartlett kernel
            nw_var += 2.0 * w * cov
        nw_std = math.sqrt(nw_var / T) if nw_var > 0 else None
        rows.append(
            {
                "factor": f,
                "n_days": T,
                "mean_ic": mean,
                "ic_std": std,
                # std=0 (IC konstan) -> inf bermakna; mean juga 0 -> NaN.
                "icir": (mean / std)
                if std
                else (math.inf if mean > 0 else (-math.inf if mean < 0 else np.nan)),
                "hit_rate": (np.sign(s) == np.sign(mean)).mean(),
                "t_stat": (
                    mean / nw_std
                    if nw_std
                    else (math.inf if mean > 0 else (-math.inf if mean < 0 else np.nan))
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("mean_ic", key=lambda s: s.abs(), ascending=False)


def quintile_returns(
    merged: pd.DataFrame, factor: str, horizon: int, q: int = 5
) -> pd.DataFrame:
    """Mean forward return per kuintil faktor (cross-sectional, per tanggal lalu rata)."""
    fwd = f"fwd_{horizon}"
    per_day = []
    for d, day_df in merged.groupby("date"):
        dd = day_df[[factor, fwd]].dropna()
        if len(dd) < 25:  # butuh cukup nama biar kuintil bermakna
            continue
        try:
            buckets = pd.qcut(dd[factor], q, labels=False, duplicates="drop")
        except ValueError:
            continue
        means = dd.groupby(buckets)[fwd].mean()
        per_day.append(means)
    if not per_day:
        return pd.DataFrame()
    stacked = pd.concat(per_day, axis=1)
    return pd.DataFrame(
        {"quintile": range(stacked.shape[0]), "mean_fwd_ret": stacked.mean(axis=1).values}
    )


# --------------------------------------------------------------------------- #
# Orkestrasi + CLI
# --------------------------------------------------------------------------- #


@dataclass
class ICReport:
    horizons: list[int]
    summaries: dict[int, pd.DataFrame]      # horizon -> ringkasan agregat
    daily_ic: dict[int, pd.DataFrame]       # horizon -> IC harian
    quintiles: dict[tuple[str, int], pd.DataFrame]  # (factor, horizon) -> kuintil


def run_ic_analysis(
    dsn: str,
    horizons: list[int] | None = None,
    nlags: int = 1,
    start: str | None = None,
    end: str | None = None,
    min_history: int = 60,
    min_names: int = 10,
    top_quintile_factors: int = 3,
) -> ICReport:
    """Pipeline lengkap: load -> faktor -> fwd returns -> IC -> ringkasan."""
    horizons = horizons or [5, 10]
    panel = load_panel(dsn, start=start, end=end, min_history=min_history)
    if panel.empty:
        raise SystemExit("panel kosong — cek DATABASE_URL / rentang tanggal")

    panel = compute_factors(panel)
    merged = forward_returns(panel, horizons, nlags=nlags)
    factors = [c for c in FACTOR_DEFINITIONS if c in merged.columns]
    # Buang faktor yang kosong total (mis. foreign flow belum ada datanya).
    factors = [f for f in factors if merged[f].notna().any()]

    daily_ic: dict[int, pd.DataFrame] = {}
    summaries: dict[int, pd.DataFrame] = {}
    quintiles: dict[tuple[str, int], pd.DataFrame] = {}

    for k in horizons:
        ic = rank_ic(merged, factors, k, min_names=min_names)
        daily_ic[k] = ic
        summaries[k] = summarize(ic, factors)
        # Kuintil hanya untuk top-|IC| biar output tidak meledak.
        top = summaries[k].head(top_quintile_factors)["factor"].tolist()
        for f in top:
            quintiles[(f, k)] = quintile_returns(merged, f, k)

    return ICReport(
        horizons=horizons,
        summaries=summaries,
        daily_ic=daily_ic,
        quintiles=quintiles,
    )


def print_report(report: ICReport) -> None:
    print("=" * 78)
    print("IC ANALYSIS — Spearman rank IC faktor vs forward return (IDX, PIT-adjusted)")
    print("=" * 78)
    for k in report.horizons:
        s = report.summaries[k]
        print(f"\n--- Horizon {k} hari bursa (fill T+1, Newey-West lag 5) ---")
        if s.empty:
            print("  (tidak cukup data)")
            continue
        for _, r in s.iterrows():
            if pd.isna(r.get("mean_ic")):
                print(f"  {r['factor']:<16}  (kurang dari 5 hari IC valid)")
                continue
            star = ""
            icir_str = f"{r['icir']:+.2f}" if math.isfinite(r["icir"]) else "inf"
            if abs(r["mean_ic"]) >= 0.05 and abs(r["icir"]) >= 0.5:
                star = "  <-- LAYAK"
            elif abs(r["mean_ic"]) >= 0.03:
                star = "  (+ menjanjikan)"
            print(
                f"  {r['factor']:<16} IC={r['mean_ic']:+.4f}  "
                f"ICIR={icir_str}  hit={r['hit_rate']:.0%}  "
                f"t={r['t_stat']:+.1f}  hari={int(r['n_days'])}{star}"
            )
        print()
        for (f, kh), qdf in report.quintiles.items():
            if kh != k or qdf.empty:
                continue
            qtxt = "  ".join(
                f"Q{int(row.quintile)}={row.mean_fwd_ret:+.2%}" for row in qdf.itertuples()
            )
            spread = (
                qdf.iloc[-1]["mean_fwd_ret"] - qdf.iloc[0]["mean_fwd_ret"]
                if len(qdf) >= 2
                else float("nan")
            )
            print(f"  kuintil {f:<16} {qtxt}   | Q5-Q1 spread={spread:+.2%}")
    print()
    print("Catatan: |IC|>=0.05 & |ICIR|>=0.5 = layak dipakai; t-stat pakai Newey-West")
    print("(lag 5) karena forward return tumpang tindih antar hari sampling.")
    print("Faktor flow (foreign_*) coverage-nya masih pendek di data sekarang —")
    print("baca hasilnya sebagai indikatif, ulangi setelah histori flow memanjang.")


def main() -> int:
    ap = argparse.ArgumentParser(prog="ic")
    ap.add_argument("--k", type=int, nargs="+", default=[5, 10], help="horizon hari bursa")
    ap.add_argument("--nlags", type=int, default=1, help="lag fill (1 = close T+1)")
    ap.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    ap.add_argument("--min-history", type=int, default=60)
    ap.add_argument("--min-names", type=int, default=10, help="nama minimum per hari IC")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=__import__("sys").stderr)
        return 1

    report = run_ic_analysis(
        dsn,
        horizons=args.k,
        nlags=args.nlags,
        start=args.start,
        end=args.end,
        min_history=args.min_history,
        min_names=args.min_names,
    )
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
