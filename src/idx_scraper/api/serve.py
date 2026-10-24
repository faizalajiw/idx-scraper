"""Entrypoint API :8000: loop Selector di Windows + pengawas port.

Kenapa bukan `python -m uvicorn idx_scraper.api.app:app`: di Windows uvicorn
memakai ProactorEventLoop, dan accept-loop proactor MENUTUP socket pendengar
pada OSError pertama — mis. `WinError 64` ketika klien mereset koneksi sebelum
`AcceptEx` selesai. Event loop dan prosesnya tetap hidup, jadi hasilnya zombie:
proses ada, :8000 kosong, dan `_api.log` masih dipegang proses itu sehingga
baris `python ... >> _api.log` di `start-api.bat` gagal membuka berkas dan
uvicorn baru tidak pernah menyala (senyap). Loop Selector tidak memakai
`AcceptEx`, jadi tidak punya pola gagal itu — terbukti: 3000 koneksi RST,
server tetap melayani.

Pengawas port adalah jaring kedua untuk mode gagal lain yang belum ketahuan:
selama server sudah `started`, :8000 diprobe tiap `jeda_pengawas` detik, dan
`gagal_maks` kegagalan beruntun membuat pengawas mengambil alih — uvicorn
diminta berhenti rapi, lalu supervisor di proses yang sama menyalakan ulang
(maksimal `ulang_maks` kali). Sengaja di dalam satu proses: task `IDX-API`
hanya menyala sekali sehari 08:45 dan tidak bisa diedit tanpa elevasi, jadi
mengandalkan task berarti API mati sampai besok pagi.

Jalankan dari `start-api.bat`:
    .venv\\Scripts\\python.exe -m idx_scraper.api.serve --port 8000
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from collections.abc import Callable

import uvicorn

APP_DEFAULT = "idx_scraper.api.app:app"


def loop_factory() -> asyncio.AbstractEventLoop:
    """Loop untuk uvicorn: Selector di Windows, karena proactor mematikan pendengar."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def port_hidup(host: str, port: int, timeout: float = 1.0) -> bool:
    """True bila ada yang menerima koneksi TCP di `host:port`."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _minta_ulang(server: uvicorn.Server, tunggu: float = 10.0) -> None:
    """Minta uvicorn berhenti rapi supaya supervisor menyalakan ulang.

    Kalau `serve()` tidak juga kembali dalam `tunggu` detik (mis. macet menunggu
    koneksi yang tidak pernah selesai), proses keluar paksa dengan kode 3 -
    lebih baik task mencatat kegagalan daripada zombie tanpa port.
    """
    server.should_exit = True

    def paksa() -> None:
        time.sleep(tunggu)
        print(
            f"[pengawas] uvicorn belum berhenti {tunggu:.0f} dtk setelah diminta - keluar (3)",
            flush=True,
        )
        os._exit(3)

    threading.Thread(target=paksa, daemon=True).start()


def _pengawas(
    server: uvicorn.Server,
    host: str,
    port: int,
    jeda: float,
    gagal_maks: int,
    cek: Callable[..., bool] = port_hidup,
    ambil_alih: Callable[[], None] | None = None,
) -> None:
    """Awasi port; ambil alih kalau server hidup tapi berhenti menjawab."""
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.2)

    def bawaan_ambil_alih() -> None:
        _minta_ulang(server)

    if ambil_alih is None:
        ambil_alih = bawaan_ambil_alih

    gagal = 0
    while not server.should_exit:
        time.sleep(jeda)
        if cek(host, port):
            gagal = 0
            continue
        gagal += 1
        print(f"[pengawas] :{port} tidak menjawab ({gagal}/{gagal_maks})", flush=True)
        if gagal >= gagal_maks:
            print(f"[pengawas] :{port} gagal {gagal}x beruntun - menyalakan ulang", flush=True)
            ambil_alih()
            return


def jalankan(
    app: object = APP_DEFAULT,
    host: str = "127.0.0.1",
    port: int = 8000,
    jeda_pengawas: float = 10.0,
    gagal_maks: int = 3,
    ulang_maks: int = 3,
    tidur_ulang: float = 2.0,
) -> None:
    """Jalankan API sampai berhenti; nyalakan ulang kalau pengawas memintanya."""
    jenis_loop = "Selector" if sys.platform == "win32" else "policy"
    print(
        f"[api] {host}:{port} loop={jenis_loop} pengawas={jeda_pengawas:g}s/{gagal_maks}",
        flush=True,
    )
    for percobaan in range(ulang_maks + 1):
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
        diminta = threading.Event()

        def ambil_alih(server: uvicorn.Server = server, diminta: threading.Event = diminta) -> None:
            diminta.set()
            _minta_ulang(server)

        threading.Thread(
            target=_pengawas,
            args=(server, host, port, jeda_pengawas, gagal_maks),
            kwargs={"ambil_alih": ambil_alih},
            daemon=True,
        ).start()
        asyncio.run(server.serve(), loop_factory=loop_factory)

        if not diminta.is_set():
            return
        if percobaan == ulang_maks:
            print(f"[pengawas] sudah {ulang_maks}x dinyalakan ulang - menyerah", flush=True)
            sys.exit(4)
        print("[pengawas] menyalakan ulang uvicorn", flush=True)
        if tidur_ulang:
            time.sleep(tidur_ulang)


def main() -> None:
    parser = argparse.ArgumentParser(description="API IDX :8000 (loop Selector + pengawas port)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    jalankan(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
