"""Parameter walk-forward + logger paket (nama logger lama dipertahankan)."""

from __future__ import annotations

import logging

from ..recommend import (
    COMPONENT_NOTES,
)

logger = logging.getLogger("idx_scraper.research.score_eval")

# --- parameter walk-forward ------------------------------------------------- #

TRAIN_SESSIONS = 100  # panjang jendela train (hari bursa)
TEST_SESSIONS = 25  # panjang jendela test per fold (tidak tumpang tindih)
STEP_SESSIONS = 25  # langkah antar fold

MIN_NAMES_PER_DAY = 20  # sesi dengan kandidat lebih sedikit dari ini dilewati
MIN_EDGE_DAYS = 20  # bendera harus terukur di >= N sesi agar boleh dapat bobot
EDGE_GATE_T = 1.5  # |t| Newey-West minimum (di train) agar komponen dapat bobot
MIN_EDGE = 0.0015  # |edge| minimum (0,15%) agar komponen dapat bobot
EDGE_TO_POINTS = 600.0  # 1% edge -> 6 poin skor
MAX_COMPONENT_POINTS = 15.0
SHRINK = 0.5  # pengecilan bobot train (sampel pendek)
MIN_FOLD_CONSISTENCY = 2 / 3  # bagian fold minimum yang harus sependapat tanda
BAND_SHARES = (0.75, 0.90, 0.98)  # bagian pool harian untuk band C / B / A

_ABN = "abn"  # prefix kolom outcome di attach_outcomes

_FEATURES: tuple[str, ...] = tuple(COMPONENT_NOTES)
