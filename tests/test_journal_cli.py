"""Unit tests for perintah `idx journal` + tiga job sorenya.

Tidak menyentuh DB atau Telegram.

Yang dikunci di sini adalah bagian yang membuat job terjadwal bisa dipercaya:
sesi yang ditulis adalah **sesi bursa terakhir** yang datanya ada (jam 00:10
dini hari bukan hari bursa baru), perintah halt menolak sebab kosong, dan pesan
Telegram menyebut kode serta arahnya dengan angka apa adanya.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from idx_scraper import cli, notify
from idx_scraper.health import WIB


def test_sesi_terakhir_dipakai_saat_lewat_tengah_malam(monkeypatch) -> None:
    """Job jalan 00:10 WIB: sesinya tetap sesi bursa terakhir, bukan tanggal itu."""
    monkeypatch.setattr(cli, "_now_wib", lambda: datetime(2026, 10, 20, 0, 10, tzinfo=WIB))
    monkeypatch.setattr(cli, "_last_session_date", lambda dsn: "2026-10-19")
    assert cli._journal_session(dsn="x") == "2026-10-19"


def test_trade_date_menimpa_sesi_terakhir(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_last_session_date", lambda dsn: "2026-10-19")
    assert cli._journal_session(dsn="x", trade_date="2026-10-16") == "2026-10-16"


def test_perintah_halt_tanpa_sebab_keluar_dengan_pesan(capsys) -> None:
    args = argparse.Namespace(reason="   ", by="pemilik")
    cli.cmd_journal_halt(args)
    assert "sebab" in capsys.readouterr().err.lower()


def test_pesan_hits_menyebut_kode_dan_arahnya() -> None:
    pesan = notify.format_claim_hits(
        [
            {"code": "PEGE", "status": "kena_target", "outcome_abn": 0.034},
            {"code": "CAKK", "status": "kena_stop", "outcome_abn": -0.021},
        ]
    )
    assert "PEGE" in pesan
    assert "kena_target" in pesan
    assert "CAKK" in pesan


def test_pesan_hits_kosong_mengatakan_tidak_ada() -> None:
    assert "tidak ada" in notify.format_claim_hits([]).lower()


def test_pesan_sesi_bisu_menyebut_tanggal_dan_keanehannya() -> None:
    pesan = notify.format_claim_silence("2026-10-16")
    assert "2026-10-16" in pesan
    assert "tidak" in pesan.lower()
