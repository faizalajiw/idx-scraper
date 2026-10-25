"""Unit test: penjaga satu instance `serve`.

Dua `serve` sekaligus berarti dua Chrome berebut profil yang sama — browser
saling menendang dan EOD masuk 0 baris. Kunci harus menolak instance kedua,
lalu lepas sendiri begitu proses pemegangnya mati (tanpa kunci basi).
"""

from __future__ import annotations

import os
import subprocess
import sys

from idx_scraper import cli
from idx_scraper.cli import serve

HOLDER = (
    "from idx_scraper.cli import _take_single_instance_lock as t\n"
    "import sys, time\n"
    "sys.stdout.write('ok' if t() else 'gagal')\n"
    "sys.stdout.flush()\n"
    "time.sleep(3)\n"
)


def test_instance_kedua_ditolak_lalu_kunci_lepas_di_akhir(tmp_path, monkeypatch):
    monkeypatch.setenv("IDX_SERVE_LOCK", str(tmp_path / "serve.lock"))
    monkeypatch.setattr(serve, "_SERVE_LOCK_FD", None)

    # Instance pertama (proses terpisah, seperti kenyataannya) pegang kunci.
    holder = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.read(2) == "ok"
        # Instance kedua ditolak selama yang pertama masih hidup.
        assert cli._take_single_instance_lock() is False
    finally:
        # Tunggu pemegang keluar normal; kunci dilepas OS saat proses berakhir.
        holder.wait(timeout=30)
        holder.stdout.close()

    # Setelah pemegang mati, OS melepas kunci -> boleh diambil lagi.
    monkeypatch.setattr(serve, "_SERVE_LOCK_FD", None)
    assert cli._take_single_instance_lock() is True

    # Tutup sendiri agar pytest bisa membersihkan direktori temporer (Windows).
    if cli._SERVE_LOCK_FD is not None:
        os.close(cli._SERVE_LOCK_FD)
        cli._SERVE_LOCK_FD = None
