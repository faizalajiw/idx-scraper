"""Tests for the .env-backed watchlist store."""

from __future__ import annotations

from pathlib import Path

import pytest

from idx_scraper.watchlist_store import read_watchlist, update_watchlist, write_watchlist


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "IDX_STORAGE=sqlite\n"
        "IDX_WATCHLIST=BBCA,BBRI,TLKM\n"
        "IDX_MARKET_OPEN=09:00\n",
        encoding="utf-8",
    )
    return p


def test_read_parses_codes(env_file: Path) -> None:
    assert read_watchlist(env_file) == ["BBCA", "BBRI", "TLKM"]


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_watchlist(tmp_path / "nope.env") == []


def test_write_preserves_other_lines(env_file: Path) -> None:
    out = write_watchlist(["bbcA", "BNGA", "bbca"], env_file)
    assert out == ["BBCA", "BNGA"]  # dedup + uppercase, order preserved
    text = env_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# comment"
    assert "IDX_STORAGE=sqlite" in lines
    assert "IDX_MARKET_OPEN=09:00" in lines
    assert "IDX_WATCHLIST=BBCA,BNGA" in lines


def test_write_appends_when_key_absent(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("IDX_STORAGE=sqlite\n", encoding="utf-8")
    write_watchlist(["BBCA"], p)
    assert p.read_text(encoding="utf-8").splitlines()[-1] == "IDX_WATCHLIST=BBCA"


def test_write_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_watchlist(["BBCA"], tmp_path / "nope.env")


def test_update_add_and_remove(env_file: Path) -> None:
    out = update_watchlist(add=["bnga", "BBCA"], remove=["tlkm"], path=env_file)
    assert out == ["BBCA", "BBRI", "BNGA"]
    assert read_watchlist(env_file) == out


def test_crlf_env_is_handled(tmp_path: Path) -> None:
    """read_text with splitlines must handle CRLF files (Windows)."""
    p = tmp_path / ".env"
    p.write_bytes(b"IDX_WATCHLIST=BBCA,BBRI\r\nIDX_STORAGE=sqlite\r\n")
    assert read_watchlist(p) == ["BBCA", "BBRI"]
    write_watchlist(["TLKM"], p)
    assert read_watchlist(p) == ["TLKM"]
    assert "IDX_STORAGE=sqlite" in p.read_text(encoding="utf-8")
