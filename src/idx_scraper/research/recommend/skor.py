"""Komponen skor, skor akhir kandidat, dan peringkat akhir."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .bukti import smart_money_evidence
from .param import (
    BREAKOUT_NEAR_HIGH_PCT,
    COMPONENT_NOTES,
    DEFAULT_RISK_PCT,
    HIGH_VOL_DAILY,
    MAX_POSITION_PCT,
    MIN_MARKET_HISTORY,
    MOM20_STRONG_PCT,
    PATTERN_COMPONENTS,
    RSI_HEALTHY_HIGH,
    RSI_HEALTHY_LOW,
    RSI_OVERBOUGHT,
    RSI_PULLBACK_LOW,
    RSI_WEAK,
    VALUE_LARGE,
    Z_EXTENDED,
    Z_MID,
    _b,
    _f,
)
from .peringkat import candidate_rank_key, safe_horizon, score_components
from .rencana import entry_plan


def candidate_features(
    metrics: Mapping[str, Any],
    *,
    setup: str | None = None,
) -> dict[str, float]:
    """Bendera komponen skor satu emiten (pure) — 0.0/1.0 per komponen.

    Ini **definisi tunggal** komponen: dipakai ``score_candidate`` saat menyusun
    skor, dan dipakai ``research.score_eval`` saat mengukur edge tiap komponen
    secara walk-forward. Kalau keduanya berbeda, bobot hasil ukur tidak lagi
    menggambarkan skor yang benar-benar dipakai.

    Semua fitur memakai ambang absolut (tanpa cross-section hari itu) supaya
    nilainya sama baik saat dinilai se-pasar maupun saat dinilai satu emiten.
    ``setup`` = gaya entry dari ``entry_plan`` (opsional; bila None, setup tidak
    memberi poin).
    """
    rsi = _f(metrics.get("rsi"))
    z = _f(metrics.get("z_score"))
    mom = _f(metrics.get("mom_20d"))
    vol_d = _f(metrics.get("vol_daily"))
    value = _f(metrics.get("value"))
    dist = _f(metrics.get("dist_52w_pct"))
    vr = _f(metrics.get("vol_ratio"))
    signal = metrics.get("signal")
    trend_up = _b(metrics.get("trend_up"))

    feats: dict[str, float] = {name: 0.0 for name in COMPONENT_NOTES}

    feats["signal_buy"] = 1.0 if signal == "BUY" else 0.0
    feats["signal_hold"] = 1.0 if signal == "HOLD" else 0.0
    feats["trend_up"] = 1.0 if trend_up else 0.0
    if rsi is not None:
        feats["rsi_healthy"] = 1.0 if RSI_HEALTHY_LOW <= rsi <= RSI_HEALTHY_HIGH else 0.0
        feats["rsi_pullback"] = (
            1.0 if trend_up and RSI_PULLBACK_LOW <= rsi < RSI_HEALTHY_LOW else 0.0
        )
        feats["rsi_overbought"] = 1.0 if rsi > RSI_OVERBOUGHT else 0.0
        feats["rsi_weak"] = 1.0 if rsi < RSI_WEAK else 0.0
    feats["near_52w_high"] = 1.0 if dist is not None and dist >= BREAKOUT_NEAR_HIGH_PCT else 0.0
    feats["vol_expansion"] = 1.0 if vr is not None and vr >= 1.5 else 0.0
    feats["mom20_strong"] = 1.0 if mom is not None and mom >= MOM20_STRONG_PCT else 0.0
    feats["high_volatility"] = 1.0 if vol_d is not None and vol_d >= HIGH_VOL_DAILY else 0.0
    if z is not None:
        feats["z_extended"] = 1.0 if z >= Z_EXTENDED else 0.0
        feats["z_mid"] = 1.0 if Z_MID <= z < Z_EXTENDED else 0.0
    feats["value_large"] = 1.0 if value is not None and value >= VALUE_LARGE else 0.0
    feats["setup_pullback"] = 1.0 if setup == "pullback" else 0.0
    feats["setup_breakout"] = 1.0 if setup == "breakout" else 0.0

    for pid in metrics.get("pattern_buy") or ():
        comp = PATTERN_COMPONENTS.get(str(pid))
        if comp:
            feats[comp] = 1.0
    for pid in metrics.get("pattern_sell") or ():
        comp = PATTERN_COMPONENTS.get(str(pid))
        if comp:
            feats[comp] = 1.0
    return feats


# ------------------------------------------------------------------ skor


def _candidate_inputs(
    metrics: Mapping[str, Any],
) -> tuple[str, float | None, float | None, Any, float | None, dict[str, Any]] | None:
    """Gerbang kelayakan kandidat + level eksekusi; ``None`` = tidak layak.

    Mengembalikan ``(code, hist_days, value, signal, close, plan)``. Semua
    penolakan (kode kosong, histori kurang dari ``MIN_MARKET_HISTORY``, sinyal
    SELL, level eksekusi tak bisa dihitung) terkonsentrasi di sini.
    """
    code = str(metrics.get("code") or "").strip().upper()
    if not code:
        return None

    hist_days = _f(metrics.get("hist_days"))
    if hist_days is not None and hist_days < MIN_MARKET_HISTORY:
        return None

    value = _f(metrics.get("value"))

    signal = metrics.get("signal")
    if signal == "SELL":
        return None

    close = _f(metrics.get("close"))
    plan = entry_plan(
        close=close,
        signal=str(signal) if signal else None,
        trend_up=metrics.get("trend_up"),
        rsi=metrics.get("rsi"),
        bb_lower=metrics.get("bb_lower"),
        dist_52w_pct=metrics.get("dist_52w_pct"),
        vol_ratio=metrics.get("vol_ratio"),
        vol_daily=metrics.get("vol_daily"),
        range_low_20=metrics.get("range_low_20"),
    )
    if plan is None:
        return None
    return code, hist_days, value, signal, close, plan


def _layer_points(
    factor_adj: float,
    factor_validated: bool,
    broker_adj: float,
    broker_validated: bool,
) -> float:
    """Poin tambahan dari lapisan faktor IC / aliran broker.

    Hanya lapisan yang sudah lolos gate sendiri (IC / aliran broker) yang
    menggeser skor; yang belum tervalidasi tidak dikirim sebagai nol tersamar.
    """
    points = 0.0
    if factor_validated and factor_adj:
        points += float(factor_adj)
    if broker_validated and broker_adj:
        points += float(broker_adj)
    return points


def _layer_notes(
    factor_adj: float,
    factor_validated: bool,
    broker_adj: float,
    broker_validated: bool,
) -> tuple[list[str], list[str]]:
    """Jejak lapisan untuk dibaca pengguna; poinnya sudah masuk ``_layer_points``.

    ``reasons`` untuk포인트 positif, ``warnings`` untuk negatif. Lapisan yang
    belum tervalidasi tidak menulis apa pun.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    if factor_validated and factor_adj:
        _note = (
            f"Lapisan faktor IC {'+' if factor_adj > 0 else ''}{factor_adj:.1f} poin "
            "(bobot dari run IC tervalidasi)."
        )
        (reasons if factor_adj > 0 else warnings).append(_note)
    if broker_validated and broker_adj:
        _note = (
            f"Lapisan aliran broker {'+' if broker_adj > 0 else ''}{broker_adj:.1f} poin "
            "(faktor aliran lolos gate IC)."
        )
        (reasons if broker_adj > 0 else warnings).append(_note)
    return reasons, warnings


def _risk_notes(metrics: Mapping[str, Any]) -> list[str]:
    """Peringatan risiko (z-score, ATR) untuk ``warnings`` kandidat."""
    notes: list[str] = []
    z = _f(metrics.get("z_score"))
    if z is not None:
        if z >= 2.0:
            notes.append(f"Harga jauh di atas band 60-hari (z {z:+.1f}) — overbought.")
        elif z >= 1.5:
            notes.append(f"Harga di atas band 60-hari (z {z:+.1f}) — waspada.")
    atr = _f(metrics.get("atr_pct"))
    if atr is not None and atr >= 8.0:
        notes.append(f"Volatilitas harian tinggi (ATR {atr:.1f}%) — perbesar jarak stop.")
    return notes


def _candidate_payload(
    code: str,
    metrics: Mapping[str, Any],
    plan: dict[str, Any],
    ev: dict[str, Any],
    *,
    score: float,
    h: int,
    layers: dict[str, bool],
    reasons: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Bentuk akhir dict kandidat yang dikembalikan ``score_candidate``."""
    atr = _f(metrics.get("atr_pct"))
    return {
        "code": code,
        "name": metrics.get("name"),
        "score": round(score, 1),
        "setup": plan["setup"],
        "entry_low": plan["entry_low"],
        "entry_high": plan["entry_high"],
        "entry_ref": plan["entry_ref"],
        "stop": plan["stop"],
        "target": plan["target"],
        "rr": plan["rr"],
        "entry_note": plan["note"],
        "horizon_days": h,
        "horizon_units": "hari bursa",
        "confidence": ev["best_confidence"],
        "patterns": list(metrics.get("pattern_buy") or []),
        "reasons": reasons,
        "warnings": warnings,
        "layers": layers,
        "metrics": {
            "close": _f(metrics.get("close")),
            "signal": metrics.get("signal"),
            "trend_up": _b(metrics.get("trend_up")),
            "rsi": _f(metrics.get("rsi")),
            "mom_20d": _f(metrics.get("mom_20d")),
            "atr_pct": atr,
            "dist_52w_pct": _f(metrics.get("dist_52w_pct")),
            "vol_ratio": _f(metrics.get("vol_ratio")),
            "value": _f(metrics.get("value")),
            "z_score": _f(metrics.get("z_score")),
            "hist_days": _f(metrics.get("hist_days")),
        },
    }


def score_candidate(
    metrics: Mapping[str, Any],
    *,
    histories: Mapping[str, Mapping[str, Any]] | None = None,
    horizon: int | None = None,
    factor_adj: float = 0.0,
    factor_validated: bool = False,
    broker_adj: float = 0.0,
    broker_validated: bool = False,
) -> dict[str, Any] | None:
    """Skor satu emiten jadi kandidat beli + level eksekusi (pure, testable).

    ``metrics`` = satu baris metrik terbaru (bentuk keluaran
    ``research.recommendation_log.metrics_for_date``): ``code, name, close,
    signal, trend_up, rsi, mom_20d, vol_daily, atr_pct, dist_52w_pct,
    vol_ratio, bb_lower, range_low_20, value, z_score, hist_days,
    pattern_buy, pattern_sell``.

    ``horizon`` memilih komposisi skor (``COMPONENT_WEIGHTS``); None =
    ``DEFAULT_HORIZON``. Satu papan memakai SATU horizon supaya peringkatnya
    membandingkan angka yang sebanding — horizon per-nama membuat skor dari
    skala berbeda diadu di satu daftar.

    Lapisan faktor IC (``factor_adj``) dan aliran broker (``broker_adj``) ikut
    HANYA bila ``factor_validated`` / ``broker_validated`` True; kalau tidak,
    nilainya diabaikan total — bukan diterapkan sebagai nol tersamar.

    **Grade tidak ditentukan di sini.** Ia peringkat relatif pool hari itu
    (``assign_grades``), karena skor ber-titik-massa membuat ambang absolut
    berbohong. Pemanggil wajib melewati ``assign_grades`` sebelum grade dipakai.

    Returns dict kandidat ``{code, name, score, setup, entry_low, entry_high,
    entry_ref, stop, target, rr, horizon_days, reasons, warnings, layers,
    metrics}`` atau ``None`` bila emiten tidak layak jadi kandidat (sinyal SELL,
    histori kurang, atau level eksekusi tak bisa dihitung). **Likuiditas
    bukan urusan fungsi ini**: kolam kandidat ditentukan `research.pool`
    (satu kolam, spec 6.6) supaya papan, halaman emiten, dan jurnal harian
    memakai definisi yang sama.
    """
    got = _candidate_inputs(metrics)
    if got is None:
        return None
    code, _, _, _, _, plan = got

    reasons: list[str] = []
    warnings: list[str] = []

    # Alasan berasal dari metrik yang sama yang masuk bobot; biar alasan dibuang
    # hanya komponen yang benar-benar berbobot untuk horizon yang dipunyai.
    features = candidate_features(metrics, setup=plan["setup"])
    ev = smart_money_evidence(
        buy_patterns=metrics.get("pattern_buy") or [],
        sell_patterns=metrics.get("pattern_sell") or [],
        histories=histories,
    )

    # Satu horizon per pemanggilan: skor dari skala berbeda tidak boleh diadu
    # dalam satu urutan (lihat docstring).
    h = safe_horizon(horizon)
    score = score_components(
        features,
        h,
        extra_points=_layer_points(factor_adj, factor_validated, broker_adj, broker_validated),
    )

    reasons.extend(ev["reasons"])
    warnings.extend(ev["warnings"])
    layer_reasons, layer_warnings = _layer_notes(
        factor_adj, factor_validated, broker_adj, broker_validated
    )
    reasons.extend(layer_reasons)
    warnings.extend(layer_warnings)
    warnings.extend(_risk_notes(metrics))

    layers = {
        "factor": bool(factor_validated),
        "broker": bool(broker_validated),
        "smart_money": bool(ev["reasons"] or ev["warnings"]),
    }
    return _candidate_payload(
        code,
        metrics,
        plan,
        ev,
        score=score,
        h=h,
        layers=layers,
        reasons=reasons,
        warnings=warnings,
    )


def rank_candidates(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: int = 20,
    min_grade: str = "C",
) -> list[dict[str, Any]]:
    """Urutkan kandidat dengan ``candidate_rank_key`` menurun (stabil & pure).

    ``min_grade`` menyaring band terendah yang masih ditampilkan (A/B/C).
    Urutannya sama persis dengan yang dipakai ``assign_grades``, jadi nomor
    peringkat di papan konsisten dengan grade-nya.
    """
    allowed = {"A": {"A"}, "B": {"A", "B"}, "C": {"A", "B", "C"}}.get(
        str(min_grade).upper(), {"A", "B", "C"}
    )
    items = [dict(r) for r in rows if str(r.get("grade")) in allowed]
    items.sort(key=candidate_rank_key, reverse=True)
    return items[: max(1, int(limit))]


def position_size_pct(
    *,
    entry: float | None,
    stop: float | None,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_pct: float = MAX_POSITION_PCT,
) -> float | None:
    """Ukuran posisi sederhana (% modal) dari risiko per posisi & jarak stop.

    ``posisi % = risk_pct / jarak_stop(%) * 100``. Contoh: risiko 1% modal,
    jarak stop 10% harga -> posisi 10% modal. Dibatasi ``max_pct`` supaya satu
    nama tidak pernah mendominasi. Returns None bila stop tidak valid.
    """
    e = _f(entry)
    s = _f(stop)
    if e is None or s is None or e <= 0 or s <= 0 or s >= e:
        return None
    stop_distance = (e - s) / e
    if stop_distance <= 0:
        return None
    size = float(risk_pct) / stop_distance
    return round(min(size, float(max_pct)), 1)
