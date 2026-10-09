"""Jejak rekomendasi — log harian kandidat beli + track record per grade.

Menjawab pertanyaan yang membuat fitur rekomendasi layak dipercaya: **"kalau
tiap hari saya ikut daftar kandidat beli ini, hasilnya bagaimana?"**

Prinsip (sama dengan ``research.signal_log`` dan ``smart_money`` track record):

1. **Point-in-time.** Metrik di tanggal T hanya dari baris ``<= T`` (semua
   rolling backward; pola memakai ``smart_money.pattern_flags`` — satu sumber
   kebenaran yang sama dengan banner emiten).
2. **Eksekusi T+1.** Penilaian memakai entry di close T+1 (``nlags=1``),
   konsisten dengan IC analysis, jejak sinyal, dan backtest — bukan close hari
   kandidat.
3. **Abnormal vs pasar.** Return window yang sama dibandingkan equal-weight
   seluruh pasar (dari panel itu sendiri, PIT-safe).
4. **Idempoten per (code, trade_date).** Run ulang menimpa baris lama; backfill
   menghitung ulang rentangnya (bukan menyisakan baris yang sudah tidak
   memenuhi definisi baru).
5. **Panel = ``research.latest_pit``** (tabel yang sama dengan radar / smart
   money / screener), supaya angka di daftar kandidat tidak pernah berbeda dari
   halaman lain. Catatan jujur: panel ini harga **raw** (tidak disesuaikan
   aksi korporasi) — keterbatasan yang sama dengan track record smart money;
   ``signal_log`` memakai panel adjusted terpisah.

Tabel ``research.recommendation_log`` sengaja HANYA menyimpan kandidat yang
lolos grade C ke atas; "hari kandidat" adalah definisinya.

Contoh pakai:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.recommendation_log \\
        --months 6
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import psycopg
from psycopg.rows import dict_row

from ..analysis import calculate_indicators, signal_series
from ..smart_money import PRICE_WINDOW, pattern_flags
from .recommend import assign_grades, score_candidate
from .signal_log import DEFAULT_HORIZONS, NLAGS_DEFAULT, attach_outcomes

# --- parameter default ------------------------------------------------------
SESSION_LOOKBACK = 300      # sesi ditarik untuk rolling 252 + warmup
BACKFILL_MONTHS_DEFAULT = 6
MIN_HISTORY_DEFAULT = 60

WIB = timezone(timedelta(hours=7))

_DDL = """
create table if not exists research.recommendation_log (
    code         TEXT        NOT NULL,
    trade_date   DATE        NOT NULL,
    grade        TEXT        NOT NULL CHECK (grade IN ('A','B','C')),
    score        NUMERIC(6,2),
    signal       TEXT,
    setup        TEXT,
    close        NUMERIC(18,4),
    entry_ref    NUMERIC(18,4),
    stop         NUMERIC(18,4),
    target       NUMERIC(18,4),
    rr           NUMERIC(8,2),
    horizon_days INTEGER,
    patterns     TEXT,
    source       TEXT        NOT NULL DEFAULT 'PIT',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
)
"""

_UPSERT = """
insert into research.recommendation_log
    (code, trade_date, grade, score, signal, setup, close, entry_ref, stop,
     target, rr, horizon_days, patterns, source)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (code, trade_date) do update set
    grade = excluded.grade,
    score = excluded.score,
    signal = excluded.signal,
    setup = excluded.setup,
    close = excluded.close,
    entry_ref = excluded.entry_ref,
    stop = excluded.stop,
    target = excluded.target,
    rr = excluded.rr,
    horizon_days = excluded.horizon_days,
    patterns = excluded.patterns,
    source = excluded.source,
    generated_at = now()
"""

_SELECT_LOG = """
select code, trade_date, grade, score, signal, setup, close, entry_ref, stop,
       target, rr, horizon_days, patterns
from research.recommendation_log
order by trade_date, code
"""

_LOG_COLUMNS = [
    "code", "date", "grade", "score", "signal", "setup", "close", "entry_ref",
    "stop", "target", "rr", "horizon_days", "patterns",
]


# --------------------------------------------------------------------------- #
# Panel loader
# --------------------------------------------------------------------------- #


def load_candidate_panel(dsn: str, sessions: int = SESSION_LOOKBACK) -> dict[str, dict[str, Any]]:
    """Panel baris harian per emiten untuk ``sessions`` sesi terakhir (satu query).

    Bentuk yang sama dengan ``api.analytics._smart_money_panel`` supaya definisi
    metrik (dan karenanya pola) tidak berbeda antar halaman: baris berisi
    ``date, close, high, low, volume, value, net_idr`` (net asing dalam rupiah =
    ``foreign_net * close``), urut tanggal menaik per emiten.
    """
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=15, row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """select code, name, trade_date as date,
                      foreign_net * close as net_idr,
                      value, close, high, low, volume
               from research.latest_pit
               where trade_date in (
                   select distinct trade_date from research.latest_pit
                   order by 1 desc limit %s
               )
                 and close is not null
               order by code, trade_date""",
            (sessions,),
        )
        panel: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        for r in cur.fetchall():
            code = str(r["code"]).upper()
            names.setdefault(code, r["name"])
            panel.setdefault(code, []).append(
                {
                    "date": str(r["date"])[:10],
                    "net_idr": _num(r["net_idr"]),
                    "value": _num(r["value"]),
                    "close": _num(r["close"]),
                    "high": _num(r["high"]),
                    "low": _num(r["low"]),
                    "volume": _num(r["volume"]),
                }
            )
    return {c: {"name": names.get(c), "rows": rows} for c, rows in panel.items()}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# --------------------------------------------------------------------------- #
# Metrik per emiten (pure)
# --------------------------------------------------------------------------- #


def _code_metrics(code: str, name: str | None, rows: list[dict[str, Any]]) -> pd.DataFrame | None:
    """Metrik satu emiten per baris — semuanya backward-looking (pure).

    Returns DataFrame (baris = sesi) kolom: ``code, name, date, close, value,
    vol_daily, mom_20d, dist_52w_pct, vol_ratio, bb_lower, range_low_20,
    z_score, hist_days, signal, trend_up, pattern_buy, pattern_sell``.
    """
    if len(rows) < 2:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in ("close", "high", "low", "volume", "value", "net_idr"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].reset_index(drop=True)
    if len(df) < 2:
        return None

    # Indikator + sinyal memakai fungsi yang SAMA dengan dashboard.
    ind = calculate_indicators(df[["date", "close"]].copy())
    sig = signal_series(ind)

    close = df["close"]
    ret = close.pct_change()
    vol_daily = ret.rolling(21).std()
    mom20 = close.pct_change(20) * 100.0
    hi252 = close.rolling(252, min_periods=63).max()
    dist_52w = close / hi252.where(hi252 > 0) - 1.0
    vol_mean20 = df["volume"].rolling(20).mean()
    vol_ratio = df["volume"] / vol_mean20.where(vol_mean20 > 0)

    # Metrik pola (definisi syarat tetap di smart_money.pattern_flags).
    roll10 = close.rolling(PRICE_WINDOW)
    range10 = (roll10.max() - roll10.min()) / roll10.mean().where(lambda s: s > 0) * 100.0
    chg10 = close.pct_change(PRICE_WINDOW) * 100.0
    net_sum10 = df["net_idr"].rolling(PRICE_WINDOW).sum()
    val_sum10 = df["value"].rolling(PRICE_WINDOW).sum()
    netval10 = net_sum10 / val_sum10.where(val_sum10 > 0) * 100.0

    mean60 = close.rolling(60).mean()
    std60 = close.rolling(60).std()
    z = (close - mean60) / std60.where(std60 > 0)

    buy_ids: list[tuple[str, ...]] = []
    sell_ids: list[tuple[str, ...]] = []
    for i in range(len(df)):
        flags = pattern_flags(
            _num(range10.iloc[i]),
            _num(chg10.iloc[i]),
            _num(vol_ratio.iloc[i]),
            _num(netval10.iloc[i]),
        )
        buy_ids.append(
            tuple(
                pid
                for pid in ("silent_accumulation", "initiation")
                if flags.get(pid)
            )
        )
        sell_ids.append(
            tuple(
                pid
                for pid in ("distribution_on_rally", "silent_distribution")
                if flags.get(pid)
            )
        )

    out = pd.DataFrame(
        {
            "code": str(code).upper(),
            "name": name,
            "date": df["date"],
            "close": close,
            "value": df["value"],
            "vol_daily": vol_daily,
            "mom_20d": mom20,
            "dist_52w_pct": dist_52w * 100.0,
            "vol_ratio": vol_ratio,
            "bb_lower": ind["BB_Lower"],
            "rsi": ind["RSI"],
            "range_low_20": close.rolling(20).min(),
            "z_score": z.round(2),
            "signal": sig,
            "trend_up": (ind["MA_Short"] > ind["MA_Long"]),
            "pattern_buy": buy_ids,
            "pattern_sell": sell_ids,
        }
    )
    out["hist_days"] = np.arange(1, len(out) + 1)
    return out


def metrics_frame(panel: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Gabungkan metrik semua emiten jadi satu DataFrame (pure, tanpa DB)."""
    frames: list[pd.DataFrame] = []
    for code, entry in panel.items():
        rows = entry.get("rows") if isinstance(entry, dict) else None
        if not rows:
            continue
        m = _code_metrics(code, (entry or {}).get("name"), rows)
        if m is not None and not m.empty:
            frames.append(m)
    if not frames:
        return pd.DataFrame(
            columns=[
                "code", "name", "date", "close", "signal", "trend_up", "rsi",
                "mom_20d", "vol_daily", "dist_52w_pct", "vol_ratio", "bb_lower",
                "range_low_20", "value", "z_score", "hist_days",
                "pattern_buy", "pattern_sell",
            ]
        )
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Kandidat per tanggal (pure)
# --------------------------------------------------------------------------- #


def candidates_for_date(
    metrics: pd.DataFrame,
    date_value: Any,
    *,
    histories: dict[str, dict[str, Any]] | None = None,
    factor_adj_by_code: dict[str, float] | None = None,
    factor_validated: bool = False,
    broker_adj_by_code: dict[str, float] | None = None,
    broker_validated: bool = False,
    horizon: int | None = None,
) -> list[dict[str, Any]]:
    """Semua kandidat beli pada satu tanggal (pure) — hasil ``score_candidate``.

    ``histories`` = peta ``pattern_id -> smart_money.pattern_history`` (bukti
    pola, untuk alasan/peringatan). ``factor_adj_by_code`` /
    ``broker_adj_by_code`` hanya dipakai bila bendera ``*_validated`` True —
    sejalan dengan pilihan "hanya lapisan tervalidasi yang boleh menggerakkan
    skor".

    Grade ditentukan di sini lewat ``recommend.assign_grades``: peringkat relatif
    pool hari itu (C 25% · B 10% · A 2% teratas). Kandidat di luar 25% teratas
    tidak dikembalikan — itu definisi "masuk papan".
    """
    if metrics.empty:
        return []
    target = pd.Timestamp(date_value)
    day = metrics[metrics["date"] == target]
    if day.empty:
        return []

    fac = factor_adj_by_code or {}
    brk = broker_adj_by_code or {}
    out: list[dict[str, Any]] = []
    for row in day.itertuples(index=False):
        m = {
            "code": row.code,
            "name": row.name,
            "close": row.close,
            "signal": row.signal,
            "trend_up": bool(row.trend_up),
            "rsi": _num(row.rsi),
            "mom_20d": _num(row.mom_20d),
            "vol_daily": _num(row.vol_daily),
            "dist_52w_pct": _num(row.dist_52w_pct),
            "vol_ratio": _num(row.vol_ratio),
            "bb_lower": _num(row.bb_lower),
            "range_low_20": _num(row.range_low_20),
            "value": _num(row.value),
            "z_score": _num(row.z_score),
            "hist_days": int(row.hist_days),
            "pattern_buy": tuple(row.pattern_buy or ()),
            "pattern_sell": tuple(row.pattern_sell or ()),
        }
        scored = score_candidate(
            m,
            histories=histories,
            horizon=horizon,
            factor_adj=float(fac.get(str(row.code).upper(), 0.0)),
            factor_validated=factor_validated,
            broker_adj=float(brk.get(str(row.code).upper(), 0.0)),
            broker_validated=broker_validated,
        )
        if scored is not None:
            out.append(scored)
    return assign_grades(out)


def build_records(
    metrics: pd.DataFrame,
    *,
    histories: dict[str, dict[str, Any]] | None = None,
    months: int = BACKFILL_MONTHS_DEFAULT,
    factor_adj_by_code: dict[str, float] | None = None,
    factor_validated: bool = False,
    broker_adj_by_code: dict[str, float] | None = None,
    broker_validated: bool = False,
    horizon: int | None = None,
) -> list[dict[str, Any]]:
    """Kandidat untuk semua tanggal dalam rentang ``months`` terakhir (pure)."""
    if metrics.empty:
        return []
    cutoff = pd.Timestamp(datetime.now(WIB).date() - timedelta(days=int(months * 31)))
    dates = sorted(d for d in metrics["date"].unique() if pd.Timestamp(d) >= cutoff)
    records: list[dict[str, Any]] = []
    for d in dates:
        for c in candidates_for_date(
            metrics,
            d,
            histories=histories,
            factor_adj_by_code=factor_adj_by_code,
            factor_validated=factor_validated,
            broker_adj_by_code=broker_adj_by_code,
            broker_validated=broker_validated,
            horizon=horizon,
        ):
            records.append(_to_log_row(c, pd.Timestamp(d)))
    return records


def _to_log_row(candidate: dict[str, Any], when: pd.Timestamp) -> dict[str, Any]:
    return {
        "code": candidate["code"],
        "date": when.date(),
        "grade": candidate["grade"],
        "score": candidate["score"],
        "signal": (candidate.get("metrics") or {}).get("signal"),
        "setup": candidate.get("setup"),
        "close": (candidate.get("metrics") or {}).get("close"),
        "entry_ref": candidate.get("entry_ref"),
        "stop": candidate.get("stop"),
        "target": candidate.get("target"),
        "rr": candidate.get("rr"),
        "horizon_days": candidate.get("horizon_days"),
        "patterns": ",".join(candidate.get("patterns") or []) or None,
    }


# --------------------------------------------------------------------------- #
# Persistensi
# --------------------------------------------------------------------------- #


def load_log(dsn: str) -> pd.DataFrame:
    """Baca tabel log sebagai DataFrame (date datetime64 siap-merge)."""
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(_DDL)  # idempoten: tabel selalu ada
        cur.execute(_SELECT_LOG)
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=_LOG_COLUMNS)
    df = pd.DataFrame(rows, columns=_LOG_COLUMNS)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("score", "close", "entry_ref", "stop", "target", "rr"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def record(
    dsn: str,
    *,
    months: int = BACKFILL_MONTHS_DEFAULT,
    sessions: int = SESSION_LOOKBACK,
    histories: dict[str, dict[str, Any]] | None = None,
    factor_adj_by_code: dict[str, float] | None = None,
    factor_validated: bool = False,
    broker_adj_by_code: dict[str, float] | None = None,
    broker_validated: bool = False,
    horizon: int | None = None,
) -> int:
    """Hitung ulang kandidat rentang ``months`` terakhir lalu tulis (idempoten).

    Recompute (delete rentang + insert dalam satu transaksi): definisi skor bisa
    menajam, dan upsert saja akan menyisakan baris lama yang sudah tidak
    memenuhi definisi baru — pola yang sama dengan ``signal_log.backfill``.
    """
    panel = load_candidate_panel(dsn, sessions=sessions)
    metrics = metrics_frame(panel)
    records = build_records(
        metrics,
        histories=histories,
        months=months,
        factor_adj_by_code=factor_adj_by_code,
        factor_validated=factor_validated,
        broker_adj_by_code=broker_adj_by_code,
        broker_validated=broker_validated,
        horizon=horizon,
    )
    cutoff = (datetime.now(WIB).date() - timedelta(days=int(months * 31)))
    with psycopg.connect(dsn, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        cur.execute("delete from research.recommendation_log where trade_date >= %s", (cutoff,))
        for r in records:
            cur.execute(
                _UPSERT,
                (
                    r["code"], r["date"], r["grade"], r["score"], r["signal"],
                    r["setup"], r["close"], r["entry_ref"], r["stop"], r["target"],
                    r["rr"], r["horizon_days"], r["patterns"], "PIT",
                ),
            )
    return len(records)


def record_recent(dsn: str, months: int = 1, horizon: int | None = None) -> int:
    """Job harian: recompute window pendek (default 1 bulan)."""
    return record(dsn, months=months, horizon=horizon)


# --------------------------------------------------------------------------- #
# Evaluasi track record (pure transform + agregasi)
# --------------------------------------------------------------------------- #


def _summarize(sub: pd.DataFrame, grade: str, horizon: int) -> dict[str, Any]:
    """Statistik satu irisan (grade, horizon)."""
    fwd = sub[f"fwd_{horizon}"].dropna()
    base: dict[str, Any] = {
        "grade": grade,
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
            "t_stat": _round_opt(_nw(sub[f"fwd_{horizon}"])),
            "avg_mfe": _round_opt(sub[f"mfe_{horizon}"].mean()),
            "avg_mae": _round_opt(sub[f"mae_{horizon}"].mean()),
        }
    )
    return base


def _nw(values: pd.Series) -> float | None:
    try:
        from .events import _nw_tstat

        return _nw_tstat(values.dropna())
    except Exception:
        return None


def _round_opt(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, 6)


def summarize_log(
    log: pd.DataFrame,
    outcomes: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    recent_limit: int = 30,
) -> dict[str, Any]:
    """Gabungkan log kandidat dengan outcome lalu agregasi per grade/horizon (pure)."""
    if log.empty:
        return {
            "candidates": 0,
            "first_date": None,
            "last_date": None,
            "horizons": list(horizons),
            "by_grade": [],
            "recent": [],
        }

    cols = [
        "code", "date",
        *[f"{p}_{k}" for k in horizons for p in ("fwd", "mfe", "mae", "abn")],
    ]
    merged = log.merge(outcomes[cols], on=["code", "date"], how="left")

    by_grade: list[dict[str, Any]] = []
    for h in horizons:
        for grade in ("A", "B", "C"):
            by_grade.append(_summarize(merged[merged["grade"] == grade], grade, h))

    recent = [
        {
            "code": r.code,
            "date": pd.Timestamp(r.date).strftime("%Y-%m-%d"),
            "grade": r.grade,
            "score": _round_opt(r.score),
            "close": _round_opt(r.close),
            **{f"fwd_{k}": _round_opt(getattr(r, f"fwd_{k}")) for k in horizons},
        }
        for r in merged[
            ["code", "date", "grade", "score", "close", *[f"fwd_{k}" for k in horizons]]
        ]
        .sort_values("date", ascending=False)
        .head(recent_limit)
        .itertuples()
    ]

    dates = pd.to_datetime(merged["date"])
    counts = merged["grade"].value_counts()
    return {
        "candidates": len(merged),
        "grade_a": int(counts.get("A", 0)),
        "grade_b": int(counts.get("B", 0)),
        "grade_c": int(counts.get("C", 0)),
        "first_date": dates.min().strftime("%Y-%m-%d"),
        "last_date": dates.max().strftime("%Y-%m-%d"),
        "horizons": list(horizons),
        "by_grade": by_grade,
        "recent": recent,
    }


def evaluate(
    dsn: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    recent_limit: int = 30,
    sessions: int = SESSION_LOOKBACK,
) -> dict[str, Any]:
    """Baca log + panel, hitung outcome, kembalikan ringkasan track record."""
    log = load_log(dsn)
    if log.empty:
        return summarize_log(log, pd.DataFrame(), horizons, recent_limit)
    panel = load_candidate_panel(dsn, sessions=sessions)
    panel_df = pd.DataFrame(
        [
            {"code": code, "date": row["date"], "close": row["close"]}
            for code, entry in panel.items()
            for row in entry["rows"]
            if row.get("close") is not None
        ]
    )
    if panel_df.empty:
        return summarize_log(log, pd.DataFrame(), horizons, recent_limit)
    panel_df["date"] = pd.to_datetime(panel_df["date"])
    outcomes = attach_outcomes(panel_df, horizons=horizons, nlags=NLAGS_DEFAULT)
    return summarize_log(log, outcomes, horizons, recent_limit)


def record_and_report(dsn: str, months: int = BACKFILL_MONTHS_DEFAULT) -> dict[str, Any]:
    """Orchestrator: isi log + kembalikan ringkasan (dipakai CLI/job)."""
    written = record(dsn, months=months)
    report = evaluate(dsn)
    report["written"] = written
    return report


def print_report(r: dict[str, Any]) -> None:
    print("=" * 78)
    print("REKOMENDASI BELI — track record kandidat (PIT, entry T+1, vs pasar)")
    print("=" * 78)
    if not r.get("candidates"):
        print("  (belum ada kandidat tercatat — jalankan backfill)")
        return
    print(
        f"  {r['candidates']} kandidat "
        f"({r.get('grade_a', 0)} A / {r.get('grade_b', 0)} B / {r.get('grade_c', 0)} C)"
        f"  {r['first_date']} -> {r['last_date']}"
    )
    for h in r["horizons"]:
        print(f"\n--- horizon {h} hari bursa ---")
        for row in r["by_grade"]:
            if row["horizon"] != h or row["n"] == 0:
                continue
            abn = row["mean_abnormal"]
            abn_s = f"  abn={abn:+.2%}" if abn is not None else ""
            print(
                f"  grade {row['grade']}  n={row['n']:<5} hit={row['hit_rate']:.0%}  "
                f"mean={row['mean_fwd']:+.2%}{abn_s}"
            )
    print("\nCatatan: hit-rate/mean return historis indikatif, bukan jaminan.")
    print("Kandidat tanpa level eksekusi yang bisa dihitung tidak masuk log.")


def main() -> int:
    ap = argparse.ArgumentParser(prog="recommendation_log")
    ap.add_argument("--months", type=int, default=BACKFILL_MONTHS_DEFAULT)
    ap.add_argument("--k", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=sys.stderr)
        return 1
    written = record(dsn, months=args.months)
    print(f"[ok] {written} baris kandidat ditulis")
    print_report(evaluate(dsn, horizons=tuple(args.k)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
