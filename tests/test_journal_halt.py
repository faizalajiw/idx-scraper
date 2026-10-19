"""Unit tests for idx_scraper.journal_halt (halt manual penulisan klaim).

Tidak menyentuh DB atau jaringan; berkasnya di `tmp_path`.

Yang dikunci di sini adalah sifat yang membuat halt bisa dipercaya: sebab wajib
tertulis (halt tanpa sebab tidak bisa dibedakan dari mesin rusak), pembacaan
tidak pernah melempar (berkas cacat berarti "tidak dihentikan" dengan catatan,
bukan scheduler mati), dan jejak siapa-kapan tetap ada sesudah halt dibuka.
"""

from __future__ import annotations

import pytest

from idx_scraper.journal_halt import HALT_PATH_ENV, clear, set_halt, state


def test_halt_tanpa_sebab_ditolak(tmp_path) -> None:
    with pytest.raises(ValueError):
        set_halt(reason="   ", path=tmp_path / "halt.json")


def test_halt_menyala_terbaca_lengkap(tmp_path) -> None:
    p = tmp_path / "halt.json"
    set_halt(reason="rule saya dilanggar dua kali", by="pemilik", path=p)
    st = state(path=p)
    assert st["halted"] is True
    assert st["reason"] == "rule saya dilanggar dua kali"
    assert st["by"] == "pemilik"
    assert st["at"]


def test_halt_mati_menyisakan_jejak_di_berkas(tmp_path) -> None:
    p = tmp_path / "halt.json"
    set_halt(reason="uji", path=p)
    clear(by="pemilik", path=p)
    st = state(path=p)
    assert st["halted"] is False
    assert st["reason"] is None
    assert st["by"] == "pemilik"
    assert p.exists()


def test_berkas_belum_ada_berarti_tidak_dihentikan(tmp_path) -> None:
    st = state(path=tmp_path / "tidak-ada.json")
    assert st == {"halted": False, "reason": None, "at": None, "by": None, "note": None}


def test_berkas_halt_rusak_dianggap_tidak_dihentikan(tmp_path) -> None:
    """Fail-open, tapi sebabnya terlihat: mesin tidak boleh berhenti karena berkas cacat."""
    p = tmp_path / "halt.json"
    p.write_text("{bukan json", encoding="utf-8")
    st = state(path=p)
    assert st["halted"] is False
    assert st["note"] and "rusak" in st["note"]


def test_path_env_dipakai_kalau_argumen_kosong(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "dari-env.json"))
    set_halt(reason="dari env")
    assert state()["halted"] is True
    assert state()["reason"] == "dari env"


def test_berkas_bukan_utf8_tetap_fail_open(tmp_path) -> None:
    """Berkas yang tersentuh perkakas lain tidak boleh mematikan gerbang dari belakang.

    ``state()`` dipanggil job penulis klaim tiap jam dan rute keadaan gerbang: kalau
    ia melempar, job berhenti tanpa satu pun baris penolakan tercatat dan layar
    kehilangan blok gerbangnya, justru saat ada yang rusak.
    """
    p = tmp_path / "halt.json"
    p.write_bytes(b'{"halted": true, "reason": "uji", "by": "\xff\xfe pemilik"}')
    st = state(path=p)
    assert st["halted"] is False
    assert st["note"] and "rusak" in st["note"]
