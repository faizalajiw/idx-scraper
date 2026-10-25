"""FastAPI application entrypoint.

Run locally (dev, dengan reload):
    uvicorn idx_scraper.api.app:app --reload --port 8000

Untuk layanan yang harus hidup terus (task harian `IDX-API`), jangan pakai
perintah di atas: accept-loop proactor Windows mematikan pendengar pada
`WinError 64` dan menyisakan zombie tanpa port. Gunakan
`python -m idx_scraper.api.serve --port 8000` (loop Selector + pengawas; lihat
`api/serve.py`).

This API is READ-ONLY. It does not ingest or mutate data — the APScheduler CLI
worker remains the sole writer. There is currently NO authentication on these
endpoints; they are intended for local/trusted-network use. Before exposing this
service publicly, add authentication/authorization and rate limiting.
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from idx_scraper import watchlist_store

from ..logging_setup import say
from . import analytics, services
from .config import get_settings
from .database import close_pool, init_pool
from .routes import aliran, emiten, operasional, pasar, rekomendasi, riset

# Parameter warm-up papan: harus sama dengan yang diminta web
# (`useRecommendations(limit = 20, minGrade = "C")` di `lib/hooks.ts`), karena
# key cache-nya (`recommendations:{limit}:{min_grade}`) memuat keduanya.
BOARD_WARM_LIMIT = 20
BOARD_WARM_GRADE = "C"


def _catat(pesan: str) -> None:
    """Tulis satu baris ke log startup, di-flush langsung.

    Tanpa ``flush`` barisnya bisa tidak pernah muncul: saat stdout diarahkan ke
    berkas atau pipa (cara ``serve`` dan launcher menjalankannya), Python memakai
    buffer blok, jadi barisnya baru ikut tertulis ketika buffernya penuh - dan
    hilang total kalau prosesnya dibunuh sebelum itu. Sudah kejadian: log uji
    hanya memuat dua dari enam baris warm-up walau keenam tugasnya selesai.
    """
    say(pesan, flush=True)


def _warm_heavy_caches() -> None:
    """Hangatkan cache berat sekali saat startup (background thread, fire-and-forget).

    Enam cache dijalankan BERURUTAN di satu thread, papan kandidat paling depan.
    Versi sebelumnya menjalankan lima thread bersamaan dan **tidak pernah
    menghangatkan papan kandidat**, jadi halaman /rekomendasi tetap membayar
    penuh ~44 detik di kunjungan pertama - dan setiap pengunjung yang datang
    bersamaan membayarnya masing-masing, karena jalur itu belum punya
    hitung-sekali. Berurutan juga menyingkirkan rebutan: di port uji, begitu
    papan dimasukkan ke kumpulan yang jalan bersamaan, permintaan papan yang
    datang di tengah warm-up menunggu 298 detik.

    Event study (paling mahal, ~2 menit dingin) ditaruh paling belakang supaya
    tidak menahan yang lain. Kalau ada yang gagal (DB belum siap dsb.), cache
    terisi otomatis di permintaan berikutnya; tidak ada jalur yang menahan
    permintaan.

    Jangan dipasangi ``@asynccontextmanager``. Versi lama memang berdekorator
    itu, dan bodynya **tetap jalan** - ``contextlib`` memanggil fungsinya saat
    context manager dibuat, jadi kelima thread hangatnya memang menyala dan
    baris ``[warmup]`` sudah lama ada di ``_api.log``. Yang rusak adalah
    pulangannya: CM dengan ``gen=None``, yang meledak begitu ada yang menulis
    ``async with``. Dekoratornya jadi bom waktu, bukan penyebab matinya warm-up.
    """
    import threading

    def _run_events() -> None:
        try:
            analytics._event_study_bundle()
            _catat("[warmup] event bundle siap")
        except Exception as e:
            _catat(f"[warmup] event bundle dilewati: {e}")

    def _run_broker_snapshot() -> None:
        # Snapshot skor broker (1,3 detik saat dingin, terukur 19 Okt 2026)
        # dipakai Hold Check & Ruang Keputusan; hangatkan supaya permintaan
        # pertama tidak menanggung biaya.
        try:
            analytics.broker_activity_snapshot()
            _catat("[warmup] broker snapshot siap")
        except Exception as e:
            _catat(f"[warmup] broker snapshot dilewati: {e}")

    def _run_screener() -> None:
        # Baris screener (20,7 detik saat dingin, terukur 19 Okt 2026) dipakai
        # halaman Screener sama sekali tanpa filter; hangatkan agar kunjungan
        # pertama cepat.
        try:
            analytics._screener_rows()
            _catat("[warmup] screener rows siap")
        except Exception as e:
            _catat(f"[warmup] screener rows dilewati: {e}")

    def _run_valuation() -> None:
        # Valuasi seluruh pasar (14,2 detik saat dingin, terukur 19 Okt 2026:
        # satu query per emiten untuk 980 emiten) dipakai halaman Valuasi dan
        # gabungan verdict di Ruang Keputusan.
        try:
            analytics.get_valuation()
            _catat("[warmup] valuation siap")
        except Exception as e:
            _catat(f"[warmup] valuation dilewati: {e}")

    def _run_hold_check() -> None:
        # Verdict watchlist default (12,8 detik saat dingin, terukur 19 Okt
        # 2026) dipakai halaman Hold Check.
        try:
            services.get_hold_check(watchlist_store.read_watchlist())
            _catat("[warmup] hold check siap")
        except Exception as e:
            _catat(f"[warmup] hold check dilewati: {e}")

    def _run_board() -> None:
        # Papan kandidat (44 detik saat dingin, terukur 19 Okt 2026) dipakai
        # halaman /rekomendasi; angka itu juga yang bikin seluruh halaman
        # tertahan antre koneksi browser kalau cache-nya kosong, jadi ia
        # dihangatkan lebih dulu daripada yang lain.
        try:
            analytics.get_recommendations(limit=BOARD_WARM_LIMIT, min_grade=BOARD_WARM_GRADE)
            _catat("[warmup] papan kandidat siap")
        except Exception as e:
            _catat(f"[warmup] papan kandidat dilewati: {e}")

    def _jalan() -> None:
        for tugas in (
            _run_board,
            _run_broker_snapshot,
            _run_valuation,
            _run_screener,
            _run_hold_check,
            _run_events,
        ):
            tugas()

    threading.Thread(target=_jalan, daemon=True, name="warmup").start()


async def lifespan(app: FastAPI):
    init_pool()
    _warm_heavy_caches()
    yield
    close_pool()


app = FastAPI(
    title="IDX Scraper API",
    version="0.1.0",
    description="Read-only market data & analytics for the IDX dashboard.",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


def _resolve_codes(codes: str | None) -> list[str]:
    if codes:
        return list(dict.fromkeys(c.strip().upper() for c in codes.split(",") if c.strip()))
    return settings.watchlist


def _parse_optional_date(value: str | None, field: str) -> date | None:
    """Parse a YYYY-MM-DD query/body value, or None when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} harus format YYYY-MM-DD") from None


# --------------------------------------------------------------- registrasi rute
# Urutan pemanggilan menentukan urutan pencocokan path — jangan diacak.
# Rute pasar & broker: kesehatan data, overview, regime, leaders, broker.
pasar.route.register(app)
# Rute per emiten: pencarian, watchlist, hold-check, sinyal, harga.
emiten.route.register(app)
# Rute rekomendasi: papan kandidat, track record, keputusan.
rekomendasi.route.register(app)
# Rute aliran: flow asing, jejak smart money, dossier, kepemilikan.
aliran.route.register(app)
# Rute riset: sektor, berita, valuasi, screener, sentimen, faktor.
riset.route.register(app)
# Rute operasional: alert Telegram, buku klaim, backtest.
operasional.route.register(app)
