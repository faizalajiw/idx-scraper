"""Jejak sinyal — log harian BUY/SELL + evaluasi track record.

Menjawab pertanyaan yang sebelumnya tidak bisa dijawab dashboard: **"sinyal
BUY/SELL yang kita pakai selama ini benar-benar menghasilkan apa?"**

Aturan main modul ini (sama dengan ``research.ic`` / ``research.events``):

1. **Point-in-time.** Sinyal di tanggal T hanya dihitung dari harga ``<= T``
   (``calculate_indicators`` + ``signal_series`` — rule yang sama persis dengan
   dashboard). Tidak ada ``shift(-n)`` di jalur sinyal.
2. **Eksekusi T+1.** Penilaian memakai entry di close T+1 (``nlags=1``),
   konsisten dengan IC analysis dan backtest — bukan close hari sinyal.
3. **Abnormal vs pasar.** Return window yang sama dibandingkan dengan
   equal-weight seluruh pasar (market dari panel itu sendiri, PIT-safe).
4. **Filter corp-action.** SELL yang hanya terbentuk karena drop mekanis
   ex-dividend tidak dicatat (memakai ``analysis.is_mechanical_sell``), supaya
   track record tidak dihitung atas sinyal palsu.
5. **Idempoten per (code, trade_date).** Run ulang menimpa baris lama, tidak
   menduplikasi.

Tabel ``research.signal_log`` sengaja HANYA menyimpan sinyal non-HOLD: "hari
sinyal" adalah definisinya, dan tabel tetap kecil.

Contoh pakai:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.signal_log \\
        --months 18
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import psycopg

from ..analysis import calculate_indicators, is_mechanical_sell, signal_series
from .events import _nw_tstat

# --- parameter default ------------------------------------------------------
MIN_HISTORY_DEFAULT = 60   # warmup SMA50 + RSI; di bawah ini sinyal tidak stabil
DEFAULT_HORIZONS: tuple[int, ...] = (5, 10, 21)
NLAGS_DEFAULT = 1          # entry di close T+1 (disiplin backtest)
DIV_GUARD_DAYS = 7         # window ex-dividend untuk menolak SELL palsu
BACKFILL_MONTHS_DEFAULT = 18

SIGNAL_TYPES = ("BUY", "SELL")

WIB = timezone(timedelta(hours=7))  # tanggal bursa mengikuti WIB, bukan tz mesin

_DDL = """
create table if not exists research.signal_log (
    code        TEXT        NOT NULL,
    trade_date  DATE        NOT NULL,
    signal      TEXT        NOT NULL CHECK (signal IN ('BUY','SELL')),
    close       NUMERIC(18,4),
    rsi         NUMERIC(8,2),
    sma_short   NUMERIC(18,4),
    sma_long    NUMERIC(18,4),
    trend_up    BOOLEAN,
    regime      TEXT,
    source      TEXT        NOT NULL DEFAULT 'PIT',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
)
"""

_UPSERT = """
insert into research.signal_log
    (code, trade_date, signal, close, rsi, sma_short, sma_long, trend_up,
     regime, source)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (code, trade_date) do update set
    signal = excluded.signal,
    close = excluded.close,
    rsi = excluded.rsi,
    sma_short = excluded.sma_short,
    sma_long = excluded.sma_long,
    trend_up = excluded.trend_up,
    regime = excluded.regime,
    source = excluded.source,
    generated_at = now()
"""

_SELECT_LOG = """
select code, trade_date, signal, close, rsi, sma_short, sma_long, trend_up, regime
from research.signal_log
order by trade_date, code
"""

_LOG_COLUMNS = [
    "code", "date", "signal", "close", "rsi", "sma_short", "sma_long",
    "trend_up", "regime",
]


# --------------------------------------------------------------------------- #
# Deteksi sinyal historis (pure)
# --------------------------------------------------------------------------- #


def _recent_ex_div_cash(divs: list[tuple[date, float]], d: pd.Timestamp) -> float | None:
    """Cash dividend dari ex-date terakhir yang masih dalam window ``d``.

    ``divs`` diurutkan naik menurut ex_date. None bila tidak ada ex-date dalam
    ``DIV_GUARD_DAYS`` sebelum ``d`` (drop harga lama sudah tercerna tren).
    """
    last_ex: pd.Timestamp | None = None
    cash: float | None = None
    for ex_date, amount in divs:
        if pd.Timestamp(ex_date) > d:
            break
        last_ex = pd.Timestamp(ex_date)
        cash = float(amount) if amount and amount > 0 else None
    if last_ex is None or cash is None or (d - last_ex).days > DIV_GUARD_DAYS:
        return None
    return cash


def compute_signal_rows(
    panel: pd.DataFrame,
    min_history: int = MIN_HISTORY_DEFAULT,
    dividends: dict[str, list[tuple[date, float]]] | None = None,
    require_volume: bool = True,
) -> pd.DataFrame:
    """Hitung baris sinyal (BUY/SELL) dari panel harga PIT ``(code, date, close)``.

    Pure & testable — tidak menyentuh DB. ``panel`` diharapkan memakai
    ``adj_close`` sebagai ``close`` (split/bonus bebas distorsi).

    ``require_volume`` (default True) membuang sinyal yang jatuh di hari emiten
    **tidak diperdagangkan** (volume 0/NULL): harga di hari itu hanya carry-over,
    jadi sinyalnya artefak dari harga basi dan tidak bisa dieksekusi. Gate ini
    hanya berlaku bila panel menyertakan kolom ``volume`` — tanpa kolom itu
    tidak ada dasar untuk menilai likuiditas.

    Returns DataFrame kolom ``code, date, signal, close, rsi, sma_short,
    sma_long, trend_up`` — hanya sinyal non-HOLD setelah warmup, filter
    likuiditas, dan (untuk SELL) setelah filter SELL-palsu-ex-dividend.
    """
    if panel is None or panel.empty:
        return pd.DataFrame(columns=[*_LOG_COLUMNS[:8]])

    cols = ["code", "date", "close"] + (["volume"] if "volume" in panel.columns else [])
    df = panel[cols].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].sort_values(["code", "date"]).reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=[*_LOG_COLUMNS[:8]])

    has_volume = "volume" in df.columns
    out: list[dict[str, Any]] = []
    for code, grp in df.groupby("code", sort=False):
        grp = grp.sort_values("date").reset_index(drop=True)
        if len(grp) <= min_history:
            continue  # histori terlalu pendek: SMA50/RSI belum bermakna
        ind = calculate_indicators(grp[["date", "close"]])
        sig = signal_series(ind)
        divs = (dividends or {}).get(str(code).upper(), [])
        traded = grp["volume"].fillna(0) > 0 if has_volume else None

        mask = sig.isin(SIGNAL_TYPES)
        for pos in np.flatnonzero(mask.to_numpy()):
            if pos < min_history:
                continue  # warmup
            if require_volume and traded is not None and not bool(traded.iloc[pos]):
                continue  # hari tanpa transaksi: sinyal tidak actionable
            signal = str(sig.iloc[pos])
            if signal == "SELL" and divs:
                # SELL palsu: turun mekanis ex-dividend, bukan breakdown.
                win = ind.iloc[: pos + 1]
                if is_mechanical_sell(win, _recent_ex_div_cash(divs, ind["date"].iloc[pos])):
                    continue
            row = ind.iloc[pos]
            out.append(
                {
                    "code": str(code).upper(),
                    "date": row["date"],
                    "signal": signal,
                    "close": float(row["close"]),
                    "rsi": None if pd.isna(row["RSI"]) else round(float(row["RSI"]), 2),
                    "sma_short": None if pd.isna(row["MA_Short"]) else round(float(row["MA_Short"]), 4),
                    "sma_long": None if pd.isna(row["MA_Long"]) else round(float(row["MA_Long"]), 4),
                    "trend_up": bool(row["MA_Short"] > row["MA_Long"]),
                }
            )

    if not out:
        return pd.DataFrame(columns=[*_LOG_COLUMNS[:8]])
    return pd.DataFrame(out)


# --------------------------------------------------------------------------- #
# Loader DB
# --------------------------------------------------------------------------- #


def load_dividends(dsn: str) -> dict[str, list[tuple[date, float]]]:
    """Cash dividend per emiten ``{code: [(ex_date, cash), ...]}`` (urut naik).

    Hanya dipakai untuk filter SELL-palsu-ex-dividend; kosong bukan error.
    """
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """select code, ex_date, cash_amount
                   from research.corporate_actions
                   where action_type = 'dividend'
                     and cash_amount is not null and cash_amount > 0
                   order by code, ex_date"""
            )
        except Exception:
            return {}
        rows = cur.fetchall()
    out: dict[str, list[tuple[date, float]]] = {}
    for code, ex_date, cash in rows:
        out.setdefault(str(code).upper(), []).append((ex_date, float(cash)))
    return out


def load_regimes(dsn: str) -> dict[str, str]:
    """Peta ``{'YYYY-MM-DD': regime}`` dari research.regime_daily (kosong OK)."""
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "select trade_date, regime from research.regime_daily where regime is not null"
            )
        except Exception:
            return {}
        rows = cur.fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def load_log(dsn: str) -> pd.DataFrame:
    """Baca tabel log sebagai DataFrame dengan tipe siap-merge (date datetime64)."""
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(_DDL)  # idempoten: tabel selalu ada
        cur.execute(_SELECT_LOG)
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=_LOG_COLUMNS)
    df = pd.DataFrame(rows, columns=_LOG_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("close", "rsi", "sma_short", "sma_long"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# --------------------------------------------------------------------------- #
# Backfill / record
# --------------------------------------------------------------------------- #


def backfill(dsn: str, months: int = BACKFILL_MONTHS_DEFAULT, min_history: int = MIN_HISTORY_DEFAULT) -> int:
    """Hitung ulang histori sinyal dari layer PIT lalu tulis ulang (idempoten).

    Bersifat **recompute**: baris dalam rentang yang dihitung dihapus dulu lalu
    ditulis ulang satu transaksi. Ini penting karena definisi log bisa menajam
    (mis. filter likuiditas) — upsert saja akan menyisakan baris lama yang sudah
    tidak memenuhi definisi baru.

    Biaya dobel: ``months=1`` untuk job harian (cukup menutup koreksi telat),
    ``months=18`` untuk backfill manual.

    Returns jumlah baris yang ditulis.
    """
    from .ic import load_panel

    start = pd.Timestamp(datetime.now(WIB).date() - timedelta(days=int(months * 31)))
    # Hitung sinyal di SELURUH histori supaya warmup SMA50/RSI terpenuhi, baru
    # potong baris ke rentang yang diminta (bukan memotong harga lebih dulu).
    panel = load_panel(dsn, start=None, end=None, min_history=min_history)
    rows = compute_signal_rows(panel, min_history=min_history, dividends=load_dividends(dsn))
    if rows.empty:
        return 0
    rows = rows[rows["date"] >= start].copy()
    if rows.empty:
        return 0

    regimes = load_regimes(dsn)
    rows["regime"] = rows["date"].dt.strftime("%Y-%m-%d").map(regimes)

    written = 0
    # Satu transaksi: delete rentang + insert ulang, supaya gagal di tengah
    # tidak meninggalkan log bolong.
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        cur.execute("delete from research.signal_log where trade_date >= %s", (start.date(),))
        for r in rows.itertuples():
            cur.execute(
                _UPSERT,
                (
                    r.code,
                    r.date.date(),
                    r.signal,
                    r.close,
                    r.rsi,
                    r.sma_short,
                    r.sma_long,
                    bool(r.trend_up),
                    r.regime if isinstance(r.regime, str) else None,
                    "PIT",
                ),
            )
            written += 1
    return written


# --------------------------------------------------------------------------- #
# Evaluasi track record (pure transform + agregasi)
# --------------------------------------------------------------------------- #


def attach_outcomes(
    panel: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    nlags: int = NLAGS_DEFAULT,
) -> pd.DataFrame:
    """Panel -> outcome per baris: ``fwd_k``, ``mfe_k``, ``mae_k``, ``abn_k``.

    Entry di close T+``nlags``; exit di T+``nlags``+k (per emiten, hari bursa).
    - ``fwd_k`` : return entry->exit.
    - ``mfe_k`` : max favorable excursion — puncak close dalam window vs entry.
    - ``mae_k`` : max adverse excursion — dasar close dalam window vs entry.
    - ``abn_k`` : ``fwd_k`` dikurangi return pasar equal-weight window yang sama.

    Label masa depan di sini sengaja memakai ``shift(-n)`` — itu LABEL untuk
    mengukur sinyal, bukan input sinyal (lihat ic.py, aturan yang sama).
    """
    df = panel[["code", "date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code")["close"]
    entry = g.shift(-nlags)

    # Return pasar equal-weight per tanggal + kalender kumulatif (produk harian).
    daily = df.groupby("code")["close"].pct_change()
    mkt_daily = df.assign(_r=daily).groupby("date")["_r"].mean().sort_index()
    cum = (1.0 + mkt_daily.fillna(0.0)).cumprod()

    for k in horizons:
        exit_ = g.shift(-(nlags + k))
        df[f"fwd_{k}"] = exit_ / entry - 1.0
        roll_max = df.groupby("code")["close"].transform(lambda s, k=k: s.rolling(k + 1).max())
        roll_min = df.groupby("code")["close"].transform(lambda s, k=k: s.rolling(k + 1).min())
        win_max = roll_max.groupby(df["code"]).shift(-(nlags + k))
        win_min = roll_min.groupby(df["code"]).shift(-(nlags + k))
        df[f"mfe_{k}"] = win_max / entry - 1.0
        df[f"mae_{k}"] = win_min / entry - 1.0
        mkt_fwd = cum.shift(-(nlags + k)) / cum.shift(-nlags) - 1.0
        df[f"abn_{k}"] = df[f"fwd_{k}"] - df["date"].map(mkt_fwd)

    return df


def _summarize(sub: pd.DataFrame, signal: str, regime: str | None, horizon: int) -> dict[str, Any]:
    """Statistik satu irisan (signal, regime, horizon)."""
    fwd = sub[f"fwd_{horizon}"].dropna()
    base: dict[str, Any] = {
        "signal": signal,
        "regime": regime,
        "horizon": horizon,
        "n": len(fwd),
        "hit_rate": None,
        "mean_fwd": None,
        "median_fwd": None,
        "mean_abnormal": None,
        "t_stat": None,
        "avg_mfe": None,
        "avg_mae": None,
    }
    if fwd.empty:
        return base
    abn = sub[f"abn_{horizon}"].dropna()
    base.update(
        {
            "hit_rate": round(float((fwd > 0).mean()), 4),
            "mean_fwd": round(float(fwd.mean()), 6),
            "median_fwd": round(float(fwd.median()), 6),
            "mean_abnormal": round(float(abn.mean()), 6) if not abn.empty else None,
            "t_stat": _round_opt(_nw_tstat(fwd)),
            "avg_mfe": _round_opt(sub[f"mfe_{horizon}"].mean()),
            "avg_mae": _round_opt(sub[f"mae_{horizon}"].mean()),
        }
    )
    return base


def _round_opt(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, 6)


def summarize_outcomes(
    log: pd.DataFrame,
    outcomes: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    recent_limit: int = 40,
) -> dict[str, Any]:
    """Gabungkan log sinyal dengan outcome lalu agregasi per signal/regime/horizon.

    Pure & testable. Returns dict siap-JSON (dipakai API & CLI).
    """
    if log.empty:
        return {
            "signals": 0,
            "first_date": None,
            "last_date": None,
            "horizons": list(horizons),
            "overall": [],
            "by_regime": [],
            "recent": [],
        }

    cols = ["code", "date", *[f"{p}_{k}" for k in horizons for p in ("fwd", "mfe", "mae", "abn")]]
    merged = log.merge(outcomes[cols], on=["code", "date"], how="left")
    merged["regime"] = merged["regime"].fillna("TIDAK DIKETAHUI")

    overall: list[dict[str, Any]] = []
    by_regime: list[dict[str, Any]] = []
    for h in horizons:
        for sig in SIGNAL_TYPES:
            sub = merged[merged["signal"] == sig]
            overall.append(_summarize(sub, sig, None, h))
            for regime, grp in sub.groupby("regime"):
                by_regime.append(_summarize(grp, sig, str(regime), h))

    recent_cols = ["code", "date", "signal", "close"]
    recent = [
        {
            "code": r.code,
            "date": pd.Timestamp(r.date).strftime("%Y-%m-%d"),
            "signal": r.signal,
            "close": _round_opt(r.close),
            **{f"fwd_{k}": _round_opt(getattr(r, f"fwd_{k}")) for k in horizons},
        }
        for r in merged[[*recent_cols, *[f"fwd_{k}" for k in horizons]]]
        .sort_values("date", ascending=False)
        .head(recent_limit)
        .itertuples()
    ]

    dates = pd.to_datetime(merged["date"])
    return {
        "signals": len(merged),
        "buy": int((merged["signal"] == "BUY").sum()),
        "sell": int((merged["signal"] == "SELL").sum()),
        "first_date": dates.min().strftime("%Y-%m-%d"),
        "last_date": dates.max().strftime("%Y-%m-%d"),
        "horizons": list(horizons),
        "overall": overall,
        "by_regime": by_regime,
        "recent": recent,
    }


def evaluate(
    dsn: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    recent_limit: int = 40,
) -> dict[str, Any]:
    """Baca log + panel PIT, hitung outcome, kembalikan ringkasan track record."""
    from .ic import load_panel

    log = load_log(dsn)
    if log.empty:
        return summarize_outcomes(log, pd.DataFrame(), horizons, recent_limit)
    panel = load_panel(dsn, min_history=1)
    outcomes = attach_outcomes(panel, horizons=horizons)
    return summarize_outcomes(log, outcomes, horizons, recent_limit)


def record_recent(dsn: str, months: int = 1) -> int:
    """Job harian: hitung ulang window pendek (default 1 bulan) + kembalikan jumlah."""
    return backfill(dsn, months=months)


def record_and_report(dsn: str, months: int = BACKFILL_MONTHS_DEFAULT) -> dict[str, Any]:
    """Orchestrator: backfill histori + kembalikan ringkasan (dipakai CLI/job)."""
    written = backfill(dsn, months=months)
    report = evaluate(dsn)
    report["written"] = written
    return report


def print_report(r: dict[str, Any]) -> None:
    print("=" * 78)
    print("JEJAK SINYAL — track record BUY/SELL (PIT, entry T+1, vs pasar)")
    print("=" * 78)
    if not r.get("signals"):
        print("  (belum ada sinyal tercatat — jalankan backfill)")
        return
    print(f"  {r['signals']} sinyal ({r['buy']} BUY / {r['sell']} SELL)"
          f"  {r['first_date']} -> {r['last_date']}")
    for h in r["horizons"]:
        print(f"\n--- horizon {h} hari bursa ---")
        for row in r["overall"]:
            if row["horizon"] != h or row["n"] == 0:
                continue
            print(
                f"  {row['signal']:<5} n={row['n']:<5} hit={row['hit_rate']:.0%}  "
                f"mean={row['mean_fwd']:+.2%}  median={row['median_fwd']:+.2%}  "
                f"abn={row['mean_abnormal']:+.2%}"
                if row["mean_abnormal"] is not None
                else f"  {row['signal']:<5} n={row['n']:<5} hit={row['hit_rate']:.0%}  "
                f"mean={row['mean_fwd']:+.2%}"
            )
    print("\nCatatan: hit-rate/men return historis indikatif, bukan jaminan.")
    print("SELL yang dinilai di sini adalah sinyal keluar untuk posisi lama.")


def main() -> int:
    ap = argparse.ArgumentParser(prog="signal_log")
    ap.add_argument("--months", type=int, default=BACKFILL_MONTHS_DEFAULT)
    ap.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=__import__("sys").stderr)
        return 1
    written = backfill(dsn, months=args.months)
    print(f"[ok] {written} baris sinyal ditulis")
    print_report(evaluate(dsn, horizons=tuple(args.k)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
