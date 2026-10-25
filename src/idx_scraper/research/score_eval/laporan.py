"""IO data, laporan cetak, dan CLI paket."""

from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from ... import db
from ...logging_setup import say
from ..recommend import (
    HORIZONS,
)
from ..recommendation_log import load_candidate_panel, metrics_frame
from .evaluasi import frozen_eval, walk_forward
from .fitur import attach_abnormal, benchmark_prices, candidate_universe, feature_frame
from .param import (
    _FEATURES,
    BAND_SHARES,
    MIN_EDGE_DAYS,
    STEP_SESSIONS,
    TEST_SESSIONS,
    TRAIN_SESSIONS,
    logger,
)


def load_features(dsn: str, sessions: int = 300) -> pd.DataFrame:
    """Muat panel kandidat dari DB -> universe -> fitur + abnormal (siap diuji).

    Memakai panel & metrik yang PERSIS sama dengan papan Rekomendasi
    (``recommendation_log.load_candidate_panel`` + ``metrics_frame``).
    """
    panel = load_candidate_panel(dsn, sessions=sessions)
    metrics = metrics_frame(panel)
    universe = candidate_universe(metrics)
    feats = feature_frame(universe)
    prices = benchmark_prices(panel)
    prices["date"] = pd.to_datetime(prices["date"])
    return attach_abnormal(feats, HORIZONS, prices)


def format_weights(weights_by_horizon: dict[int, dict[str, float]]) -> str:
    """Cetak konstanta Python siap-tempel untuk ``recommend.py`` (deterministik)."""
    lines = ["COMPONENT_WEIGHTS: dict[int, dict[str, float]] = {"]
    for k, w in weights_by_horizon.items():
        lines.append(f"    {k}: {{")
        for name in _FEATURES:
            val = w.get(name, 0.0)
            if val:
                lines.append(f'        "{name}": {val},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# grade = peringkat relatif pool harian (bukan ambang skor absolut)")
    lines.append(f"BAND_SHARES: tuple[float, float, float] = {tuple(BAND_SHARES)}")
    return "\n".join(lines)


def print_report(report: dict[str, Any]) -> None:
    say("=" * 78)
    say("WALK-FORWARD KOMPOSISI SKOR KANDIDAT BELI (PIT, entry T+1, abn vs pasar)")
    say("=" * 78)
    say(
        f"  sesi={report['sessions']}  train={report['train']}  test={report['test']}  "
        f"step={report['step']}"
    )
    for k, h in report["by_horizon"].items():
        say(f"\n--- horizon {k} hari ---")
        if not h.get("folds"):
            say(f"  {h.get('note', 'tidak ada fold')}")
            continue
        say(f"  fold={h['folds']}  baris test OOS={h['pooled_oos_rows']}")
        for fold in h["fold_detail"]:
            ic = fold["ic"]
            mo = fold["monotonicity"]
            ic_s = f"IC={ic.get('mean_ic'):+.4f}" if ic.get("mean_ic") is not None else "IC=n/a"
            t_s = f"t={ic.get('t_stat'):+.1f}" if ic.get("t_stat") is not None else "t=n/a"
            mo_s = f"rho={mo.get('spearman'):+.2f}" if mo.get("spearman") is not None else "rho=n/a"
            say(
                f"    {fold['test'][0]}..{fold['test'][1]}  n={fold['n_test']:<5} "
                f"aktif={fold['active_components']:<2} {ic_s} {t_s} {mo_s}"
            )
        pic = h["pooled_oos_ic"]
        pmo = h["pooled_oos_monotonicity"]
        say("  pooled OOS:")
        say(
            f"    IC={pic.get('mean_ic')} ICIR={pic.get('icir')} t={pic.get('t_stat')} "
            f"hit={pic.get('hit_rate')} hari={pic.get('days')}"
        )
        say(
            f"    monotonisitas: rho={pmo.get('spearman')} spread Q10-Q1={pmo.get('top_minus_bottom')}"
        )
        for row in h["pooled_oos_deciles"]:
            say(f"      desil {row['decile']:<2} n={row['n']:<6} abn={row['mean_abn']:+.2%}")
        if h["fold_detail"]:
            mix = h["fold_detail"][-1].get("train_tier_mix") or {}
            say(
                f"  sebaran tier di train fold terakhir (n={mix.get('n')}): "
                + ", ".join(
                    f"{lab.upper()} {mix[lab]['rows']} ({mix[lab]['share']:.0%} >= {mix[lab]['min_score']})"
                    for lab in ("c", "b", "a")
                    if isinstance(mix.get(lab), dict)
                )
            )
        say("  bobot per fold (0 = tidak lolos gate di fold itu):")
        fw = h["fold_weights"]
        for name in _FEATURES:
            samples = fw.get(name) or []
            if not any(samples):
                continue
            cells = "  ".join(f"{v:+5.1f}" for v in samples)
            vd = (h["verdict"] or {}).get(name, {})
            mark = "DIPAKAI " if vd.get("status") == "dipakai" else "dimatikan"
            note = "" if vd.get("status") == "dipakai" else f"  ({vd.get('reason')})"
            say(f"    {name:<32} {cells}  -> {mark}{note}")
        if not h["weights"]:
            say("    (tidak ada komponen yang lolos gate + stabilitas tanda)")
        say(f"  bobot stabil yang dibakukan: {h['weights']}")
        say(
            "  grade = peringkat relatif pool harian; share band "
            f"C/B/A = {BAND_SHARES[0]:.0%}/{BAND_SHARES[1]:.0%}/{BAND_SHARES[2]:.0%}"
        )
        say("  edge seluruh sampel (deskriptif, in-sample — pembanding signal_log):")
        for row in h["full_sample_edges"]:
            if row.get("edge") is None:
                continue
            if abs(float(row["edge"])) < 0.002 and abs(float(row.get("edge_pooled") or 0)) < 0.002:
                continue
            say(
                f"    {row['component']:<32} edge={row['edge']:+.3%} "
                f"pooled={row['edge_pooled']:+.3%} t={row['t_stat']} "
                f"hit={row['hit_rate']} hari={row['days']}"
            )
        thin = [
            str(row["component"])
            for row in h["full_sample_edges"]
            if int(row.get("days") or 0) < MIN_EDGE_DAYS
        ]
        if thin:
            say(
                f"  {len(thin)} komponen belum bisa dinilai: terukur di < {MIN_EDGE_DAYS} sesi "
                f"(mis. {', '.join(thin[:4])}) — bobotnya 0 bukan karena lemah, tapi "
                "karena datanya belum cukup panjang."
            )
    say("\nCatatan: angka bukti = pooled OOS di atas; jalur 'frozen' hanya deskriptif.")
    say(
        "Komponen tanpa gate |t|>=1,5 & |edge|>=0,15% & >=20 sesi terukur di train "
        "berbobot 0. Grade sendiri bukan ambang skor: ia peringkat relatif pool harian."
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="score_eval")
    ap.add_argument("--horizons", type=int, nargs="+", default=list(HORIZONS))
    ap.add_argument("--train", type=int, default=TRAIN_SESSIONS)
    ap.add_argument("--test", type=int, default=TEST_SESSIONS)
    ap.add_argument("--step", type=int, default=STEP_SESSIONS)
    ap.add_argument("--sessions", type=int, default=300)
    ap.add_argument("--json", type=str, default=None, help="tulis laporan penuh ke berkas JSON")
    ap.add_argument("--frozen", action="store_true", help="tambahan evaluasi deskriptif bobot beku")
    args = ap.parse_args()

    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        logger.error("[err] DATABASE_URL belum diisi")
        return 1

    with db.connect() as conn:
        conn.execute("select 1")  # gagal cepat kalau DSN salah

    feats = load_features(dsn, sessions=args.sessions)
    if feats.empty:
        logger.error("[err] tidak ada baris kandidat — cek rentang data")
        return 1

    horizons = tuple(args.horizons)
    report = walk_forward(
        feats, horizons=horizons, train=args.train, test=args.test, step=args.step
    )

    weights_by_horizon = {
        int(k): {n: float(v) for n, v in h.get("weights_all", {}).items()}
        for k, h in report["by_horizon"].items()
    }
    report["constants"] = format_weights(weights_by_horizon)
    print_report(report)

    if args.frozen:
        report["frozen"] = frozen_eval(feats, weights_by_horizon, horizons=horizons)
        say("\n" + "=" * 78)
        say("FROZEN (deskriptif — bobot diturunkan dari sampel yang sama)")
        say("=" * 78)
        for k, v in report["frozen"].items():
            if k == "note":
                continue
            say(
                f"  horizon {k}: IC={v['ic'].get('mean_ic')} t={v['ic'].get('t_stat')} "
                f"rho={v['monotonicity'].get('spearman')}"
            )

    say("\n" + "=" * 78)
    say("KONSTANTA SIAP-TEMPEL (recommend.py)")
    say("=" * 78)
    say(report["constants"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        say(f"\n[ok] laporan penuh ditulis ke {args.json}")
    return 0
