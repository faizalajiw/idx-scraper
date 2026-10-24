"""Kontrak entrypoint API :8000 — temuan 20 Okt 2026: pendengar mati diam-diam.

Bukti lapangan (reproduksi 3000 koneksi RST di port uji):
- Loop Proactor (setelan `python -m uvicorn` yang dipakai `start-api.bat`):
  `OSError: [WinError 64]` di `asyncio/windows_events.py:finish_accept`, lalu
  `Accept failed on a socket` -> asyncio MENUTUP socket pendengar dan berhenti
  accept, sementara event loop dan proses tetap hidup. Hasilnya zombie: proses
  ada, :8000 kosong, `_api.log` masih dipegang -> peluncuran berikutnya gagal
  diam-diam.
- Loop Selector: badai yang sama, server tetap melayani 3/3 permintaan.

Karena itu kontraknya:
- `jalankan` menjalankan uvicorn di atas loop Selector (Windows).
- `port_hidup` adalah dasar pengawas: bila server sudah `started` tapi port
  berhenti menjawab `gagal_maks` kali beruntun, proses menyerahkan diri lewat
  `os._exit(3)` supaya launcher menyalakan ulang — jaring kedua kalau ada mode
  gagal lain yang belum terpikirkan.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from idx_scraper.api import serve
from idx_scraper.api.serve import _minta_ulang, _pengawas, jalankan, port_hidup


def _port_kosong() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_port_hidup_false_untuk_port_kosong() -> None:
    assert port_hidup("127.0.0.1", _port_kosong(), timeout=0.3) is False


def test_pengawas_tidak_ambil_alih_selama_port_menjawab() -> None:
    server = SimpleNamespace(started=True, should_exit=False)
    diambil: list[int] = []

    def cek(host: str, port: int, timeout: float = 1.0) -> bool:
        return True

    t = threading.Thread(
        target=_pengawas,
        args=(server, "127.0.0.1", 1, 0.01, 2),
        kwargs={"cek": cek, "ambil_alih": lambda: diambil.append(1)},
        daemon=True,
    )
    t.start()
    time.sleep(0.2)
    server.should_exit = True
    t.join(timeout=2)
    assert diambil == []


def test_pengawas_ambil_alih_setelah_gagal_beruntun() -> None:
    server = SimpleNamespace(started=True, should_exit=False)
    diambil: list[int] = []

    def cek(host: str, port: int, timeout: float = 1.0) -> bool:
        return False

    t = threading.Thread(
        target=_pengawas,
        args=(server, "127.0.0.1", 1, 0.01, 3),
        kwargs={"cek": cek, "ambil_alih": lambda: diambil.append(1)},
        daemon=True,
    )
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert diambil == [1]


def test_minta_ulang_menyetel_should_exit(monkeypatch) -> None:
    """Ambil alih = minta uvicorn berhenti rapi supaya supervisor mengulang.

    `_minta_ulang` memasang thread paksa yang memanggil `os._exit(3)` setelah
    `tunggu` detik. Thread itu daemon dan tidak bisa dibatalkan, jadi tes ini
    mengganti `os._exit` dengan pencatat: kalau tidak, sisa suite yang berjalan
    lebih lama dari `tunggu` akan dimatikan dengan exit 3 (gejala yang muncul
    saat suite dijalankan dengan coverage, yang memperlambat semua tes).
    """
    server = SimpleNamespace(should_exit=False)
    keluar: list[int] = []
    monkeypatch.setattr(serve.os, "_exit", lambda code: keluar.append(code))

    _minta_ulang(server, tunggu=0.05)
    assert server.should_exit is True

    batas = time.monotonic() + 3.0
    while not keluar and time.monotonic() < batas:
        time.sleep(0.02)
    assert keluar == [3]


def test_jalankan_berhenti_bila_tidak_ada_permintaan_ulang(monkeypatch) -> None:
    dibuat: list[object] = []

    class ServerPalsu:
        def __init__(self, config) -> None:
            self.started = True
            self.should_exit = False
            dibuat.append(self)

        async def serve(self) -> None:
            return None

    def run_palsu(coro, loop_factory=None) -> None:
        coro.close()

    monkeypatch.setattr(serve.uvicorn, "Server", ServerPalsu)
    monkeypatch.setattr(serve.uvicorn, "Config", lambda app, **kw: None)
    monkeypatch.setattr(serve.asyncio, "run", run_palsu)
    monkeypatch.setattr(serve, "_pengawas", lambda *a, **k: None)

    jalankan(tidur_ulang=0)
    assert len(dibuat) == 1


def test_jalankan_menyalakan_ulang_lalu_menyerah(monkeypatch) -> None:
    dibuat: list[object] = []

    class ServerPalsu:
        def __init__(self, config) -> None:
            self.started = True
            self.should_exit = False
            dibuat.append(self)

        async def serve(self) -> None:
            return None

    def run_palsu(coro, loop_factory=None) -> None:
        coro.close()

    def pengawas_palsu(server, host, port, jeda, gagal_maks, ambil_alih=None) -> None:
        ambil_alih()

    monkeypatch.setattr(serve.uvicorn, "Server", ServerPalsu)
    monkeypatch.setattr(serve.uvicorn, "Config", lambda app, **kw: None)
    monkeypatch.setattr(serve.asyncio, "run", run_palsu)
    monkeypatch.setattr(serve, "_pengawas", pengawas_palsu)
    monkeypatch.setattr(serve, "_minta_ulang", lambda srv, tunggu=10.0: None)

    with pytest.raises(SystemExit) as keluar:
        jalankan(tidur_ulang=0, ulang_maks=3)

    assert keluar.value.code == 4
    assert len(dibuat) == 4  # percobaan pertama + 3 ulang


SCRIPT_PROBE = textwrap.dedent(
    """
    import sys

    from idx_scraper.api.serve import jalankan

    class App:
        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"2")]})
            await send({"type": "http.response.body", "body": b"ok"})

    jalankan(
        app=App(),
        host="127.0.0.1",
        port=int(sys.argv[1]),
        jeda_pengawas=5.0,
        gagal_maks=10,
    )
    """
)


def _tunggu_siap(port: int, proses: subprocess.Popen, batas: float = 30.0) -> None:
    akhir = time.monotonic() + batas
    while time.monotonic() < akhir:
        if proses.poll() is not None:
            keluaran = proses.stdout.read() if proses.stdout else ""
            pytest.fail(f"server uji mati lebih awal (exit {proses.returncode}):\n{keluaran}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"server uji tidak siap dalam {batas:.0f} detik")


def _badai_reset(port: int, jumlah: int = 3000, utas: int = 8) -> None:
    """Koneksi lalu RST (SO_LINGER 0) — pemicu `AcceptEx` gagal di proactor."""

    def churn() -> None:
        for i in range(jumlah // utas):
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                pass
            finally:
                s.close()
            if i % 40 == 0:
                time.sleep(0.01)

    daftar = [threading.Thread(target=churn) for _ in range(utas)]
    for t in daftar:
        t.start()
    for t in daftar:
        t.join()


@pytest.mark.skipif(os.name != "nt", reason="pola gagal accept-loop khusus Windows")
def test_pendengar_bertahan_setelah_klien_mereset_koneksi(tmp_path) -> None:
    port = _port_kosong()
    skrip = tmp_path / "api_probe.py"
    skrip.write_text(SCRIPT_PROBE, encoding="utf-8")

    proses = subprocess.Popen(
        [sys.executable, str(skrip), str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _tunggu_siap(port, proses)
        _badai_reset(port)
        time.sleep(0.5)
        for _ in range(3):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=15) as r:
                assert r.status == 200
            time.sleep(0.1)
    finally:
        proses.kill()
        proses.wait(timeout=10)
