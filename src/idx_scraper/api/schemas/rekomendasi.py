"""Skema ruang keputusan & rekomendasi beli."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .pasar import Freshness, RegimeGate


class StockDecision(BaseModel):
    """Ruang Keputusan satu emiten: verdict gabungan + konteks & level invalidasi.

    ``hold_check`` adalah SATU-SATUNYA sumber verdict (skor 0-100 + band);
    sisanya konteks yang tidak mengubah verdict. Semua field bisa None bila
    sumbernya belum tersedia — UI menyajikannya apa adanya, bukan 0.
    """

    code: str
    generated_at: str
    hold_check: dict[str, Any] | None = None
    regime: dict[str, Any] | None = None
    sentiment: dict[str, Any] | None = None
    market_gauge: dict[str, Any] | None = None
    foreign_10: list[dict[str, Any]] = []
    last_flip: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    invalidation: dict[str, Any] | None = None


class RecommendationMetrics(BaseModel):
    """Metrik mentah di balik satu kandidat beli (apa adanya, boleh None)."""

    close: float | None = None
    signal: str | None = None
    trend_up: bool = False
    rsi: float | None = None
    mom_20d: float | None = None
    atr_pct: float | None = None
    dist_52w_pct: float | None = None
    vol_ratio: float | None = None
    value: float | None = None
    z_score: float | None = None
    hist_days: int | None = None


class RecommendationLayerStatus(BaseModel):
    """Lapisan mana yang ikut menggerakkan skor + alasannya (transparansi).

    ``factor``/``broker`` False = lapisan belum lolos gate IC sehingga TIDAK
    dipakai (bukan diterapkan sebagai nol tersamar).
    """

    factor: bool = False
    broker: bool = False
    factor_note: str | None = None
    broker_note: str | None = None
    note: str | None = None


class RankBasis(BaseModel):
    """Urutan pemutus yang dipakai peringkat kandidat (spec D3).

    ``order`` = kunci berurutan; ``values`` = nilai tiap kunci untuk baris
    ini. Ditampilkan supaya pembaca bisa melihat kenapa satu nama di atas
    nama lain saat skornya sama.
    """

    order: list[str] = []
    values: list[Any] = []


class PositionPlanCaps(BaseModel):
    """Plafon yang berlaku untuk satu nama, dalam % modal (spec P5 5.1)."""

    per_name: float | None = None
    liquidity: float | None = None
    regime: float | None = None
    portfolio: float | None = None


class PositionPlan(BaseModel):
    """Rencana posisi lapis 3: persen, rupiah, lot, dan rugi kalau stop kena.

    Dihitung SESUDAH cache papan, dari empat angka keadaan yang diketik pemilik
    (modal, risiko per transaksi, posisi terbuka, eksposur) - jadi papan yang
    di-cache tidak pernah dipecah per ukuran modal. ``pct`` = angka final yang
    berlaku; ``caps`` = tiap plafon sebelum dipilih yang terkecil;
    ``binding_cap`` = plafon yang benar-benar memotong (None = tidak ada).
    """

    pct: float | None = None
    pct_raw: float | None = None
    binding_cap: str | None = None
    caps: PositionPlanCaps | None = None
    concurrent_risk_pct: float | None = None
    rupiah: float | None = None
    lots: int | None = None
    risk_rupiah: float | None = None
    note: str | None = None


class RecommendationRow(BaseModel):
    """Satu kandidat beli: skor + grade + level eksekusi + alasan.

    Level (``entry_*``/``stop``/``target``) selalu terisi untuk kandidat yang
    masuk papan — tanpa level pembatalan yang bisa dihitung, kandidat tidak
    ditampilkan. ``position_pct`` = ukuran posisi sederhana (% modal) dari
    risiko 1% per posisi. ``plan`` = rencana posisi lapis 3 (rupiah, lot, rugi,
    plafon) yang menghormati angka modal pemilik; None = tanpa modal atau baris
    ini ditahan.

    ``grade`` = **peringkat relatif pool hari itu** (C 25% · B 10% · A 2%
    teratas), bukan probabilitas dan bukan ambang skor absolut.
    ``horizon_days`` = horizon komposisi skor yang dipakai (``COMPONENT_WEIGHTS``;
    default ``DEFAULT_HORIZON`` = 21 hari bursa) — jadi pembaca tahu klaimnya
    diukur pada jangka berapa.
    """

    code: str
    name: str | None = None
    score: float
    # None = verdict ditahan karena datanya basi (spec D1).
    grade: str | None = None  # A | B | C
    # Peringkat 0-100 di kolam sesi itu (spec D3), bukan probabilitas.
    percentile: float | None = None
    # Skor terakhir vs riwayat emiten itu sendiri; None = riwayat < 20 sesi.
    own_percentile: float | None = None
    rank_basis: RankBasis | None = None
    # Label skor (spec D3) — BUKAN ``confidence`` pola di bawah: yang ini soal
    # "skornya bergerak & horizonnya teruji", yang itu soal bukti pola.
    score_confidence: str | None = None  # "meyakinkan" | "belum terbukti"
    setup: str | None = None  # pullback | breakout | trend | netral
    entry_low: float | None = None
    entry_high: float | None = None
    entry_ref: float | None = None
    stop: float | None = None
    target: float | None = None
    rr: float | None = None
    entry_note: str | None = None
    horizon_days: int | None = None
    confidence: str | None = None  # tinggi | sedang | lemah (bila ada pola)
    position_pct: float | None = None
    # Rencana posisi (lapis 3). None = tidak ada modal, atau baris ini ditahan.
    plan: PositionPlan | None = None
    patterns: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    layers: dict[str, bool] = {}
    metrics: RecommendationMetrics | None = None
    # True = grade/ukuran baris ini ditahan karena data tidak segar (D1).
    withheld: bool = False


class RecommendationResponse(BaseModel):
    """Papan kandidat beli se-pasar untuk sesi terakhir."""

    date: str | None = None
    generated_at: str | None = None
    scanned: int = 0
    total_candidates: int = 0
    #: Anggota kolam sesi itu sebelum dipotong limit/min_grade (spec 6.6:
    #: 'berapa banyak nama yang keluar' harus terbaca, bukan ditaksir).
    pool_size: int = 0
    limit: int = 20
    min_grade: str = "C"
    layers: RecommendationLayerStatus
    rows: list[RecommendationRow] = []
    freshness: Freshness | None = None
    withheld: bool = False
    #: Gerbang rezim. `blocked` True berarti grade & ukuran posisi ditahan
    #: (spec 6.5/Q5) - angkanya, level, dan sebabnya tetap tampil.
    gate: RegimeGate | None = None


class StockRecommendation(BaseModel):
    """Kandidat beli satu emiten (mesin skor sama dengan papan se-pasar)."""

    code: str
    as_of: str | None = None
    has_data: bool = False
    candidate: RecommendationRow | None = None
    layers: RecommendationLayerStatus
    reason: str | None = None
    freshness: Freshness | None = None
    withheld: bool = False
    gate: RegimeGate | None = None
    generated_at: str | None = None


class RecommendationTrackStat(BaseModel):
    """Statistik track record satu irisan (grade x horizon)."""

    grade: str
    horizon: int
    n: int
    hit_rate: float | None = None
    mean_fwd: float | None = None
    median_fwd: float | None = None
    mean_abnormal: float | None = None
    t_stat: float | None = None
    avg_mfe: float | None = None
    avg_mae: float | None = None


class RecommendationTrackRecent(BaseModel):
    """Satu kandidat terbaru dengan return yang sudah terealisasi."""

    code: str
    date: str
    grade: str
    score: float | None = None
    close: float | None = None
    fwd_5: float | None = None
    fwd_10: float | None = None
    fwd_21: float | None = None


class RecommendationTrack(BaseModel):
    """Track record kandidat beli (per grade & horizon) — "rekomendasi terbukti?"."""

    candidates: int = 0
    grade_a: int = 0
    grade_b: int = 0
    grade_c: int = 0
    first_date: str | None = None
    last_date: str | None = None
    horizons: list[int] = []
    by_grade: list[RecommendationTrackStat] = []
    recent: list[RecommendationTrackRecent] = []
    reason: str | None = None
