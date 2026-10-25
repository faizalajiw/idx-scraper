"""Kontrak pintu koneksi DB: psycopg.connect dan DATABASE_URL hanya di db.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from idx_scraper import db

AKAR = Path(__file__).resolve().parents[1]
BERKAS_PY = (*AKAR.joinpath("src").rglob("*.py"), *AKAR.joinpath("scripts").glob("*.py"))
POLA_ENV = (
    'os.getenv("DATABASE_URL")',
    'os.environ["DATABASE_URL"]',
    'os.environ.get("DATABASE_URL")',
)


def test_dsn_dari_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/idx")
    assert db.dsn() == "postgresql://u:p@localhost:5432/idx"


def test_dsn_argumen_menang_atas_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://env@x/db")
    assert db.dsn("postgresql://arg@host/db") == "postgresql://arg@host/db"


def test_dsn_kosong_melempar(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(db.DatabaseUrlMissing, match="DATABASE_URL belum diisi"):
        db.dsn()


def test_tersedia_mengikuti_env(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert db.tersedia() is False
    monkeypatch.setenv("DATABASE_URL", "  ")
    assert db.tersedia() is False
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert db.tersedia() is True


def _tangkap(monkeypatch) -> dict:
    tertangkap: dict = {}

    def connect_palsu(dsn, **kw):
        tertangkap.clear()
        tertangkap.update(kw, dsn=dsn)
        return "CONN"

    monkeypatch.setattr(db.psycopg, "connect", connect_palsu)
    return tertangkap


def test_connect_setelan_bawaan(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:rahasia@localhost:5432/x")
    tertangkap = _tangkap(monkeypatch)
    assert db.connect() == "CONN"
    assert tertangkap["autocommit"] is True
    assert tertangkap["connect_timeout"] == db.DEFAULT_TIMEOUT
    assert tertangkap["application_name"]
    assert "row_factory" not in tertangkap


def test_connect_dict_rows_dan_transaksional(monkeypatch) -> None:
    tertangkap = _tangkap(monkeypatch)
    db.connect(dict_rows=True, autocommit=False, timeout=3, url="postgresql://eksplisit")
    assert tertangkap["dsn"] == "postgresql://eksplisit"
    assert tertangkap["autocommit"] is False
    assert tertangkap["connect_timeout"] == 3
    assert tertangkap["row_factory"] is db.dict_row


def test_psycopg_connect_hanya_di_db_py() -> None:
    """Satu pintu: pemanggilan ``psycopg.connect(`` tidak boleh muncul di tempat lain."""
    berkas = (*AKAR.joinpath("src").rglob("*.py"), *AKAR.joinpath("scripts").glob("*.py"))
    bermasalah = [
        p.relative_to(AKAR).as_posix()
        for p in berkas
        if "psycopg.connect(" in p.read_text(encoding="utf-8")
    ]
    assert bermasalah == ["src/idx_scraper/db.py"], bermasalah


def test_database_url_env_hanya_terbaca_di_satu_modul() -> None:
    """Pembacaan env ``DATABASE_URL`` hanya di db.py.

    Satu pengecualian: ``scripts/backfill_raw_eod.py`` menyalin isi ``.env`` ke
    ``os.environ`` (bootstrap berkas, bukan pembacaan untuk koneksi).
    """
    bermasalah: list[str] = []
    berkas = (*AKAR.joinpath("src").rglob("*.py"), *AKAR.joinpath("scripts").glob("*.py"))
    for p in berkas:
        if p.name == "db.py":
            continue
        for nomor, baris in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if baris.lstrip().startswith("#"):
                continue
            if any(pola in baris for pola in POLA_ENV):
                bermasalah.append(f"{p.relative_to(AKAR).as_posix()}:{nomor}")
    tepat = [b for b in bermasalah if b.startswith("scripts/backfill_raw_eod.py")]
    assert bermasalah == [tepat[0]], bermasalah
