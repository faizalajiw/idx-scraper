"""Rekomendasi Beli — kandidat beli berperingkat, entry, stop, target (pure).

Menjawab pertanyaan yang belum pernah dijawab dashboard: **"kalau hari ini
harus memilih saham untuk DIBELI, mana yang paling masuk akal — dan di harga
berapa masuk, stop di mana, targetnya berapa?"**

Bedanya dengan ``services.get_hold_check`` (verdict STRONG HOLD/HOLD/TRIM/EXIT):
hold-check menjawab "masih layak DIPEGANG?", modul ini menyusun **daftar
kandidat beli berperingkat** plus level eksekusi, dan mengurutkannya se-pasar.

Aturan yang dipegang (lihat juga skill ``technical-analysis-patterns`` &
``backtesting-discipline``)
-----------------------------------------------------------------------------------------
1. **Pure.** Menerima metrik yang sudah dihitung (dict/DataFrame), tanpa akses
   DB, supaya bisa dites tanpa Postgres — pola yang sama dengan
   ``research.composite`` dan ``research.broker_activity``.
2. **Hanya lapisan TERUKUR yang dihitung penuh.** Sinyal teknikal punya track
   record (``research.signal_log``), pola jejak smart money punya track record
   (``smart_money.pattern_track_record``). Lapisan faktor IC dan aliran broker
   ikut HANYA bila parameternya dinyatakan tervalidasi (``*_validated=True``) —
   kalau tidak, kontribusinya nol dan UI menampilkan lapisan itu sebagai tidak
   aktif, bukan skor rekaan.
3. **Tidak ada klaim arah dari asumsi.** Pola buy-side menambah skor sesuai
   track record-nya (hit rate searah + kata keyakinan); pola sell-side menjadi
   peringatan dan mengurangi skor.
4. **Level eksekusi selalu punya pembatalan.** Tiap kandidat membawa ``stop``;
   tanpa level pembatalan yang bisa dihitung, kandidat tidak boleh masuk papan.
5. **Skor 0..100 + grade A/B/C.** Grade adalah **label peringkat relatif** dari
   distribusi skor nyata (lihat ``GRADE_*_MIN``) — bukan probabilitas.
6. **Ambang dikalibrasi dari data, bukan feeling.** Lantai likuiditas dan band
   grade diambil dari distribusi nyata (tanggal & sampel tercatat di konstanta);
   outcome-nya diuji oleh ``research.recommendation_log``.

Modul ini **bukan rekomendasi keuangan**; ia meringkas data tersimpan supaya
keputusan bisa diperiksa, bukan dipercaya buta.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

# ------------------------------------------------------------------ konstanta

#: Histori harga minimum (hari bursa) sebelum emiten boleh jadi kandidat.
#: SMA50 + RSI(14) belum bermakna di bawah ini.
MIN_MARKET_HISTORY = 60

#: Lantai likuiditas harian (Rp): nilai transaksi hari terakhir.
#: **Kalibrasi 2026-10-06** (60 sesi terakhir, ~964 emiten dari
#: ``research.latest_pit``): median nilai transaksi harian emiten yang
#: **benar-benar diperdagangkan** ≈ Rp 566 jt (p50 value>0), persentil 55 dari
#: seluruh baris ber-data. Dibulatkan ke bawah → **Rp 500 jt**, yang meloloskan
#: ~435 emiten/sesi (median). Emiten di bawah lantai tidak layak masuk daftar
#: eksekusi, sekuat apa pun sinyalnya.
MIN_DAY_VALUE = 5.0e8

#: Grade = **peringkat relatif pool hari itu**, bukan ambang skor absolut.
#: C/B/A = potongan persentil 75/90/98 pool kandidat sesi tersebut (lihat
#: ``assign_grades``); labelnya menyatakan posisi, bukan peluang.
#:
#: Kenapa peringkat, bukan ambang absolut (diukur ulang 2026-10-09): distribusi
#: skor **ber-titik-massa** — mayoritas kandidat persis di ``NEUTRAL_SCORE``
#: karena tidak ada komponen berbobot positif. Ambang hasil persentil
#: (p75/p90/p98) karena itu jatuh di angka yang sama, dan band B/C terukur
#: **28% / 28%** dari pool padahal labelnya 10% / 25% — angkanya berbohong.
#: Peringkat pool harian selalu memberi 2%/10%/25% seperti yang tertulis di UI.
BAND_SHARES: tuple[float, float, float] = (0.75, 0.90, 0.98)

#: Komposisi per horizon — hasil walk-forward (``research.score_eval``, 3 fold,
#: train 100 -> test 25, 190 sesi, run 2026-10-09). Aturan: gate per fold
#: (|t| NW >= 1,5 & |edge| >= 0,15% & terukur di >= 20 sesi), shrinkage 0,5, lalu
#: stabilitas tanda lintas fold (>= 2/3 fold sependapat). Komponen yang tidak
#: ada di daftar = bobot 0 untuk horizon itu.
#:
#: Catatan jujur yang harus ikut terbaca di UI (bukan cuma di sini):
#:
#: * Bukti OOS pooled — h5 IC +0,010 (t 0,55) · h10 IC **−0,019** (t −1,19) ·
#:   h21 IC **+0,038** (t 1,95, rho desil 0,39). Hanya h21 yang punya sinyal
#:   OOS, dan itulah alasan ``DEFAULT_HORIZON = 21``.
#: * Semua komponen yang lolos gate berbobot **negatif**, jadi skor berarti
#:   "seberapa bersih nama itu dari ciri yang historis merugikan", bukan
#:   "seberapa kuat kandidatnya". Sinyal BUY sendiri tidak lolos gate (edge
#:   +0,27%, t 1,17 di h10) — skor ini **bukan** penguat sinyal BUY.
#: * Komponen pola jejak smart money berbobot 0 di semua horizon **bukan**
#:   karena polanya lemah (``pattern_initiation`` edge +3,19% di h21, pooled
#:   +4,01%), tapi karena datanya belum cukup: ``foreign_net`` baru tersedia
#:   sejak Jul 2026 (~50 sesi) sementara jendela train 100 sesi — tidak ada fold
#:   yang memuat kejadian pola sama sekali, jadi bobotnya tak bisa diukur.
#:   Selama itu pola tampil sebagai alasan/peringatan, bukan poin.
COMPONENT_WEIGHTS: dict[int, dict[str, float]] = {
    5: {
        "rsi_healthy": -1.0,
        "vol_expansion": -1.0,
        "mom20_strong": -3.0,
        "value_large": -1.0,
    },
    10: {
        "rsi_healthy": -1.5,
        "mom20_strong": -4.0,
        "value_large": -2.0,
    },
    21: {
        "signal_hold": -1.0,
        "rsi_pullback": -4.0,
        "mom20_strong": -4.0,
        "high_volatility": -5.0,
        "value_large": -4.5,
    },
}

#: Horizon default untuk skor papan. Dipilih dari bukti OOS: h21 satu-satunya
#: yang IC pooled-nya positif dengan monotonisitas desil searah (lihat di atas).
#: Horizon lain tetap bisa dipakai pemanggil (``score_candidate(horizon=...)``).
DEFAULT_HORIZON = 21

#: Target risk/reward default untuk menghitung level target dari stop.
TARGET_RR = 2.0

#: Jarak stop minimum (fraksi harga) dan pengali volatilitas harian bila stop
#: struktural (low konsolidasi) tidak tersedia.
STOP_MIN_DISTANCE = 0.05
STOP_VOL_MULT = 3.0

#: Batas ukuran posisi default (persen modal) saat sizing sederhana.
DEFAULT_RISK_PCT = 1.0
MAX_POSITION_PCT = 25.0

#: Ambang RSI yang dipakai skor (sejalan dengan band sinyal 20..50 / 50..80).
RSI_HEALTHY_LOW = 45.0
RSI_HEALTHY_HIGH = 70.0
RSI_PULLBACK_LOW = 30.0
RSI_OVERBOUGHT = 75.0
RSI_WEAK = 30.0

#: Ambang kedekatan puncak 52-minggu (%) untuk gaya masuk breakout.
BREAKOUT_NEAR_HIGH_PCT = -3.0

#: CATATAN: pola jejak smart money **tidak** lagi menambah poin skor.
#: Sebelumnya poinnya ditulis tangan dari kata keyakinan (tinggi 12 / sedang 8 /
#: lemah 4) — angka yang tidak pernah diukur dengan skor yang benar-benar
#: dipakai, sehingga band grade tidak lagi menggambarkan distribusi skornya.
#: Pola sekarang masuk lewat bendera komponen (``PATTERN_COMPONENTS``) yang
#: bobotnya datang dari walk-forward — dan sekarang bobotnya 0 karena datanya
#: belum cukup (lihat ``COMPONENT_WEIGHTS``). Sementara itu ia tampil sebagai
#: alasan/peringatan, bukan poin tersembunyi.

#: Horizon yang punya komposisi skor sendiri (hari bursa).
#: Komposisi per horizon itu wajib: edge jangka pendek (5 hari) dan jangka
#: menengah (21 hari) berasal dari sinyal yang berbeda — memakai satu skor untuk
#: semua horizon adalah asumsi, bukan hasil pengukuran.
HORIZONS: tuple[int, ...] = (5, 10, 21)

#: Skor netral. Tanpa komponen yang lolos gate, semua kandidat bernilai 50 dan
#: diurutkan oleh tie-break (R/R) — "tidak ada edge terukur" bukan alasan
#: mengarang poin.
NEUTRAL_SCORE = 50.0

# --- ambang komponen (absolut, tanpa cross-section) -------------------------
#: Semua fitur komponen adalah bendera 0/1 dengan ambang absolut, supaya bisa
#: diukur di harness DAN dihitung saat skoring satu emiten tanpa butuh sebaran
#: hari itu (satu sumber definisi untuk keduanya).
MOM20_STRONG_PCT = 10.0      # momentum 20 sesi >= +10%
HIGH_VOL_DAILY = 0.04        # std return 21 sesi >= 4% per hari
VALUE_LARGE = 5.0e9          # nilai transaksi >= Rp 5 miliar
Z_MID = 1.5                  # harga 1,5 std di atas band 60 sesi
Z_EXTENDED = 2.0             # harga 2 std di atas band 60 sesi

#: Nama komponen skor: bendera -> penjelasan (dipakai alasan di UI, dokumen
#: harness, dan tabel bobot). Komponen di sini adalah SATU-SATUNYA sumber poin;
#: tidak ada penalti yang ditulis tangan di luar daftar ini (lihat
#: ``score_eval``: bebannya ditentukan hasil ukur, bukan opini).
COMPONENT_NOTES: dict[str, str] = {
    "signal_buy": "Sinyal teknikal BUY (SMA20 di atas SMA50, RSI sehat)",
    "signal_hold": "Sinyal teknikal HOLD (belum BUY, belum SELL)",
    "trend_up": "Tren menengah naik (SMA20 di atas SMA50)",
    "rsi_healthy": "RSI 45-70 (momentum tanpa jenuh)",
    "rsi_pullback": "RSI 30-45 saat tren naik (kualitas dip)",
    "rsi_overbought": "RSI di atas 75 (rentan koreksi)",
    "rsi_weak": "RSI di bawah 30 (tekanan jual kuat)",
    "near_52w_high": "Harga dalam 3% puncak 52 minggu",
    "vol_expansion": "Volume >= 1,5x rata-rata 20 sesi",
    "mom20_strong": "Momentum 20 sesi >= +10%",
    "high_volatility": "Volatilitas harian (std 21 sesi) >= 4%",
    "z_extended": "Harga >= 2 std di atas band 60 sesi",
    "z_mid": "Harga 1,5-2 std di atas band 60 sesi",
    "value_large": "Nilai transaksi >= Rp 5 miliar (likuid)",
    "setup_pullback": "Setup entry pullback terkonfirmasi",
    "setup_breakout": "Setup entry breakout terkonfirmasi",
    "pattern_silent_accumulation": "Pola jejak smart money: akumulasi diam-diam",
    "pattern_initiation": "Pola jejak smart money: inisiasi volume + asing",
    "pattern_distribution_on_rally": "Pola jejak smart money: distribusi saat naik",
    "pattern_silent_distribution": "Pola jejak smart money: distribusi diam-diam",
}

#: Nama komponen yang berasal dari pola jejak smart money (dipakai untuk
#: memetakan bendera pola -> komponen skor).
PATTERN_COMPONENTS: dict[str, str] = {
    "silent_accumulation": "pattern_silent_accumulation",
    "initiation": "pattern_initiation",
    "distribution_on_rally": "pattern_distribution_on_rally",
    "silent_distribution": "pattern_silent_distribution",
}

#: Label pola jejak smart money (cerminan label di ``smart_money.detect_patterns``)
#: supaya alasan di daftar kandidat memakai bahasa yang sama dengan banner emiten.
PATTERN_LABELS = {
    "silent_accumulation": "Akumulasi diam-diam",
    "initiation": "Inisiasi volume + asing",
    "distribution_on_rally": "Distribusi saat naik",
    "silent_distribution": "Distribusi diam-diam",
}


def pattern_label(pattern_id: str) -> str:
    """Label human-readable satu pola (fallback ke id bila tak dikenal)."""
    return PATTERN_LABELS.get(str(pattern_id), str(pattern_id))

#: Umur maksimum referensi entry terhadap close (%) sebelum level terlalu
#: jauh dari harga (level basi tidak boleh dipakai).
MAX_ENTRY_DRIFT_PCT = 9.0


def _f(value: Any) -> float | None:
    """Ambil float; None/NaN/inf/invalid -> None (bukan 0)."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _b(value: Any) -> bool:
    return bool(value) if value is not None else False


def validated_horizons() -> tuple[int, ...]:
    """Horizon yang punya minimal satu komponen berbobot (hasil walk-forward)."""
    return tuple(k for k in sorted(COMPONENT_WEIGHTS) if COMPONENT_WEIGHTS[k])


def candidate_rank_key(row: Mapping[str, Any]) -> tuple[float, float, float, str]:
    """Kunci urutan kandidat (menurun): skor -> R/R -> likuiditas -> kode (pure).

    Tie-break-nya **dibukukan** karena komposisi skor sekarang beresolusi kasar
    (banyak kandidat persis di ``NEUTRAL_SCORE``). Tanpa tie-break yang berarti,
    peringkat akan ditentukan urutan kedatangan data. Likuiditas dipakai sebagai
    penyela supaya di antara nama ber-skor sama yang tampil lebih dulu adalah
    yang benar-benar diperdagangkan.
    """
    metrics = row.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    return (
        float(_f(row.get("score")) or 0.0),
        float(_f(row.get("rr")) or 0.0),
        float(_f(metrics.get("value")) or 0.0),
        str(row.get("code") or ""),
    )


def tier_sizes(n: int, shares: tuple[float, float, float] = BAND_SHARES) -> tuple[int, int, int]:
    """Jumlah nama per tier (A, B, C) untuk pool ``n`` — minimal 1 nama per tier.

    Batas minimum 1 menjaga pool kecil tetap terwakili (satu kandidat tetap
    dapat grade A, bukan hilang tanpa penjelasan), dan tiap tier selalu lebih
    lebar dari tier di atasnya.
    """
    total = max(0, int(n))
    a = max(1, round(total * (1.0 - float(shares[2]))))
    b = max(a + 1, round(total * (1.0 - float(shares[1]))))
    c = max(b + 1, round(total * (1.0 - float(shares[0]))))
    return (a, b, c)


def assign_grades(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Beri ``grade`` lewat peringkat relatif pool hari itu (pure).

    Urutan: ``candidate_rank_key`` menurun. Potongan tier dari ``BAND_SHARES``
    (C 25% · B 10% · A 2% teratas). Kandidat di luar 25% teratas **dibuang** —
    itu definisi "masuk papan", bukan filter skor absolut.

    Dipakai jalur papan maupun jalur satu-emiten supaya grade sebuah emiten di
    halaman detail persis sama dengan grade-nya di papan.
    """
    ordered = sorted((dict(c) for c in candidates), key=candidate_rank_key, reverse=True)
    n_a, n_b, n_c = tier_sizes(len(ordered))
    out: list[dict[str, Any]] = []
    for i, c in enumerate(ordered):
        if i < n_a:
            c["grade"] = "A"
        elif i < n_b:
            c["grade"] = "B"
        elif i < n_c:
            c["grade"] = "C"
        else:
            break
        c["rank"] = i + 1
        c["pool"] = len(ordered)
        out.append(c)
    return out


def score_components(
    features: Mapping[str, float],
    horizon: int,
    *,
    extra_points: float = 0.0,
) -> float:
    """Skor satu horizon: ``NEUTRAL + Σ bobot x bendera + poin lapis terukur lain``.

    Bobot berasal dari ``COMPONENT_WEIGHTS`` (walk-forward, lihat docstring-nya).
    Tidak ada poin yang ditulis tangan di luar tabel itu selain ``extra_points``
    (lapis faktor IC / aliran broker — keduanya sudah punya gate IC sendiri dan
    hanya dikirim pemanggil bila benar-benar tervalidasi).
    """
    weights = COMPONENT_WEIGHTS.get(int(horizon), {})
    total = NEUTRAL_SCORE + float(extra_points)
    for name, w in weights.items():
        if w:
            total += float(w) * float(features.get(name, 0.0) or 0.0)
    return round(max(0.0, min(100.0, total)), 1)


def safe_horizon(horizon: int | None) -> int:
    """Horizon yang benar-benar punya komponen berbobot, else horizon default."""
    h = int(horizon) if horizon is not None else int(DEFAULT_HORIZON)
    return h if h in validated_horizons() else int(DEFAULT_HORIZON)


# ------------------------------------------------------------------ entry plan


def entry_plan(
    *,
    close: float | None,
    signal: str | None,
    trend_up: bool | None,
    rsi: float | None,
    bb_lower: float | None,
    dist_52w_pct: float | None,
    vol_ratio: float | None,
    vol_daily: float | None,
    range_low_20: float | None,
) -> dict[str, Any] | None:
    """Level eksekusi untuk satu kandidat: zona entry, stop, target, R/R (pure).

    Tiga gaya masuk, dipilih dari data (bukan asumsi):

    - ``pullback``  : tren menengah masih naik, tapi harga sedang di/di bawah
      band bawah atau RSI di bawah netral — masuk di area diskon dalam tren.
    - ``breakout``  : harga dekat puncak 52-minggu (<= 3%) dan volume
      mengembang (>= 1,5x rata-rata 20 sesi) — masuk saat level ditembus.
    - ``trend``     : tren naik tanpa kondisi khusus di atas — referensi entry
      adalah close terakhir.

    Stop: low 20 sesi terakhir bila level itu masuk akal (di bawah close dan
    tidak lebih jauh dari 25%), kalau tidak ``close * (1 - max(5%, 3 x vol)``.
    Target = ``entry_ref + 2 x (entry_ref - stop)`` (R/R 2,0).

    Returns ``{setup, entry_low, entry_high, entry_ref, stop, target, rr,
    note}`` atau ``None`` bila level tidak bisa dihitung (tanpa close / stop
    tidak valid) — tanpa level pembatalan, kandidat tidak boleh ditampilkan.
    """
    c = _f(close)
    if c is None or c <= 0:
        return None

    bb = _f(bb_lower)
    vol_d = _f(vol_daily)
    rng_low = _f(range_low_20)
    dist = _f(dist_52w_pct)
    vr = _f(vol_ratio)
    r = _f(rsi)
    up = _b(trend_up)

    pullback = up and (
        (bb is not None and c <= bb * 1.02)
        or (r is not None and r <= RSI_HEALTHY_LOW)
    )
    breakout = (
        dist is not None
        and dist >= BREAKOUT_NEAR_HIGH_PCT
        and vr is not None
        and vr >= 1.5
    )

    if pullback:
        setup = "pullback"
        low = bb if bb is not None and 0 < bb <= c else c * 0.98
        entry_low, entry_high = round(low, 2), round(c, 2)
        entry_ref = round((entry_low + entry_high) / 2.0, 2)
        note = (
            "Tren masih naik tapi harga sedang di area diskon — masuk bertahap "
            f"di area Rp {entry_low:,.0f}-{entry_high:,.0f}."
        )
    elif breakout:
        setup = "breakout"
        entry_low, entry_high = round(c, 2), round(c * 1.01, 2)
        entry_ref = round(c, 2)
        note = (
            "Harga menempel puncak 52-minggu dengan volume mengembang — masuk "
            f"saat level Rp {entry_ref:,.0f} ditembus, jangan kejar di atasnya."
        )
    elif up:
        setup = "trend"
        entry_low, entry_high = round(c * 0.99, 2), round(c, 2)
        entry_ref = round(c, 2)
        note = f"Tren naik tanpa kondisi khusus — referensi entry Rp {entry_ref:,.0f}."
    else:
        # Bukan tren naik & bukan setup khusus: tetap boleh dikandidatkan bila
        # skornya kuat (mis. pola akumulasi), tapi gaya masuknya netral.
        setup = "netral"
        entry_low, entry_high = round(c * 0.98, 2), round(c, 2)
        entry_ref = round(c, 2)
        note = (
            "Belum ada konfirmasi tren — entry referensi "
            f"Rp {entry_ref:,.0f}, tunggu konfirmasi arah."
        )

    # --- stop: struktural dulu, volatilitas sebagai cadangan ---
    stop: float | None = None
    if rng_low is not None and 0 < rng_low < c and (c - rng_low) / c <= 0.25:
        stop = rng_low
    else:
        dist_frac = STOP_MIN_DISTANCE
        if vol_d is not None and vol_d > 0:
            dist_frac = max(STOP_MIN_DISTANCE, STOP_VOL_MULT * vol_d)
        if dist_frac >= 0.5:  # volatilitas ekstrem -> level tak bermakna
            return None
        stop = c * (1.0 - dist_frac)

    stop = round(float(stop), 2)
    if stop >= entry_ref or stop <= 0:
        return None

    target = round(entry_ref + TARGET_RR * (entry_ref - stop), 2)
    rr = round((target - entry_ref) / (entry_ref - stop), 2)
    return {
        "setup": setup,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_ref": entry_ref,
        "stop": stop,
        "target": target,
        "rr": rr,
        "note": note,
    }


# ------------------------------------------------------------------ smart money


def smart_money_evidence(
    *,
    buy_patterns: Iterable[str],
    sell_patterns: Iterable[str],
    histories: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Alasan + peringatan dari pola jejak smart money (pure) — **tanpa poin**.

    ``histories`` = peta ``pattern_id -> ringkasan track record`` (bentuk
    keluaran ``smart_money.pattern_history``: ``confidence``, ``horizon_days``,
    ``aligned_hit_rate``, ``reliable``, ``edge_pct``).

    Pola **tidak** menggeser skor di sini: bobotnya, kalau ada, datang dari
    walk-forward lewat bendera komponen (``PATTERN_COMPONENTS``). Yang dilakukan
    fungsi ini adalah menyatakan buktinya dalam kata — termasuk jujur saat
    catatan historisnya masih terlalu tipis untuk diklaim. Pola sisi jual jadi
    peringatan: sinyal jual yang menyala bukan kabar baik untuk kandidat beli.

    Returns ``{reasons, warnings, best_confidence}``.
    """
    hist = histories or {}
    reasons: list[str] = []
    warnings: list[str] = []
    best_conf: str | None = None

    for pid in buy_patterns:
        h = hist.get(str(pid)) or {}
        conf = str(h.get("confidence") or "lemah")
        reliable = bool(h.get("reliable", False))
        label = str(h.get("label") or pattern_label(str(pid)))
        if not reliable:
            reasons.append(
                f"Pola {label} menyala, tapi catatan historisnya masih terlalu "
                "kecil untuk diklaim — tidak menambah skor."
            )
            continue
        rate = _f(h.get("aligned_hit_rate"))
        days = h.get("horizon_days")
        edge = _f(h.get("edge_pct"))
        detail = []
        if rate is not None:
            detail.append(f"{rate * 100:.0f}% searah")
        if days:
            detail.append(f"dalam {days} hari bursa")
        if edge is not None:
            detail.append(f"alpha {edge:+.2f}%")
        tail = f" ({', '.join(detail)})" if detail else ""
        reasons.append(
            f"Pola {label} — keyakinan {conf}{tail}. Bukti ini ditampilkan, "
            "tapi belum ikut menimbang skor (bobotnya belum lolos walk-forward)."
        )
        if best_conf is None or _conf_rank(conf) < _conf_rank(best_conf):
            best_conf = conf

    for pid in sell_patterns:
        h = hist.get(str(pid)) or {}
        label = str(h.get("label") or pattern_label(str(pid)))
        warnings.append(
            f"Pola {label} (sisi jual) juga menyala — sinyal beli jadi lemah."
        )

    return {
        "reasons": reasons,
        "warnings": warnings,
        "best_confidence": best_conf,
    }


def _conf_rank(conf: str) -> int:
    return {"tinggi": 0, "sedang": 1, "lemah": 2}.get(conf, 3)


# ------------------------------------------------------------------ komponen


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
    likuiditas di bawah lantai, histori kurang, atau level eksekusi tak bisa
    dihitung).
    """
    code = str(metrics.get("code") or "").upper()
    if not code:
        return None

    hist_days = _f(metrics.get("hist_days"))
    if hist_days is not None and hist_days < MIN_MARKET_HISTORY:
        return None

    value = _f(metrics.get("value"))
    if value is not None and value < MIN_DAY_VALUE:
        return None

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

    reasons: list[str] = []
    warnings: list[str] = []

    # Bahan alasan berasal dari metrik yang sama yang masuk bobot; masukkan ke
    # ``score_components`` biar alasan dibuang hanya komponen yang benar-benar
    # berbobot untuk horizon yang dipunyai.
    features = candidate_features(metrics, setup=plan["setup"])

    ev = smart_money_evidence(
        buy_patterns=metrics.get("pattern_buy") or [],
        sell_patterns=metrics.get("pattern_sell") or [],
        histories=histories,
    )

    # Satu horizon per pemanggilan: skor dari skala berbeda tidak boleh diadu
    # dalam satu urutan (lihat docstring).
    h = safe_horizon(horizon)

    # Hanya lapisan yang sudah lolos gate sendiri (IC / aliran broker) yang
    # menggeser skor; yang belum tervalidasi tidak dikirim sebagai nol tersamar.
    layer_points = 0.0
    if factor_validated and factor_adj:
        layer_points += float(factor_adj)
    if broker_validated and broker_adj:
        layer_points += float(broker_adj)

    score = score_components(features, h, extra_points=layer_points)

    reasons.extend(ev["reasons"])
    warnings.extend(ev["warnings"])

    # --- 2) lapisan faktor IC / aliran broker (hanya bila tervalidasi) -----
    # Sudah masuk ``layer_points`` di atas; di sini hanya jejaknya untuk dibaca
    # pengguna. Ketidakhadiran lapisan tidak diklaim sebagai nol — ia disebut
    # sebagai tidak aktif oleh ``layers`` di payload API.
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

    # Catatan penalti risiko tetap masuk alasan ("risk zone": z-score tinggi =
    # harga jauh dari band 60 sesi; volatilitas tinggi = ATR besar). Tampilkan di
    # bawah kondisi yang relevan.
    z = _f(metrics.get("z_score"))
    if z is not None:
        if z >= 2.0:
            warnings.append(f"Harga jauh di atas band 60-hari (z {z:+.1f}) — overbought.")
        elif z >= 1.5:
            warnings.append(f"Harga di atas band 60-hari (z {z:+.1f}) — waspada.")

    atr = _f(metrics.get("atr_pct"))
    if atr is not None and atr >= 8.0:
        warnings.append(f"Volatilitas harian tinggi (ATR {atr:.1f}%) — perbesar jarak stop.")

    layers = {
        "factor": bool(factor_validated),
        "broker": bool(broker_validated),
        "smart_money": bool(ev["reasons"] or ev["warnings"]),
    }
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
        # Horizon eksplisit = horizon komposisi skor yang dipakai, supaya pembaca
        # tahu klaim ini diukur pada jangka berapa (lihat COMPONENT_WEIGHTS).
        "horizon_days": h,
        "horizon_units": "hari bursa",
        "confidence": ev["best_confidence"],
        "patterns": list(metrics.get("pattern_buy") or []),
        "reasons": reasons,
        "warnings": warnings,
        "layers": layers,
        "metrics": {
            "close": close,
            "signal": signal,
            "trend_up": _b(metrics.get("trend_up")),
            "rsi": _f(metrics.get("rsi")),
            "mom_20d": _f(metrics.get("mom_20d")),
            "atr_pct": atr,
            "dist_52w_pct": _f(metrics.get("dist_52w_pct")),
            "vol_ratio": _f(metrics.get("vol_ratio")),
            "value": value,
            "z_score": z,
            "hist_days": hist_days,
        },
    }


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
