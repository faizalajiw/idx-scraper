"""Event study engine — ukur dampak rata-rata suatu event terhadap return.

Menjawab pertanyaan riset seperti:
    "Saham yang naik >3% sehari dengan volume >= 2x rata-rata 21 hari,
     secara statistik habisnya bagaimana selama 21 hari ke depan?"

Semua deteksi point-in-time (faktor backward-looking, no look-ahead):
- Event di (code, T) hanya dari data <= T.
- Forward return memakai entry T+1 close (disiplin backtest; konsisten ic.py).
- Dedup event per emiten dengan jarak minimum ``min_gap`` hari bursa supaya
  episode yang sama tidak dihitung berkali-kali.
- Abnormal return = return emiten - return pasar (equal-weight, hari sama),
  dengan return pasar dari panel itu sendiri (breadth pasar, PIT-safe).

Contoh:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.events \\
        --event jump_up --gap 10
"""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Preset event (semua backward-looking)
# --------------------------------------------------------------------------- #

EVENT_PRESETS: dict[str, dict] = {
    "jump_up": {
        "description": "Hari ini naik >= 3% dengan volume >= 2x rata-rata 21 hari",
        "fn": "jump", "ret": 0.03, "volx": 2.0,
    },
    "jump_down": {
        "description": "Hari ini turun <= -3% dengan volume >= 2x rata-rata 21 hari",
        "fn": "jump", "ret": -0.03, "volx": 2.0,
    },
    "vol_spike": {
        "description": "Volume hari ini >= 3x rata-rata 21 hari (tanpa syarat arah)",
        "fn": "spike", "volx": 3.0,
    },
    "near_high": {
        "description": "Menutup dekat puncak 63 hari (<= 2% di bawahnya)",
        "fn": "near_high", "max_dist": 0.02, "window": 63,
    },
    "ma_cross_up": {
        "description": "SMA20 memotong ke atas SMA50 hari ini",
        "fn": "cross", "short": 20, "long": 50,
    },
    "ma_cross_down": {
        "description": "SMA20 memotong ke bawah SMA50 hari ini",
        "fn": "cross", "short": 20, "long": 50, "down": True,
    },
}


def _event_mask(panel: pd.DataFrame, name: str) -> pd.Series:
    """Boolean Series (index selaras panel) = True di baris event."""
    df = panel.sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code")
    preset = EVENT_PRESETS[name]

    if preset["fn"] == "jump":
        ret = g["close"].pct_change()
        vol_mean = g["volume"].transform(lambda s: s.rolling(21).mean())
        volx_ok = df["volume"] >= preset["volx"] * vol_mean
        cond = (ret >= preset["ret"]) if preset["ret"] > 0 else (ret <= preset["ret"])
        mask = cond & volx_ok & ret.notna()

    elif preset["fn"] == "spike":
        vol_mean = g["volume"].transform(lambda s: s.rolling(21).mean())
        mask = (df["volume"] >= preset["volx"] * vol_mean) & vol_mean.notna()

    elif preset["fn"] == "near_high":
        high = g["close"].transform(lambda s: s.rolling(preset["window"]).max())
        mask = df["close"] >= (1.0 - preset["max_dist"]) * high

    elif preset["fn"] == "cross":
        short = g["close"].transform(lambda s: s.rolling(preset["short"]).mean())
        long = g["close"].transform(lambda s: s.rolling(preset["long"]).mean())
        diff = short - long
        prev = diff.groupby(df["code"]).shift(1)  # diff hari sebelumnya, per emiten
        if preset.get("down"):
            mask = (diff < 0) & (prev >= 0)
        else:
            mask = (diff > 0) & (prev <= 0)

    else:  # pragma: no cover - preset baru harus didefinisikan di atas
        raise ValueError(f"preset tidak dikenal: {name}")

    return mask.fillna(False)


def detect_events(
    panel: pd.DataFrame,
    event: str,
    min_gap: int = 10,
    params: dict | None = None,
) -> pd.DataFrame:
    """Deteksi baris event per emiten dengan jarak minimum antar event.

    Returns DataFrame ``(code, date, event)``. ``params`` boleh mengoverride
    parameter preset (mis. ``{"ret": 0.05}`` untuk jump 5%).
    """
    if params:
        EVENT_PRESETS[event] = {**EVENT_PRESETS[event], **params}
    df = panel.sort_values(["code", "date"]).reset_index(drop=True)
    mask = _event_mask(df, event)

    out = df.loc[mask, ["code", "date"]].copy()
    out["event"] = event

    # Dedup per emiten: buang event yang terlalu dekat dengan event sebelumnya.
    if min_gap > 1 and not out.empty:
        out = out.sort_values(["code", "date"])
        dates = pd.to_datetime(out["date"])
        gap = dates.groupby(out["code"]).diff()
        # ambang dalam HARI KALENDER (~1.4x hari bursa)
        out = out[(gap.isna()) | (gap >= pd.Timedelta(days=int(min_gap * 1.4)))]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Forward / abnormal return + kurva event-time
# --------------------------------------------------------------------------- #


@dataclass
class EventStudyResult:
    event: str
    n_events: int
    horizon: int
    mean_fwd: float | None
    median_fwd: float | None
    hit_rate: float | None
    t_stat: float | None          # Newey-West lag 5 (event tumpang tindih)
    mean_abnormal: float | None   # vs pasar equal-weight hari yang sama
    t_abnormal: float | None
    curve: pd.DataFrame           # kolom: offset, mean_fwd, mean_abnormal, n


def _nw_tstat(x: pd.Series) -> float | None:
    """t-stat mean dengan Newey-West lag 5 (idem ic.py)."""
    x = x.dropna()
    if len(x) < 5:
        return None
    mean = x.mean()
    d = x - mean
    T = len(d)
    var = (d * d).mean()
    for l in range(1, min(5, T - 1) + 1):
        cov = (d.iloc[l:] * d.iloc[:-l].values).mean()
        var += 2.0 * (1.0 - l / 6.0) * cov
    if var <= 0:
        return math.inf if mean > 0 else (-math.inf if mean < 0 else None)
    return float(mean / math.sqrt(var / T))


def event_forward_returns(
    panel: pd.DataFrame,
    events: pd.DataFrame,
    horizon: int = 21,
    market: bool = True,
) -> pd.DataFrame:
    """Untuk tiap event: fwd return T+1 -> T+1+h (entry close T+1).

    ``abnormal`` = fwd - return pasar (equal-weight semua emiten di panel)
    pada window yang sama. Join strict on date emiten; event tanpa cukup
    data ke depan di-drop.
    """
    df = panel.sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code")
    n_in = g["code"].transform("size")
    pos = df.groupby("code").cumcount()
    entry_pos, exit_pos = pos + 1, pos + 1 + horizon

    close_entry = _at_pos(df, "close", entry_pos)
    close_exit = _at_pos(df, "close", exit_pos)
    valid = (exit_pos < n_in) & close_entry.notna() & (close_entry > 0)
    df["_fwd"] = (close_exit / close_entry - 1.0).where(valid)

    # Return pasar equal-weight per tanggal (dari panel itu sendiri).
    daily_ret = df.groupby("code")["close"].pct_change()
    mkt_daily = df.assign(_r=daily_ret).groupby("date")["_r"].mean()
    df["_mkt"] = df["date"].map(mkt_daily)

    # Kumulatif pasar antar posisi kalender via compounding harian.
    # Return emiten: close[entry=e] -> close[exit=x], e=i+1, x=i+1+horizon
    # (= horizon pieces return harian). Return pasar window yang SAMA:
    # cum[x] / cum[e] - 1, dengan cum = produk (1+r) s.d. posisi itu.
    cal = df[["date", "_mkt"]].drop_duplicates("date").sort_values("date").reset_index(drop=True)
    cal["_cum"] = (1.0 + cal["_mkt"].fillna(0.0)).cumprod()
    date_pos = {d: i for i, d in enumerate(cal["date"])}

    ev = events.copy()
    ev["_i"] = ev["date"].map(date_pos)

    def _mkt_window_cum(i: int) -> float:
        if pd.isna(i) or i + 1 + horizon >= len(cal):
            return np.nan
        e, x = i + 1, i + 1 + horizon
        return float(cal["_cum"].iloc[x] / cal["_cum"].iloc[e] - 1.0)

    ev["_mkt_fwd"] = ev["_i"].apply(_mkt_window_cum)

    ev = ev.merge(df[["code", "date", "_fwd"]], on=["code", "date"], how="left")
    ev = ev[ev["_fwd"].notna()].copy()
    ev["fwd"] = ev["_fwd"]
    ev["abnormal"] = np.nan
    if market:
        ev["abnormal"] = ev["fwd"] - ev["_mkt_fwd"]
        ev = ev[ev["abnormal"].notna()]
    return ev[["code", "date", "event", "fwd", "abnormal"]].reset_index(drop=True)


def _at_pos(df: pd.DataFrame, col: str, target: pd.Series) -> pd.Series:
    """Nilai `col` pada posisi per-emiten `target` (NaN bila di luar jangkauan)."""
    tmp = df[["code", col]].copy()
    tmp["_pos"] = df.groupby("code").cumcount()
    lookup = tmp.set_index(["code", "_pos"])[col]
    keys = list(zip(df["code"], target.astype(int)))
    vals = [lookup.get(k, np.nan) for k in keys]
    return pd.Series(vals, index=df.index, dtype=float)


def event_time_curve(
    panel: pd.DataFrame,
    events: pd.DataFrame,
    max_days: int = 21,
    market: bool = True,
) -> pd.DataFrame:
    """Rata-rata return harian emiten & pasar pada offset 1..max_days setelah event.

    Lookup pakai posisi PER-EMITEN (baris observasi), bukan posisi global —
    emiten dengan tanggal bolong (suspend, baru listing) tidak boleh
    tertukar baris dengan emiten lain.
    """
    df = panel.sort_values(["code", "date"]).reset_index(drop=True)
    df["_ret"] = df.groupby("code")["close"].pct_change()
    mkt_daily = df.groupby("date")["_ret"].mean()
    df["_mkt"] = df["date"].map(mkt_daily)

    # (code, pos_per_emiten) untuk tiap event
    pos_of = df.groupby("code").cumcount()
    key_pos = pd.Series(
        pos_of.values,
        index=pd.MultiIndex.from_arrays([df["code"], pd.to_datetime(df["date"])]),
    )
    event_idx = set()
    for code, d in zip(events["code"], events["date"]):
        p = key_pos.get((code, pd.Timestamp(d)))
        if p is not None:
            event_idx.add((code, int(p)))

    # Lookup (code, pos) -> (ret, mkt) sekali saja, bukan .iloc per baris.
    lut = df.copy()
    lut["_pos"] = df.groupby("code").cumcount()
    lut = lut.set_index(["code", "_pos"])[["_ret", "_mkt"]]

    rows = []
    for offset in range(1, max_days + 1):
        fwd_ret, fwd_abn = [], []
        for code, p in event_idx:
            got = lut.loc[(code, p + offset)] if (code, p + offset) in lut.index else None
            if got is not None and not pd.isna(got["_ret"]):
                fwd_ret.append(float(got["_ret"]))
                if market and not pd.isna(got["_mkt"]):
                    fwd_abn.append(float(got["_ret"]) - float(got["_mkt"]))
        rows.append({
            "offset": offset,
            "mean_fwd": float(np.mean(fwd_ret)) if fwd_ret else np.nan,
            "mean_abnormal": float(np.mean(fwd_abn)) if fwd_abn and market else np.nan,
            "n": len(fwd_ret),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Orkestrasi + CLI
# --------------------------------------------------------------------------- #


def run_event_study(
    panel: pd.DataFrame,
    event: str,
    horizon: int = 21,
    min_gap: int = 10,
    params: dict | None = None,
) -> EventStudyResult:
    """Pipeline: deteksi -> fwd returns -> statistik -> kurva."""
    events = detect_events(panel, event, min_gap=min_gap, params=params)
    if events.empty:
        return EventStudyResult(event, 0, horizon, None, None, None, None, None, None,
                                pd.DataFrame())
    fwd = event_forward_returns(panel, events, horizon=horizon)
    curve = event_time_curve(panel, events, max_days=horizon)
    if fwd.empty:
        return EventStudyResult(event, len(events), horizon, None, None, None, None,
                                None, None, curve)
    mean_fwd = float(fwd["fwd"].mean())
    t_abn = _nw_tstat(fwd["abnormal"]) if "abnormal" in fwd else None
    return EventStudyResult(
        event=event,
        n_events=len(fwd),
        horizon=horizon,
        mean_fwd=mean_fwd,
        median_fwd=float(fwd["fwd"].median()),
        hit_rate=float((fwd["fwd"] > 0).mean()),
        t_stat=_nw_tstat(fwd["fwd"]),
        mean_abnormal=float(fwd["abnormal"].mean()) if "abnormal" in fwd else None,
        t_abnormal=t_abn,
        curve=curve,
    )


def load_panel_from_db(dsn: str, months: int = 18) -> pd.DataFrame:
    """Panel minimal untuk event study (PIT adjusted, sama sumbernya dgn ic.py)."""
    from .ic import load_panel

    return load_panel(dsn, min_history=40)


def print_result(r: EventStudyResult) -> None:
    print(f"\n=== {r.event} — {EVENT_PRESETS[r.event]['description']} ===")
    print(f"  event valid: {r.n_events} (horizon {r.horizon} hari bursa, entry T+1)")
    if r.n_events == 0 or r.mean_fwd is None:
        print("  (tidak ada event dengan data ke depan yang cukup)")
        return
    print(
        f"  mean fwd={r.mean_fwd:+.2%}  median={r.median_fwd:+.2%}  "
        f"hit={r.hit_rate:.0%}  t(NW)={r.t_stat:+.2f}"
    )
    if r.mean_abnormal is not None:
        print(
            f"  abnormal vs pasar: {r.mean_abnormal:+.2%}  t(NW)={r.t_abnormal:+.2f}"
        )
    if not r.curve.empty:
        marks = [1, 3, 5, 10, 21]
        parts = [
            f"D+{row.offset}:{row.mean_fwd:+.2%}"
            for row in r.curve.itertuples()
            if row.offset in marks
        ]
        print("  kurva: " + "  ".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser(prog="events")
    ap.add_argument("--event", type=str, default=None,
                    choices=sorted(EVENT_PRESETS), help="kosongkan untuk semua preset")
    ap.add_argument("--horizon", type=int, default=21)
    ap.add_argument("--gap", type=int, default=10, help="jarak minimum antar event (hari bursa)")
    ap.add_argument("--months", type=int, default=18)
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        print("[err] DATABASE_URL/SUPABASE_DB_URL belum diisi", file=__import__("sys").stderr)
        return 1

    panel = load_panel_from_db(dsn, months=args.months)
    if panel.empty:
        print("[abort] panel kosong")
        return 1
    # vol_spike/jump butuh kolom rel_volume? tidak — deteksi langsung dari volume.
    names = [args.event] if args.event else sorted(EVENT_PRESETS)
    for name in names:
        print_result(run_event_study(panel, name, horizon=args.horizon, min_gap=args.gap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
