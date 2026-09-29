"""Unit tests: pembanding skor aktivitas broker antar emiten satu sektor.

``analytics._sector_peers`` menggabungkan snapshot skor (pandas) dengan peta
sektor; satu-satunya bagian yang butuh DB adalah pengambilan nama emiten, dan
itu di-monkeypatch di sini supaya logika peringkat/median bisa diuji murni.

Kode uji diambil dari SECTOR_MAP kurasi (BBCA/BBRI/BMRI/BBNI = Perbankan) supaya
hasilnya tidak bergantung pada file sector_lookup.json hasil generate.
"""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.api import analytics


@pytest.fixture
def no_names(monkeypatch):
    """Nama emiten datang dari DB; untuk unit test cukup kosong."""
    monkeypatch.setattr(analytics, "_broker_activity_meta", lambda codes, as_of: {})


def _scores(pairs: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [c for c, _ in pairs],
            "score": [s for _, s in pairs],
            "coverage": [1.0] * len(pairs),
        }
    )


def _peers(code: str, pairs: list[tuple[str, float]], **kw):
    return analytics._sector_peers(code, _scores(pairs), as_of=None, **kw)


def test_peers_are_limited_to_the_same_sector(no_names):
    out = _peers("BBRI", [("BBCA", 70.0), ("BBRI", 90.0), ("BMRI", 50.0), ("TLKM", 99.0)])
    assert out is not None
    assert out["name"] == "Perbankan"
    assert out["comparable"] is True
    # TLKM (Telkom & Internet) tidak ikut walau skornya tertinggi.
    assert out["peer_count"] == 3
    assert [p["code"] for p in out["peers"]] == ["BBRI", "BBCA", "BMRI"]
    assert [p["is_self"] for p in out["peers"]] == [True, False, False]
    assert out["my_rank"] == 1
    assert out["median_score"] == 70.0
    assert out["peers"][0]["name"] is None  # nama kosong -> None, bukan error


def test_peer_rank_counts_from_the_top(no_names):
    out = _peers("BMRI", [("BBCA", 70.0), ("BBRI", 90.0), ("BMRI", 50.0)])
    assert out["my_rank"] == 3
    assert out["peers"][-1]["is_self"] is True
    assert out["peers"][-1]["score"] == 50.0


def test_peers_only_include_names_that_already_have_a_score(no_names):
    # BMRI satu sektor tapi belum punya skor: TIDAK dianggap 0, cukup absen.
    out = _peers("BBRI", [("BBCA", 70.0), ("BBRI", 90.0)])
    assert out["peer_count"] == 2
    assert {p["code"] for p in out["peers"]} == {"BBCA", "BBRI"}


def test_display_cap_does_not_change_reported_rank(no_names):
    out = _peers(
        "BMRI",
        [("BBCA", 90.0), ("BBRI", 80.0), ("BMRI", 70.0), ("BBNI", 60.0)],
        limit=2,
    )
    assert out["peer_count"] == 4
    assert out["my_rank"] == 3  # peringkat sebenarnya, bukan posisi di daftar tampil
    assert len(out["peers"]) == 2
    assert [p["code"] for p in out["peers"]] == ["BBCA", "BBRI"]


def test_median_score_uses_all_sector_members(no_names):
    out = _peers(
        "BBCA",
        [("BBCA", 10.0), ("BBRI", 20.0), ("BMRI", 30.0), ("BBNI", 100.0)],
    )
    # median dari [100, 30, 20, 10] -> (20+30)/2
    assert out["median_score"] == 25.0
    assert out["my_rank"] == 4


def test_fallback_sector_is_flagged_not_comparable(no_names):
    # "Lainnya" bukan sektor sebenarnya -> pembandingnya tidak boleh diklaim.
    out = _peers("ZZZZ", [("ZZZZ", 80.0), ("BBCA", 70.0)])
    assert out is not None
    assert out["name"] == "Lainnya"
    assert out["comparable"] is False
    assert out["peers"] == []
    assert out["peer_count"] == 0
    assert out["my_rank"] is None
    assert out["median_score"] is None


def test_no_peers_without_a_score(no_names):
    # Emiten tanpa skor: tidak ada perbandingan yang bisa dibuat.
    assert _peers("BBRI", [("BBCA", 70.0)]) is None
    assert analytics._sector_peers("BBRI", pd.DataFrame(), as_of=None) is None
    # Snapshot tanpa kolom code (defensif) juga mengembalikan None.
    assert analytics._sector_peers("BBRI", pd.DataFrame({"score": [1.0]}), as_of=None) is None


def test_single_member_sector_is_still_reported(no_names):
    # Sektor dengan satu emiten berskor: median = skornya sendiri, peringkat 1.
    out = _peers("BBCA", [("BBCA", 42.0), ("TLKM", 90.0)])
    assert out["peer_count"] == 1
    assert out["my_rank"] == 1
    assert out["median_score"] == 42.0
