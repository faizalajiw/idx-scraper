"""Unit tests: kepemilikan emiten & aksi pemilik (pure, tanpa DB)."""

from __future__ import annotations

from idx_scraper.ownership import (
    classify,
    controller_names,
    free_float_pct,
    owner_changes,
    parse_shareholders,
)


def _h(name, category, pct, shares=None, ctrl=False):
    return {
        "holder_name": name,
        "category": category,
        "pct": pct,
        "shares": shares,
        "is_controller": ctrl,
    }


# ------------------------------------------------------------------ parse


def test_parse_shareholders_from_payload():
    profile = {
        "PemegangSaham": [
            {"Nama": "PT Contoh", "Kategori": "Lebih dari 5%", "Jumlah": 1000.0,
             "Persentase": 60.0, "Pengendali": True},
            {"Nama": "Masyarakat Non Warkat", "Kategori": "Masyarakat Non Warkat",
             "Jumlah": 400.0, "Persentase": 40.0, "Pengendali": False},
            {"Nama": None, "Kategori": "x", "Jumlah": 0, "Persentase": 0},  # tanpa nama
            "bukan-dict",
        ]
    }
    rows = parse_shareholders(profile)
    assert [r["holder_name"] for r in rows] == ["PT Contoh", "Masyarakat Non Warkat"]
    assert rows[0]["is_controller"] is True
    assert rows[0]["pct"] == 60.0


def test_parse_shareholders_sorts_by_pct_desc():
    profile = {"PemegangSaham": [
        {"Nama": "Kecil", "Kategori": "Komisaris", "Jumlah": 1, "Persentase": 0.1},
        {"Nama": "Besar", "Kategori": "Lebih dari 5%", "Jumlah": 100, "Persentase": 70.0},
    ]}
    rows = parse_shareholders(profile)
    assert rows[0]["holder_name"] == "Besar"


def test_parse_shareholders_bad_input_is_safe():
    assert parse_shareholders(None) == []
    assert parse_shareholders({}) == []
    assert parse_shareholders({"PemegangSaham": "nope"}) == []


# ------------------------------------------------------------------ classify


def test_classify_categories():
    assert classify("Masyarakat Warkat") == "publik"
    assert classify("Masyarakat Non Warkat") == "publik"
    assert classify("Saham Treasury") == "treasury"
    assert classify("Direksi") == "manajemen"
    assert classify("Komisaris") == "manajemen"
    assert classify("Lebih dari 5%") == "besar"
    assert classify(None) == "lain"


# ------------------------------------------------------------------ free float


def test_free_float_sums_public_categories():
    holders = [
        _h("Pengendali", "Lebih dari 5%", 60.0),
        _h("Masyarakat Warkat", "Masyarakat Warkat", 8.0),
        _h("Masyarakat Non Warkat", "Masyarakat Non Warkat", 30.0),
        _h("Direksi", "Direksi", 0.5),
    ]
    assert free_float_pct(holders) == 38.0


def test_free_float_none_when_no_public_rows():
    assert free_float_pct([_h("Pengendali", "Lebih dari 5%", 90.0)]) is None
    assert free_float_pct([]) is None


def test_controller_names():
    holders = [
        _h("PT A", "Lebih dari 5%", 60.0, ctrl=True),
        _h("PT B", "Lebih dari 5%", 7.0, ctrl=False),
    ]
    assert controller_names(holders) == ["PT A"]


# ------------------------------------------------------------------ owner changes


def test_owner_changes_add_and_reduce():
    prev = [_h("PT A", "Lebih dari 5%", 60.0), _h("PT B", "Lebih dari 5%", 10.0)]
    curr = [_h("PT A", "Lebih dari 5%", 62.5), _h("PT B", "Lebih dari 5%", 8.0)]
    ch = owner_changes(prev, curr)
    by = {c["holder_name"]: c for c in ch}
    assert by["PT A"]["action"] == "tambah"
    assert by["PT A"]["delta_pct"] == 2.5
    assert by["PT B"]["action"] == "kurang"
    assert by["PT B"]["delta_pct"] == -2.0


def test_owner_changes_new_and_exit():
    prev = [_h("PT Lama", "Lebih dari 5%", 12.0)]
    curr = [_h("PT Baru", "Lebih dari 5%", 6.0)]
    ch = owner_changes(prev, curr)
    by = {c["holder_name"]: c for c in ch}
    assert by["PT Baru"]["action"] == "baru"
    assert by["PT Lama"]["action"] == "keluar"


def test_owner_changes_ignores_small_deltas():
    prev = [_h("PT A", "Lebih dari 5%", 60.0)]
    curr = [_h("PT A", "Lebih dari 5%", 60.02)]  # < 0.05 -> diabaikan
    assert owner_changes(prev, curr) == []


def test_owner_changes_excludes_public_and_treasury():
    # Bergesernya agregat masyarakat / treasury bukan "aksi pemilik" tertentu.
    prev = [_h("Masyarakat Non Warkat", "Masyarakat Non Warkat", 40.0),
            _h("Saham Treasury", "Saham Treasury", 5.0)]
    curr = [_h("Masyarakat Non Warkat", "Masyarakat Non Warkat", 45.0),
            _h("Saham Treasury", "Saham Treasury", 1.0)]
    assert owner_changes(prev, curr) == []


def test_owner_changes_empty_prev_marks_new():
    # Snapshot pertama (prev kosong) -> semua pemilik bernama jadi "baru".
    ch = owner_changes([], [_h("PT A", "Lebih dari 5%", 60.0)])
    assert len(ch) == 1 and ch[0]["action"] == "baru"
