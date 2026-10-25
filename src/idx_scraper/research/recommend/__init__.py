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

from .bukti import (
    _conf_rank,
    smart_money_evidence,
)
from .param import (
    BAND_SHARES,
    BREAKOUT_NEAR_HIGH_PCT,
    COMPONENT_NOTES,
    COMPONENT_WEIGHTS,
    DEFAULT_HORIZON,
    DEFAULT_RISK_PCT,
    HIGH_VOL_DAILY,
    HORIZONS,
    MAX_ENTRY_DRIFT_PCT,
    MAX_POSITION_PCT,
    MIN_DAY_VALUE,
    MIN_MARKET_HISTORY,
    MOM20_STRONG_PCT,
    NEUTRAL_SCORE,
    PATTERN_COMPONENTS,
    PATTERN_LABELS,
    RSI_HEALTHY_HIGH,
    RSI_HEALTHY_LOW,
    RSI_OVERBOUGHT,
    RSI_PULLBACK_LOW,
    RSI_WEAK,
    STOP_MIN_DISTANCE,
    STOP_VOL_MULT,
    TARGET_RR,
    VALUE_LARGE,
    Z_EXTENDED,
    Z_MID,
    _b,
    _f,
    pattern_label,
    validated_horizons,
)
from .peringkat import (
    RANK_BASIS,
    assign_grades,
    candidate_rank_key,
    confidence_label,
    own_history_percentile,
    pool_percentile,
    rank_basis,
    safe_horizon,
    score_components,
    tier_sizes,
)
from .rencana import (
    entry_plan,
)
from .skor import (
    candidate_features,
    position_size_pct,
    rank_candidates,
    score_candidate,
)
