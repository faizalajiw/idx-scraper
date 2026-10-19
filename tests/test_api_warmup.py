"""Tests warm-up API dan hitung-sekali di jalur papan (temuan operasional 19 Okt 2026).

Kontrak:
- ``app._warm_heavy_caches`` menyalakan SATU thread hangat, berurutan, dengan
  papan kandidat paling depan. Versi sebelumnya menjalankan lima thread
  bersamaan tanpa papan sama sekali, jadi halaman /rekomendasi membayar penuh
  ~44 detik di kunjungan pertama (terukur) dan tiap pengunjung bersamaan
  membayarnya sendiri-sendiri.
- ``app._warm_heavy_caches`` adalah fungsi biasa yang memulangkan ``None``.
  Versi lama dipasangi ``@asynccontextmanager``; bodynya tetap jalan
  (``contextlib`` memanggil fungsinya saat CM dibuat - terbukti dari baris
  ``[warmup]`` yang sudah lama ada di ``_api.log``), tapi pulangannya CM rusak
  dengan ``gen=None`` yang meledak kalau dipakai ``async with``. Tes
  ``test_warm_heavy_caches_fungsi_biasa`` menjaga jebakan itu.
- Enam hitungan berat yang jalan SERENTAK saling menghambat (pool API 10
  koneksi, event study 2 menit): di port uji, permintaan pertama papan menunggu
  298 detik. Karena itu urutannya dikunci di tes ini.
- ``analytics.research_cache_once`` menghitung sekali untuk satu key walau banyak
  permintaan datang bersamaan, dan tidak pernah menahan pemanggil selamanya
  (gagal-terbuka kalau yang menghitung mati atau melempar).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from idx_scraper.api import analytics, app, database

URUTAN_WARMUP = ["papan", "broker", "valuation", "screener", "hold", "event"]


@pytest.fixture(autouse=True)
def _cache_bersih():
    """Tiap tes mulai dari cache kosong dan tidak meninggalkan key in-flight."""
    analytics.clear_research_cache()
    yield
    analytics.clear_research_cache()
    assert analytics._INFLIGHT == {}


class ThreadPalsu:
    """Pengganti ``threading.Thread`` yang mencatat namanya dan bisa langsung jalan."""

    def __init__(self, target=None, name=None, daemon=None, **kw):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        daftar.append(self.name)
        if jalankan:
            self.target()


daftar: list[str] = []
jalankan = False


@pytest.fixture
def thread_palsu(monkeypatch):
    global daftar, jalankan
    daftar = []
    jalankan = False
    monkeypatch.setattr(threading, "Thread", ThreadPalsu)
    return daftar


def test_warmup_dijalankan_saat_startup(thread_palsu):
    """Warm-up menyalakan tepat satu thread bernama ``warmup``."""
    app._warm_heavy_caches()
    assert thread_palsu == ["warmup"]


def test_warm_heavy_caches_fungsi_biasa(thread_palsu):
    """Pulangannya ``None``, bukan context manager.

    Versi lama dipasangi ``@asynccontextmanager``. Bodynya jalan juga (jadi
    warm-up-nya tidak pernah mati), tapi yang dipulangkan adalah CM rusak
    dengan ``gen=None``; begitu ada yang menulis ``async with``, panggilan itu
    meledak dengan TypeError. Fungsi biasa memulangkan None, CM tidak.
    """
    assert app._warm_heavy_caches() is None


def test_warmup_berurutan_dengan_papan_paling_depan(thread_palsu, monkeypatch):
    """Enam hitungan berat jalan berurutan di satu thread, papan lebih dulu."""
    global jalankan
    jalankan = True
    urutan: list[str] = []
    papan: dict[str, object] = {}

    def catat(nama: str):
        def _f(*a, **kw):
            urutan.append(nama)

        return _f

    def papan_palsu(*, limit, min_grade):
        urutan.append("papan")
        papan.update(limit=limit, min_grade=min_grade)
        return {"rows": []}

    monkeypatch.setattr(analytics, "_event_study_bundle", catat("event"))
    monkeypatch.setattr(analytics, "broker_activity_snapshot", catat("broker"))
    monkeypatch.setattr(analytics, "_screener_rows", catat("screener"))
    monkeypatch.setattr(analytics, "get_valuation", catat("valuation"))
    monkeypatch.setattr(app.services, "get_hold_check", catat("hold"))
    monkeypatch.setattr(app.watchlist_store, "read_watchlist", lambda: ["BBRI"])
    monkeypatch.setattr(analytics, "get_recommendations", papan_palsu)

    app._warm_heavy_caches()

    assert urutan == URUTAN_WARMUP
    assert papan == {"limit": app.BOARD_WARM_LIMIT, "min_grade": app.BOARD_WARM_GRADE}


def test_catat_selalu_flush(monkeypatch):
    """Baris warm-up harus di-flush: stdout yang diarahkan ke berkas itu
    block-buffered, jadi tanpa flush barisnya bisa tidak pernah muncul di log."""
    panggilan: list[dict] = []

    def print_palsu(*args, **kwargs):
        panggilan.append({"args": args, **kwargs})

    monkeypatch.setattr("builtins.print", print_palsu)

    app._catat("halo")

    assert panggilan == [{"args": ("halo",), "flush": True}]


def test_hitung_sekali_satu_kali_untuk_banyak_pemanggil():
    """Empat pemanggil bersamaan, satu hitungan; sesudahnya dilayani cache."""
    kunci = threading.Lock()
    mulai = threading.Barrier(4)
    jumlah = 0

    def lambat() -> dict:
        nonlocal jumlah
        with kunci:
            jumlah += 1
        time.sleep(0.2)
        return {"nilai": 42}

    hasil: list[dict] = []
    pengunci = threading.Lock()

    def pekerja() -> None:
        mulai.wait(timeout=5)
        keluar = analytics.research_cache_once("uji:serentak", lambat, ttl=60.0)
        with pengunci:
            hasil.append(keluar)

    threads = [threading.Thread(target=pekerja) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert jumlah == 1
    assert len(hasil) == 4
    assert all(h == {"nilai": 42} for h in hasil)
    assert analytics.research_cache_once("uji:serentak", lambat, ttl=60.0) == {"nilai": 42}
    assert jumlah == 1


def test_hitung_sekali_gagal_terbuka_kalau_penunggu_kelamaan():
    """Penunggu yang kehabisan waktu menghitung sendiri, tidak menggantung."""
    sedang_jalan = threading.Event()
    jumlah = 0
    kunci = threading.Lock()

    def lambat() -> dict:
        nonlocal jumlah
        with kunci:
            jumlah += 1
        sedang_jalan.set()
        time.sleep(0.4)
        return {"nilai": "lambat"}

    pemilik = threading.Thread(
        target=lambda: analytics.research_cache_once("uji:lambat", lambat, ttl=60.0, wait=0.05)
    )
    pemilik.start()
    assert sedang_jalan.wait(timeout=5)

    keluar = analytics.research_cache_once("uji:lambat", lambat, ttl=60.0, wait=0.05)
    pemilik.join(timeout=10)

    assert keluar == {"nilai": "lambat"}
    assert jumlah == 2


def test_hitung_sekali_melepas_kunci_saat_melempar():
    """Hitungan yang melempar tidak menyandera pemanggil berikutnya."""
    panggilan = 0

    def rusak() -> dict:
        nonlocal panggilan
        panggilan += 1
        raise RuntimeError("gagal baca data")

    with pytest.raises(RuntimeError):
        analytics.research_cache_once("uji:rusak", rusak, ttl=60.0)

    assert "uji:rusak" not in analytics._INFLIGHT
    assert analytics.research_cache_once("uji:rusak", lambda: {"ok": True}) == {"ok": True}
    assert panggilan == 1


class PoolPalsu:
    """Pengganti ``ConnectionPool`` supaya tes tidak menyentuh DB nyata."""

    def __init__(self, **kw):
        self.kw = kw


def test_init_pool_mendaftarkan_penutup(monkeypatch):
    """Penutup pool didaftarkan di dalam ``init_pool`` supaya semua entrypoint bersih."""
    didaftarkan: list = []
    monkeypatch.setattr(database, "_pool", None)
    monkeypatch.setattr(database, "ConnectionPool", PoolPalsu)
    monkeypatch.setattr(database.atexit, "register", lambda fn: didaftarkan.append(fn))
    monkeypatch.setattr(
        database,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://uji/uji", pool_min=1, pool_max=2),
    )

    pool = database.init_pool()

    assert isinstance(pool, PoolPalsu)
    assert didaftarkan == [database.close_pool]
