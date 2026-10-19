"""Unit tests for perintah `idx journal` + tiga job sorenya.

Tidak menyentuh DB atau Telegram.

Yang dikunci di sini adalah bagian yang membuat job terjadwal bisa dipercaya:
sesi yang ditulis adalah **sesi bursa terakhir** yang datanya ada (jam 00:10
dini hari bukan hari bursa baru), jalur tulisnya selalu lewat gerbang yang sama
dengan layar, perintah halt menolak sebab kosong, dan pesan Telegram menyebut
kode serta statusnya dengan angka apa adanya.
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


def test_tulis_sesi_selalu_lewat_gerbang(monkeypatch) -> None:
    """Jalur tulis memakai gerbang yang sama dengan layar: tanpa gerbang, tidak menulis.

    Yang dikunci: `_journal_write` merakit gerbang dari sumber nyata dan
    menyertakannya ke `write_session`, jadi tidak ada jalur tulis yang bisa
    menyelinap lewat tanpa diperiksa.
    """
    from idx_scraper.research import journal_store
    from idx_scraper.research.journal import claim_gate

    tercatat: dict = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            return None

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cur()

    def _tulis(dsn, cur, *, trade_date, gate=None, konteks=None):
        tercatat.update({"trade_date": trade_date, "gate": gate, "konteks": konteks})
        return {"written": 0, "refused": 1, "reason": "data"}

    monkeypatch.setattr(cli, "_journal_dsn", lambda: "dsn")
    monkeypatch.setattr(cli, "_journal_session", lambda dsn, trade_date=None: "2026-10-16")
    monkeypatch.setattr(
        cli,
        "_journal_gate_parts",
        lambda dsn: (
            claim_gate(freshness_status="basi"),
            {"freshness_status": "basi", "regime": None, "vol_state": None},
        ),
    )
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(journal_store, "write_session", _tulis)

    hasil = cli._journal_write("dsn", cetak=False)

    assert tercatat["trade_date"] == "2026-10-16"
    assert tercatat["gate"]["reason"] == "data"
    assert tercatat["konteks"]["freshness_status"] == "basi"
    assert hasil == {"session": "2026-10-16", "written": 0, "refused": 1, "reason": "data"}


def test_perintah_halt_tanpa_sebab_keluar_dengan_pesan(capsys) -> None:
    args = argparse.Namespace(reason="   ", by="pemilik")
    cli.cmd_journal_halt(args)
    assert "sebab" in capsys.readouterr().err.lower()


def test_pesan_hits_menyebut_kode_dan_statusnya() -> None:
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


class _CurPengingat:
    """Cursor palsu untuk job pengingat: hasil berurutan, SQL dicatat."""

    def __init__(self, hasil: list) -> None:
        self.hasil = list(hasil)
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None) -> None:
        self.sql.append(" ".join(str(sql).split()))

    def fetchall(self) -> list:
        return self.hasil.pop(0) if self.hasil else []

    def fetchone(self):
        baris = self.hasil.pop(0) if self.hasil else []
        return baris[0] if baris else None


class _ConnPengingat:
    def __init__(self, cur) -> None:
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cur


def _pasang_pengingat(monkeypatch, cur, *, enabled: bool = True) -> list[str]:
    """Telegram palsu + psycopg palsu; memulangkan daftar pesan yang terkirim."""
    terkirim: list[str] = []

    class _Notifier:
        def __init__(self) -> None:
            self.enabled = enabled

        def send_message(self, teks: str) -> bool:
            terkirim.append(teks)
            return True

    monkeypatch.setattr(cli, "TelegramNotifier", _Notifier)
    monkeypatch.setattr(cli, "_journal_dsn", lambda: "dsn")
    monkeypatch.setattr(cli, "_journal_session", lambda dsn=None, trade_date=None: "2026-10-16")
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _ConnPengingat(cur))
    return terkirim


def test_pengingat_mengirim_level_yang_tersentuh(monkeypatch) -> None:
    """Ada yang kena target/stop: pesannya menyebut kode dan statusnya, apa adanya."""
    cur = _CurPengingat([[{"code": "PEGE", "status": "kena_target", "outcome_abn": 0.034}]])
    terkirim = _pasang_pengingat(monkeypatch, cur)

    assert cli._job_journal_reminder() == 1
    assert len(terkirim) == 1
    assert "PEGE" in terkirim[0]
    assert "kena_target" in terkirim[0]
    assert "kena_target" in cur.sql[0]


def test_pengingat_tenang_kalau_sesi_punya_catatan(monkeypatch) -> None:
    """Sesi yang menulis klaim tapi tidak ada yang tersentuh: bukan sesi bisu."""
    cur = _CurPengingat([[], [{"klaim": 2, "penolakan": 0}]])
    terkirim = _pasang_pengingat(monkeypatch, cur)

    assert cli._job_journal_reminder() == 0
    assert "tidak ada level yang tersentuh" in terkirim[0]
    assert "claim_refusal" in cur.sql[1]


def test_pengingat_menuduh_bisu_kalau_nol_nol(monkeypatch) -> None:
    """Tanpa klaim DAN tanpa penolakan: ada job yang gagal tanpa bersuara."""
    cur = _CurPengingat([[], [{"klaim": 0, "penolakan": 0}]])
    terkirim = _pasang_pengingat(monkeypatch, cur)

    assert cli._job_journal_reminder() == 0
    assert "2026-10-16" in terkirim[0]
    assert "melahirkan klaim" in terkirim[0]


def test_pengingat_dilewati_kalau_telegram_mati(monkeypatch) -> None:
    """Notifier yang tidak aktif tidak pernah menyentuh DB."""
    cur = _CurPengingat([])
    _pasang_pengingat(monkeypatch, cur, enabled=False)

    assert cli._job_journal_reminder() == 0
    assert cur.sql == []
