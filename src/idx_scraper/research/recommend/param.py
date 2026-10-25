"""Konstanta ambang, helper konversi, dan label pola."""

from __future__ import annotations

import math
from typing import Any

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
MOM20_STRONG_PCT = 10.0  # momentum 20 sesi >= +10%
HIGH_VOL_DAILY = 0.04  # std return 21 sesi >= 4% per hari
VALUE_LARGE = 5.0e9  # nilai transaksi >= Rp 5 miliar
Z_MID = 1.5  # harga 1,5 std di atas band 60 sesi
Z_EXTENDED = 2.0  # harga 2 std di atas band 60 sesi

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
