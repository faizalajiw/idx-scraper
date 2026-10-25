"""Kalibrasi walk-forward komposisi skor kandidat beli (pure + DB + CLI).

Menjawab pertanyaan yang menentukan apakah skor Rekomendasi layak dipercaya:
**"komponen mana yang benar-benar punya edge, berapa bobotnya, dan untuk horizon
berapa — diukur di data yang BELUM dilihat saat bobotnya ditentukan?"**

Kenapa modul ini ada
--------------------
AUDIT §15 menunjukkan ambang grade hasil kalibrasi **belum monoton** terhadap
abnormal return: skor lama adalah campuran poin yang ditulis tangan (sinyal
teknikal +30, tren +12, RSI +8, penalti −12, ...) tanpa bukti out-of-sample
bahwa setiap potongan poin itu bergerak searah hasil. Ambangnya sudah
dikalibrasi; **komposisinya belum pernah diuji**. Modul ini menguji dan
menentukan ulang komposisinya per horizon.

Aturan yang dipegang (lihat skill ``backtesting-discipline``)
-------------------------------------------------------------
1. **Satu definisi komponen.** Fitur diambil dari
   ``recommend.candidate_features`` — fungsi yang sama yang menyusun skor nyata.
   Kalau definisinya berbeda, bobot hasil ukur tidak menggambarkan skor yang
   dipakai.
2. **Walk-forward, bukan in-sample.** Tiap fold: ukur edge komponen **hanya di
   jendela train**, lalu terapkan bobotnya ke jendela test berikutnya yang belum
   dilihat. Jendela test tidak tumpang tindih.
3. **Eksekusi T+1, abnormal vs pasar.** Outcome memakai ``signal_log
   .attach_outcomes`` (entry close T+1, ``abn_k`` = return dikurangi pasar
   equal-weight window yang sama) — disiplin yang sama dengan ``signal_log``,
   ``events`` dan ``recommendation_log``.
4. **Gate, bukan keyakinan.** Komponen hanya dapat bobot bila |t| Newey-West
   >= ``EDGE_GATE_T`` DAN |edge| >= ``MIN_EDGE`` di train. Yang tidak lolos
   berbobot 0 — bukan dibulatkan jadi poin kecil.
5. **Shrinkage eksplisit.** Sampel hanya ~1 tahun; bobot train dikalikan
   ``SHRINK`` sebelum dipakai. Edge hasil ukur yang tipis tidak boleh jadi
   keyakinan penuh.
5b. **Stabilitas tanda lintas fold.** Komponen yang lolos gate di satu fold
   tapi berbalik tanda di fold lain dimatikan (``stable_weights``): bobot = 0
   beserta alasannya, bukan dibulatkan jadi poin kecil.
6. **Horizon terpisah.** Komposisi diukur & ditentukan per horizon
   (5/10/21 hari). Satu skor untuk semua horizon adalah asumsi, bukan hasil.

Cara baca hasilnya
------------------
Angka yang **menjadi bukti** adalah statistik OOS per fold (bagian
``walk_forward``); jalur ``frozen`` hanya deskriptif karena bobotnya
diturunkan dari sampel yang sama. Kalau tidak ada komponen yang lolos gate,
hasilnya sah: skor jatuh ke netral (``NEUTRAL_SCORE``) dan itu dilaporkan apa
adanya, bukan ditambal dengan poin rekaan.

Contoh pakai:
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m idx_scraper.research.score_eval \\
        --horizons 5 10 21 --train 100 --test 25 --json out_score_eval.json
"""

from __future__ import annotations

from .bobot import (
    _round_opt,
    edge_table,
    score_frame,
    tier_mix,
    weights_from_edges,
)
from .evaluasi import (
    _spearman,
    decile_table,
    frozen_eval,
    monotonicity,
    score_ic,
    session_dates,
    stable_weights,
    walk_forward,
)
from .fitur import (
    attach_abnormal,
    benchmark_prices,
    candidate_universe,
    feature_frame,
)
from .laporan import (
    format_weights,
    load_features,
    main,
    print_report,
)
from .param import (
    _ABN,
    _FEATURES,
    BAND_SHARES,
    EDGE_GATE_T,
    EDGE_TO_POINTS,
    MAX_COMPONENT_POINTS,
    MIN_EDGE,
    MIN_EDGE_DAYS,
    MIN_FOLD_CONSISTENCY,
    MIN_NAMES_PER_DAY,
    SHRINK,
    STEP_SESSIONS,
    TEST_SESSIONS,
    TRAIN_SESSIONS,
    logger,
)
