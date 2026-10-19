"""Tests lapisan API buku klaim (lapis 4). Tidak menyentuh DB.

Yang dikunci di sini:

- keadaan gerbang: halt manual jadi sebab pertama, dan DB yang tidak terbaca
  TIDAK memblokir mesin (fail-open dengan catatan, bukan 500);
- `halt` tanpa sebab ditolak sebelum menyentuh berkas (rute mengubahnya jadi 422);
- daftar klaim tanpa tanggal memakai sesi bursa terakhir yang ada di raw_eod;
- ringkasan memakai aturan papan secara bawaan dan memulangkan kalimat rekaman.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from idx_scraper.api import journal as journal_api
from idx_scraper.journal_halt import HALT_PATH_ENV, set_halt


class _Cur:
    """Cursor palsu: merekam SQL + params, memulangkan set baris berurutan."""

    def __init__(self, results: list[list[dict]] | None = None, *, boom: bool = False):
        self.results: list[list[dict]] = list(results or [])
        self.boom = boom
        self.sql: list[str] = []
        self.params: list[Any] = []

    def execute(self, sql, params=None) -> None:
        if self.boom:
            raise RuntimeError("DB mati")
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(params)

    def _rows(self) -> list[dict]:
        return self.results.pop(0) if self.results else []

    def fetchone(self):
        rows = self._rows()
        return rows[0] if rows else None

    def fetchall(self) -> list[dict]:
        return list(self._rows())


def pasang(monkeypatch, results=None, *, boom: bool = False) -> _Cur:
    """Cursor palsu + kesegaran segar + gerbang rezim terbuka."""
    cur = _Cur(results, boom=boom)

    @contextmanager
    def _cm():
        yield cur

    monkeypatch.setattr(journal_api, "get_cursor", lambda: _cm())
    monkeypatch.setattr(journal_api, "get_freshness", lambda: {"status": "segar"})
    monkeypatch.setattr(journal_api, "_regime", lambda: (False, None))
    return cur


def _klaim(trade_date: str = "2026-10-16") -> dict:
    return {
        "code": "PEGE",
        "trade_date": trade_date,
        "rule": "papan-teratas",
        "status": "kedaluwarsa",
        "outcome_abn": -0.02,
        "horizon_days": 21,
    }


def test_state_halt_manual_jadi_sebab_pertama(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    set_halt(reason="uji pemilik", path=tmp_path / "halt.json")
    pasang(monkeypatch)
    st = journal_api.state()
    assert st["allowed"] is False
    assert st["reason"] == "manual"
    assert "uji pemilik" in st["message"]
    assert st["halt"]["halted"] is True
    assert st["halt"]["by"] == "pemilik"


def test_state_db_mati_tetap_terbuka_dengan_catatan(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    pasang(monkeypatch, boom=True)

    def _kesegaran_gagal() -> dict:
        raise RuntimeError("pool mati")

    monkeypatch.setattr(journal_api, "get_freshness", _kesegaran_gagal)
    st = journal_api.state()
    assert st["allowed"] is True
    assert st["reason"] is None
    assert st["note"] is not None
    assert "buku klaim belum terbaca" in st["note"]


def test_state_data_basi_menahan(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    pasang(monkeypatch)
    monkeypatch.setattr(journal_api, "get_freshness", lambda: {"status": "basi"})
    st = journal_api.state()
    assert st["allowed"] is False
    assert st["reason"] == "data"


def test_state_verdict_tidak_terbukti_menahan(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    kalah = [_klaim(f"2026-10-{i:02d}") for i in range(1, 29)] + [_klaim(), _klaim()]
    pasang(monkeypatch, [kalah])
    st = journal_api.state()
    assert st["reason"] == "verdict"


def test_halt_tanpa_sebab_ditolak_dan_tidak_menulis(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    with pytest.raises(ValueError):
        journal_api.halt(reason="   ", by="pemilik")
    assert not (tmp_path / "halt.json").exists()


def test_resume_membuka_lagi_dan_tetap_tercatat(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    set_halt(reason="uji", path=tmp_path / "halt.json")
    pasang(monkeypatch)
    st = journal_api.resume(by="pemilik")
    assert st["allowed"] is True
    assert st["halt"]["halted"] is False
    assert st["halt"]["at"] is not None


def test_claims_tanpa_tanggal_memakai_sesi_terakhir(monkeypatch) -> None:
    baris = {"code": "PEGE", "trade_date": "2026-10-16", "status": "terbuka"}
    cur = pasang(monkeypatch, [[{"tanggal": "2026-10-16"}], [baris]])
    out = journal_api.claims()
    assert [r["code"] for r in out] == ["PEGE"]
    assert "max(trade_date)" in cur.sql[0]
    assert "research.claim_journal" in cur.sql[1]
    assert cur.params[1][0] == "2026-10-16"
    assert cur.params[1][-1] == 50


def test_claims_tanpa_data_eod_memulangkan_kosong(monkeypatch) -> None:
    cur = pasang(monkeypatch, [[{"tanggal": None}]])
    assert journal_api.claims() == []
    assert len(cur.sql) == 1


def test_claims_membatasi_limit(monkeypatch) -> None:
    cur = pasang(monkeypatch, [[{"tanggal": "2026-10-16"}], []])
    journal_api.claims(limit=10_000)
    assert cur.params[1][-1] == journal_api.CLAIM_LIMIT_MAX


def test_summary_memakai_aturan_papan_bawaan(monkeypatch) -> None:
    cur = pasang(monkeypatch, [[]])
    out = journal_api.summary()
    assert out["claims"] == 0
    assert out["record_line"] == "Aturan ini: belum punya rekaman"
    assert out["verdict"]["status"] == "belum cukup"
    assert "research.claim_journal" in cur.sql[0]


def test_summary_db_mati_fail_open_dengan_catatan(monkeypatch) -> None:
    """Buku yang tidak terbaca bukan 500, dan bukan juga dilaporkan sebagai buku kosong.

    Bentuk balasannya sama, sebabnya di ``note``: layar harus bisa membedakan
    "belum ada klaim" dari "belum terbaca" tanpa membaca log server.
    """
    pasang(monkeypatch, boom=True)
    out = journal_api.summary()
    assert out["claims"] == 0
    assert out["claim_sessions"] == 0
    assert out["record_line"] == "Aturan ini: belum punya rekaman"
    assert out["verdict"]["status"] == "belum cukup"
    assert out["note"] and "belum terbaca" in out["note"]


def test_claims_db_mati_fail_open(monkeypatch) -> None:
    """Daftar rincian memulangkan kosong supaya blok gerbang tetap bisa dirender.

    Rute yang mati karena DB akan mematikan juga bagian gerbang di layar, dan
    gerbang justru bagian yang harus tetap terbaca saat ada yang rusak.
    """
    pasang(monkeypatch, boom=True)
    assert journal_api.claims() == []


def test_rute_halt_tanpa_sebab_menghasilkan_422(monkeypatch, tmp_path) -> None:
    from fastapi import HTTPException

    from idx_scraper.api import app as app_mod

    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    with pytest.raises(HTTPException) as e:
        app_mod.journal_halt(app_mod.JournalHaltRequest(reason="  ", by="pemilik"))
    assert e.value.status_code == 422
    assert "reason" in str(e.value.detail)
