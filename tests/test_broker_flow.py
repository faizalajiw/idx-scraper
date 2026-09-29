"""Unit tests: klasifikasi kategori broker + komposisi nilai transaksi (pure).

Tanpa DB — pola yang sama dengan test_broker_store & test_broker_activity.
"""

from __future__ import annotations

import math

from idx_scraper.broker_flow import (
    CATEGORIES,
    classify_broker,
    composition,
    composition_series,
    top_brokers_by_category,
)

# --------------------------------------------------------------------------- #
# classify_broker
# --------------------------------------------------------------------------- #


def test_foreign_codes_from_real_db_pairs():
    # Pasangan (kode, nama) diverifikasi dari research.broker_daily 2026-09-29.
    assert classify_broker("AK") == "asing"  # UBS Sekuritas Indonesia
    assert classify_broker("YU") == "asing"  # CGS International
    assert classify_broker("BK") == "asing"  # J.P. Morgan
    assert classify_broker("KZ") == "asing"  # CLSA
    assert classify_broker("GI") == "asing"  # Webull


def test_bumn_codes():
    assert classify_broker("CC") == "bumn"  # Mandiri Sekuritas
    assert classify_broker("NI") == "bumn"  # BNI Sekuritas
    assert classify_broker("OD") == "bumn"  # BRI Danareksa
    assert classify_broker("DX") == "bumn"  # Bahana Sekuritas


def test_local_codes():
    assert classify_broker("XL") == "lokal"  # Stockbit Sekuritas Digital
    assert classify_broker("PD") == "lokal"  # Indo Premier
    assert classify_broker("SQ") == "lokal"  # BCA Sekuritas (swasta, bukan BUMN)


def test_code_normalization():
    assert classify_broker(" ak ") == "asing"
    assert classify_broker("cc") == "bumn"
    assert classify_broker(None) == "lokal"
    assert classify_broker("") == "lokal"


def test_unknown_code_defaults_to_local():
    # Kode baru tak dikenal tidak boleh ikut kategori khusus tanpa bukti.
    assert classify_broker("ZZ") == "lokal"


def test_yp_follows_current_owner():
    # Kode bisa ganti pemilik: fixture lama menandai YP = "Valbury", DB kini
    # menunjukkan YP = "Mirae Asset" (asing). Map mengikuti pemilik terverifikasi.
    assert classify_broker("YP") == "asing"


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #


def _row(code: str, value: float, name: str | None = None) -> dict:
    return {"broker_code": code, "broker_name": name, "value": value}


def test_composition_basic_shares():
    rows = [
        _row("AK", 300.0),  # asing
        _row("CC", 500.0),  # bumn
        _row("XL", 200.0),  # lokal
    ]
    comp = composition(rows)
    assert comp["n_brokers"] == 3
    assert comp["total_value"] == 1000.0
    cats = comp["categories"]
    assert list(cats.keys()) == list(CATEGORIES)
    assert cats["asing"]["value"] == 300.0
    assert cats["asing"]["share"] == 0.3
    assert cats["bumn"]["value"] == 500.0
    assert cats["bumn"]["share"] == 0.5
    assert cats["lokal"]["value"] == 200.0
    assert cats["lokal"]["share"] == 0.2
    assert cats["asing"]["n_brokers"] == 1
    assert cats["bumn"]["n_brokers"] == 1
    assert cats["lokal"]["n_brokers"] == 1


def test_composition_empty_rows():
    comp = composition([])
    assert comp["n_brokers"] == 0
    assert comp["total_value"] is None
    for c in CATEGORIES:
        assert comp["categories"][c]["share"] is None
        assert comp["categories"][c]["value"] == 0.0


def test_composition_missing_values_counted_but_zero():
    rows = [{"broker_code": "AK", "value": None}, {"broker_code": "XL", "value": 100.0}]
    comp = composition(rows)
    # Baris dengan value NULL = firma tetap terhitung, nilai 0.
    assert comp["n_brokers"] == 2
    assert comp["total_value"] == 100.0
    assert comp["categories"]["asing"]["value"] == 0.0
    assert comp["categories"]["asing"]["share"] == 0.0


def test_composition_only_null_values_has_no_total():
    # Semua value NULL -> "tidak ada data", bukan total 0.
    rows = [{"broker_code": "AK", "value": None}]
    comp = composition(rows)
    assert comp["total_value"] is None
    assert comp["categories"]["asing"]["share"] is None


def test_composition_skips_rows_without_code():
    rows = [{"broker_code": None, "value": 999.0}, {"value": 1.0}, _row("XL", 10.0)]
    comp = composition(rows)
    assert comp["n_brokers"] == 1
    assert comp["total_value"] == 10.0


def test_composition_treats_nan_as_zero():
    rows = [{"broker_code": "AK", "value": float("nan")}, _row("XL", 50.0)]
    comp = composition(rows)
    assert comp["total_value"] == 50.0
    assert comp["categories"]["asing"]["value"] == 0.0


# --------------------------------------------------------------------------- #
# top_brokers_by_category
# --------------------------------------------------------------------------- #


def test_top_brokers_sorted_and_capped():
    rows = [
        _row("XL", 100.0, "Stockbit"),
        _row("PD", 300.0, "Indo Premier"),
        _row("SQ", 200.0, "BCA Sekuritas"),
        _row("AK", 900.0, "UBS"),
        _row("CC", 50.0, "Mandiri"),
    ]
    tops = top_brokers_by_category(rows, top_n=2)
    lokal = tops["lokal"]
    assert [b["broker_code"] for b in lokal] == ["PD", "SQ"]
    assert tops["asing"][0]["broker_code"] == "AK"
    assert tops["bumn"][0]["broker_code"] == "CC"


def test_top_brokers_share_is_of_whole_market():
    rows = [_row("AK", 750.0), _row("XL", 250.0)]
    tops = top_brokers_by_category(rows, top_n=5)
    # Share AK dihitung terhadap total pasar (1000), bukan kategorinya.
    assert tops["asing"][0]["share"] == 0.75
    assert tops["lokal"][0]["share"] == 0.25


def test_top_brokers_without_code_rows():
    tops = top_brokers_by_category([{"broker_code": None, "value": 5.0}], top_n=3)
    assert tops == {c: [] for c in CATEGORIES}


# --------------------------------------------------------------------------- #
# composition_series
# --------------------------------------------------------------------------- #


def test_series_sorted_and_flat_shape():
    by_date = {
        "2026-09-29": [_row("AK", 300.0), _row("XL", 100.0)],
        "2026-09-26": [_row("AK", 100.0), _row("CC", 300.0)],
    }
    series = composition_series(by_date)
    assert [p["date"] for p in series] == ["2026-09-26", "2026-09-29"]
    d0, d1 = series
    assert d0["asing_value"] == 100.0
    assert d0["bumn_value"] == 300.0
    assert d0["asing_share"] == 0.25
    assert d1["asing_share"] == 0.75
    assert math.isclose(d1["total_value"], 400.0)


def test_series_empty():
    assert composition_series({}) == []
