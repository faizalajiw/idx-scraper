"""recommendation: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ... import db
from ...freshness import withhold
from ..database import get_cursor
from ._common import WIB, _research_cache_get, _research_cache_put, logger, research_cache_once
from .broker_activity import broker_scores_by_code
from .market import get_market_regime
from .smart_money import _smart_money_panel, get_smart_money_track_record

RECOMMENDATION_LAYER_NOTE = (
    "Lapisan faktor IC dan aliran broker hanya ikut bila parameternya tervalidasi "
    "(lolos gate IC). Kalau belum, keduanya tidak menggerakkan skor sama sekali "
    "— bukan diterapkan sebagai nol tersamar."
)

#: Sesi ditarik untuk metrik kandidat: rolling 252 (jarak puncak 52-minggu) +
#: warmup SMA50/RSI/ATR. Sama dengan panel pasar yang dipakai radar/smart money.
_RECOMMENDATION_HISTORY = 300


def _recommendation_metrics() -> pd.DataFrame:
    """Metrik kandidat seluruh pasar (cache 30 menit).

    Memakai panel pasar yang SAMA dengan radar/smart money
    (:func:`_smart_money_panel`), lalu menurunkannya lewat modul pure
    ``research.recommendation_log.metrics_frame`` — supaya definisi pola dan
    angka di daftar kandidat tidak pernah berbeda dari halaman lain.
    """
    cached = _research_cache_get("recommendation_metrics")
    if cached is not None:
        return cached
    from ...research.recommendation_log import metrics_frame

    panel = _smart_money_panel(_RECOMMENDATION_HISTORY)
    metrics = metrics_frame(panel)
    _research_cache_put("recommendation_metrics", metrics, ttl=1800.0)
    return metrics


def _pattern_histories() -> dict[str, dict[str, Any]]:
    """Peta ``pattern_id -> smart_money.pattern_history`` (bobot bukti pola)."""
    from ...smart_money import pattern_history

    tr = get_smart_money_track_record()
    window = tr.get("history_sessions")
    out: dict[str, dict[str, Any]] = {}
    for row in tr.get("patterns") or []:
        h = pattern_history(row, window_sessions=window)
        if h is not None:
            out[str(row.get("pattern"))] = h
    return out


def _recommendation_context() -> dict[str, Any]:
    """Semua input skor kandidat + status tiap lapisan (cache 30 menit).

    Dipakai bersama papan, halaman emiten, gerbang rezim, dan job rekaman, jadi
    hitungannya dijaga ``research_cache_once``: satu hitungan dingin untuk semua
    pemanggil yang datang bersamaan.
    """
    return research_cache_once(
        "recommendation_context", _recommendation_context_uncached, ttl=1800.0
    )


def _recommendation_context_uncached() -> dict[str, Any]:
    """Badan asli ``_recommendation_context``: hitung dari nol, tanpa cache.

    Lapisan faktor IC dan aliran broker dihitung di sini SEKALI untuk seluruh
    pasar, lalu di-injeksi per emiten. Bila gate IC belum terpenuhi, keduanya
    kosong dan tidak menggerakkan skor — transparan lewat ``layers``.
    """
    metrics = _recommendation_metrics()
    histories = _pattern_histories()

    quarantined: set[str] = set()
    try:
        from ...research.pool import quarantined_codes

        with get_cursor() as cur:
            quarantined = quarantined_codes(cur)
    except Exception as e:  # tabel karantina belum ada / DB sibuk
        logger.warning(f"[warn] karantina kolam tidak terbaca: {e}")

    factor_adj: dict[str, float] = {}
    factor_validated = False
    factor_note = (
        "Belum ada faktor umum yang lolos gate IC (|IC| >= 0,05 & |ICIR| >= 0,5) "
        "pada run terakhir — lapisan faktor tidak dipakai."
    )
    try:
        from ...research.composite import factor_adjustment, market_factor_ranks
        from ...research.ic_history import weights_by_key

        with get_cursor() as cur:
            weights = weights_by_key(cur)
            if weights:
                ranks = market_factor_ranks(cur)
                for code, pcts in ranks.items():
                    adj, _ = factor_adjustment(pcts, weights)
                    if adj:
                        factor_adj[str(code).upper()] = adj
                factor_validated = True
                factor_note = "Lapisan faktor aktif — bobot dari run IC tervalidasi."
    except Exception as e:  # DB kosong / query gagal: lapisan tidak aktif
        logger.warning(f"[warn] lapisan faktor rekomendasi dilewati: {e}")

    broker_adj: dict[str, float] = {}
    broker_validated = False
    broker_note = (
        "Faktor aliran (proksi aktivitas broker) belum lolos gate IC — lapisan "
        "aliran tidak dipakai."
    )
    try:
        from ...research.broker_activity import verdict_adjustment

        broker_by_code, broker_validated = broker_scores_by_code()
        if broker_validated:
            for code, row in broker_by_code.items():
                adj, _ = verdict_adjustment(row.get("score"), True)
                if adj:
                    broker_adj[str(code).upper()] = adj
            broker_note = "Lapisan aliran broker aktif — faktor aliran lolos gate IC."
    except Exception as e:
        logger.warning(f"[warn] lapisan broker rekomendasi dilewati: {e}")

    from ...research.pool import pool_members

    pool = (
        pool_members(metrics, metrics["date"].max(), quarantined=quarantined)
        if not metrics.empty
        else set()
    )

    out: dict[str, Any] = {
        "metrics": metrics,
        "histories": histories,
        "factor_adj": factor_adj,
        "factor_validated": factor_validated,
        "factor_note": factor_note,
        "broker_adj": broker_adj,
        "broker_validated": broker_validated,
        "broker_note": broker_note,
        "pool": pool,
        "quarantined": quarantined,
    }
    return out


def recommendation_record_inputs() -> dict[str, Any]:
    """Input lapisan untuk job log rekomendasi (tanpa metrik — job memuat panelnya).

    Dipakai worker CLI supaya baris yang ditulis ke ``research.recommendation_log``
    memakai bobot pola & lapisan yang SAMA dengan yang tampil di API.
    """
    ctx = _recommendation_context()
    return {
        "histories": ctx["histories"],
        "factor_adj_by_code": ctx["factor_adj"],
        "factor_validated": ctx["factor_validated"],
        "broker_adj_by_code": ctx["broker_adj"],
        "broker_validated": ctx["broker_validated"],
    }


def _with_sizing(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lengkapi tiap kandidat dengan ukuran posisi (% modal) — risiko 1%/posisi."""
    from ...research.recommend import position_size_pct

    for c in candidates:
        c["position_pct"] = position_size_pct(entry=c.get("entry_ref"), stop=c.get("stop"))
    return candidates


def _freshness_of() -> dict[str, Any]:
    """Status kesegaran saat ini (spec D1).

    Satu-satunya jalur `api.services` yang dipakai di modul ini, dan
    sengaja diimpor di dalam fungsi supaya tak ada impor melingkar.
    """
    from ..services import get_freshness

    return get_freshness()


def _withhold_panel(out: dict[str, Any]) -> dict[str, Any]:
    """Papan + status kesegaran, verdict ditahan bila basi (spec D1).

    Dipanggil SESUDAH cache dibaca/ditulis: status segar/basi berubah tiap
    sesi, dan cache 30 menit tidak boleh menguncinya. Dict yang di-cache
    tidak pernah termutasi - `withhold` mengembalikan salinan.
    """
    f = _freshness_of()
    return {
        **out,
        "freshness": f,
        "withheld": bool(f.get("withhold")),
        "rows": withhold(list(out.get("rows") or []), f),
    }


def _withhold_stock(out: dict[str, Any]) -> dict[str, Any]:
    """Sama seperti papan, untuk satu emiten (spec D1)."""
    f = _freshness_of()
    candidate = out.get("candidate")
    if isinstance(candidate, dict):
        candidate = withhold([candidate], f)[0]
    return {**out, "freshness": f, "withheld": bool(f.get("withhold")), "candidate": candidate}


def _regime_gate() -> dict[str, Any] | None:
    """Gerbang rezim saat ini untuk papan (spec 6.5 + keputusan pemilik Q5).

    Fail-open dengan sebab tertulis: rezim yang tidak terbaca TIDAK boleh
    membisukan papan tanpa penjelasan. Sengaja tanpa cache - dipanggil SESUDAH
    cache papan (statusnya berubah intraday), dan `get_market_regime` sudah
    dibaca banner tiap 30 detik.
    """
    try:
        gate = get_market_regime().get("gate")
    except Exception as e:
        logger.warning(f"[warn] gerbang rezim tidak terbaca: {e}")
        return None
    return gate if isinstance(gate, dict) else None


def _gate_withhold(out: dict[str, Any], gate: dict[str, Any] | None) -> dict[str, Any]:
    """Tahan TAWARAN papan saat rezim eksplisit menolak (spec 6.5/Q5).

    Yang ditahan cuma tawarannya - `grade` (label keyakinan) dan `position_pct`
    (ukuran posisi) - sementara angka, level, tanggal, dan sebabnya tetap tampil.
    Dipanggil SESUDAH cache, seperti penahanan D1, dan tidak pernah memutasi dict
    yang hidup di cache (`_withhold_panel`/`_withhold_stock` sudah mengembalikan
    salinan barisnya).
    """
    result: dict[str, Any] = {**out, "gate": gate}
    if not gate or not gate.get("blocked"):
        return result
    if "rows" in result:
        result["rows"] = [
            {**row, "grade": None, "position_pct": None, "withheld": True}
            for row in out.get("rows") or []
        ]
    candidate = result.get("candidate")
    if isinstance(candidate, dict):
        result["candidate"] = {
            **candidate,
            "grade": None,
            "position_pct": None,
            "withheld": True,
        }
    return result


def _with_position_plan(
    out: dict[str, Any],
    *,
    modal: float | None = None,
    risk_pct: float | None = None,
    open_positions: int | None = None,
    exposure_pct: float | None = None,
    metrics: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Tempelkan rencana posisi ke baris yang benar-benar tampil (spec P5 5.3).

    Dipanggil SESUDAH cache dan SESUDAH penahanan: baris ber-``withheld`` tidak
    pernah membawa ukuran posisi, dan cache papan tidak dipecah per angka modal.
    Dict yang masuk tidak pernah dimutasi - baris dan kandidatnya disalin.

    ``metrics`` hanya dipakai tes; produksi membaca panel yang sudah di-cache.
    """
    from ...research.position import position_plan, typical_day_values

    frame = metrics if metrics is not None else _recommendation_context()["metrics"]
    vol_state = (out.get("gate") or {}).get("vol_state")

    peta_likuiditas: dict[str, float] | None = None

    def likuiditas(code: Any) -> float | None:
        """Nilai transaksi harian median, dihitung sekali untuk seluruh permintaan.

        Versi per emiten memindai panel seluruh pasar tiap panggilan; papan
        memanggilnya untuk puluhan baris, jadi pemindaiannya dibayar berulang.
        """
        nonlocal peta_likuiditas
        if modal is None:
            return None  # tanpa modal plafon likuiditas memang dilewati
        if peta_likuiditas is None:
            peta_likuiditas = typical_day_values(frame)
        return peta_likuiditas.get(str(code or "").upper())

    def plan_of(row: Any) -> dict[str, Any] | None:
        if not isinstance(row, dict) or row.get("withheld"):
            return None
        return position_plan(
            entry=row.get("entry_ref"),
            stop=row.get("stop"),
            modal=modal,
            risk_pct=risk_pct,
            typical_day_value=likuiditas(row.get("code")),
            vol_state=vol_state,
            open_positions=open_positions,
            exposure_pct=exposure_pct,
        )

    result: dict[str, Any] = {**out}
    if "rows" in result:
        result["rows"] = [{**row, "plan": plan_of(row)} for row in out.get("rows") or []]
    candidate = result.get("candidate")
    if isinstance(candidate, dict):
        result["candidate"] = {**candidate, "plan": plan_of(candidate)}
    return result


def _close_recommendation(out: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Urutan tetap: penahanan D1 -> gerbang rezim -> rencana posisi (spec P5 5.3)."""
    withheld = _withhold_panel(out) if "rows" in out else _withhold_stock(out)
    return _with_position_plan(_gate_withhold(withheld, _regime_gate()), **state)


def get_recommendations(
    limit: int = 20,
    min_grade: str = "C",
    *,
    modal: float | None = None,
    risk_pct: float | None = None,
    open_positions: int | None = None,
    exposure_pct: float | None = None,
) -> dict[str, Any]:
    """Papan kandidat beli se-pasar untuk sesi terakhir (read-only).

    Menggabungkan: setup teknikal + entry timing (modul pure ``recommend``),
    bukti pola jejak smart money (track record, bukan klaim), dan — HANYA bila
    tervalidasi — lapisan faktor IC serta aliran broker. Tiap kandidat membawa
    zona entry, stop, target, R/R, dan ukuran posisi sederhana.

    Cache 30 menit (metrik harian, tidak berubah intraday). Papan kosong berarti
    tidak ada kandidat yang lolos gate hari itu — bukan error.

    Empat angka keadaan (modal, risiko per transaksi, posisi terbuka, eksposur)
    hanya dipakai untuk menghitung rencana posisi SESUDAH cache: papan yang sama
    tidak pernah disimpan terpisah per ukuran modal.
    """
    limit = max(1, min(limit, 100))
    min_grade = str(min_grade).upper()
    if min_grade not in ("A", "B", "C"):
        min_grade = "C"
    state = {
        "modal": modal,
        "risk_pct": risk_pct,
        "open_positions": open_positions,
        "exposure_pct": exposure_pct,
    }
    cache_key = f"recommendations:{limit}:{min_grade}"
    out = research_cache_once(cache_key, lambda: _board_uncached(limit, min_grade), ttl=1800.0)
    return _close_recommendation(out, state)


def _board_uncached(limit: int, min_grade: str) -> dict[str, Any]:
    """Badan asli ``get_recommendations``: hitung papan dari nol, tanpa cache.

    Dipisah supaya ``research_cache_once`` bisa menjaga key-nya: satu hitungan dingin
    (67 detik) untuk semua pemanggil yang datang bersamaan.
    """
    from ...research.recommend import rank_candidates
    from ...research.recommendation_log import candidates_for_date

    ctx = _recommendation_context()
    metrics: pd.DataFrame = ctx["metrics"]
    layers = {
        "factor": ctx["factor_validated"],
        "broker": ctx["broker_validated"],
        "factor_note": ctx["factor_note"],
        "broker_note": ctx["broker_note"],
        "note": RECOMMENDATION_LAYER_NOTE,
    }

    out: dict[str, Any] = {
        "date": None,
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "scanned": 0,
        "total_candidates": 0,
        "limit": limit,
        "min_grade": min_grade,
        "layers": layers,
        "rows": [],
    }
    if metrics.empty:
        return out

    last_date = metrics["date"].max()
    scanned = int((metrics["date"] == last_date).sum())
    candidates = candidates_for_date(
        metrics,
        last_date,
        histories=ctx["histories"],
        factor_adj_by_code=ctx["factor_adj"],
        factor_validated=ctx["factor_validated"],
        broker_adj_by_code=ctx["broker_adj"],
        broker_validated=ctx["broker_validated"],
        pool=ctx["pool"],
        quarantined=ctx["quarantined"],
    )
    candidates = _with_sizing(candidates)
    ranked = rank_candidates(candidates, limit=limit, min_grade=min_grade)
    # Persentil riwayat sendiri hanya untuk baris yang benar-benar tampil
    # (<= limit): biayanya per kode x panjang riwayat, bukan per pasar.
    from ...research.recommend import DEFAULT_HORIZON, own_history_percentile

    for r in ranked:
        r["own_percentile"] = own_history_percentile(
            metrics, r["code"], int(r.get("horizon_days") or DEFAULT_HORIZON)
        )

    out["date"] = str(pd.Timestamp(last_date).date())
    out["scanned"] = scanned
    out["pool_size"] = len(ctx["pool"])
    out["total_candidates"] = len(candidates)
    out["rows"] = ranked
    return out


def get_stock_recommendation(
    code: str,
    *,
    modal: float | None = None,
    risk_pct: float | None = None,
    open_positions: int | None = None,
    exposure_pct: float | None = None,
) -> dict[str, Any]:
    """Kandidat beli untuk SATU emiten (skor + level + alasan).

    Skor memakai mesin yang sama dengan papan se-pasar, jadi angka emiten di
    halaman detail persis sama dengan angkanya di papan. Emiten tanpa kandidat
    (di bawah grade C / likuiditas kurang / sinyal SELL) tetap mengembalikan
    alasan penolakan, bukan kandidat rekaan.

    Empat angka keadaan sama seperti papan: hanya untuk rencana posisi SESUDAH
    cache, supaya angka papan dan halaman emiten tidak pernah berbeda.
    """
    code = code.strip().upper()
    state = {
        "modal": modal,
        "risk_pct": risk_pct,
        "open_positions": open_positions,
        "exposure_pct": exposure_pct,
    }
    cache_key = f"recommendation_stock:{code}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return _close_recommendation(cached, state)

    from ...research.recommend import DEFAULT_HORIZON, own_history_percentile
    from ...research.recommendation_log import candidates_for_date

    ctx = _recommendation_context()
    metrics: pd.DataFrame = ctx["metrics"]
    out: dict[str, Any] = {
        "code": code,
        "as_of": None,
        "has_data": False,
        "candidate": None,
        "layers": {
            "factor": ctx["factor_validated"],
            "broker": ctx["broker_validated"],
            "factor_note": ctx["factor_note"],
            "broker_note": ctx["broker_note"],
            "note": RECOMMENDATION_LAYER_NOTE,
        },
        "reason": None,
        "generated_at": datetime.now(WIB).isoformat(timespec="seconds"),
    }

    if metrics.empty:
        out["reason"] = "Belum ada data harga yang cukup untuk menghitung kandidat."
        _research_cache_put(cache_key, out, ttl=1800.0)
        return _close_recommendation(out, state)

    rows = metrics[metrics["code"] == code]
    if rows.empty:
        out["reason"] = f"Tidak ada data harga untuk {code}."
        _research_cache_put(cache_key, out, ttl=1800.0)
        return _close_recommendation(out, state)

    row = rows.sort_values("date").iloc[-1]
    last_date = metrics["date"].max()
    out["has_data"] = True
    out["as_of"] = str(pd.Timestamp(last_date).date())

    # Skor **dan grade** memakai jalur yang sama dengan papan (satu kolam, satu
    # peringkat), jadi angka emiten di halaman detail tidak mungkin berbeda dari
    # angkanya di papan. Grade adalah peringkat relatif kolam hari itu.
    candidates = candidates_for_date(
        metrics,
        last_date,
        histories=ctx["histories"],
        factor_adj_by_code=ctx["factor_adj"],
        factor_validated=ctx["factor_validated"],
        broker_adj_by_code=ctx["broker_adj"],
        broker_validated=ctx["broker_validated"],
        pool=ctx["pool"],
        quarantined=ctx["quarantined"],
    )
    candidate = next((c for c in candidates if c["code"] == code), None)
    if candidate is None:
        if pd.Timestamp(row["date"]) != pd.Timestamp(last_date):
            out["reason"] = (
                f"Data terakhir {code} ({pd.Timestamp(row['date']).date()}) bukan sesi "
                f"terakhir pasar ({out['as_of']}) — tidak ada kandidat untuk sesi itu."
            )
        else:
            out["reason"] = (
                "Belum masuk kandidat beli: di luar 25% teratas pool hari itu, "
                "likuiditas kurang, sinyal jual, atau level eksekusi tidak bisa dihitung."
            )
    else:
        candidate = _with_sizing([candidate])[0]
        # N4 (review): field ini harus terisi di kedua permukaan - `null` tidak
        # boleh ambigu antara "riwayat pendek" dan "endpoint lupa mengisi".
        candidate["own_percentile"] = own_history_percentile(
            metrics, candidate["code"], int(candidate.get("horizon_days") or DEFAULT_HORIZON)
        )
        out["candidate"] = candidate

    _research_cache_put(cache_key, out, ttl=1800.0)
    return _close_recommendation(out, state)


def get_recommendation_track() -> dict[str, Any]:
    """Track record kandidat beli (per grade & horizon) — "rekomendasi terbukti?".

    Membaca ``research.recommendation_log`` (kandidat harian) lalu menilai
    return forward T+1 + abnormal vs pasar. Kalau log belum pernah diisi,
    mengembalikan payload kosong dengan alasan — bukan angka rekaan. Cache 1 jam
    (hitungan mahal & harian, tak berubah intraday).
    """
    cache_key = "recommendation_track"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    from ...research.recommendation_log import evaluate

    dsn = db.dsn() if db.tersedia() else None
    if not dsn:
        out = {
            "candidates": 0,
            "horizons": [5, 10, 21],
            "by_grade": [],
            "recent": [],
            "reason": "DATABASE_URL belum diisi.",
        }
        _research_cache_put(cache_key, out, ttl=600.0)
        return out
    try:
        out = evaluate(dsn)
    except Exception as e:
        logger.warning(f"[warn] track record rekomendasi dilewati: {e}")
        out = {
            "candidates": 0,
            "horizons": [5, 10, 21],
            "by_grade": [],
            "recent": [],
            "reason": "Tabel research.recommendation_log belum ada / belum diisi.",
        }
    if not out.get("candidates") and not out.get("reason"):
        out["reason"] = "Belum ada kandidat tercatat — jalankan `idx recommendations`."
    _research_cache_put(cache_key, out, ttl=3600.0)
    return out
