"""Unit tests: parsing broker summary IDX (broker_store) — pure, tanpa DB."""

from __future__ import annotations

from idx_scraper.broker_store import _int, _num, parse_broker_rows


def test_parse_normal_payload():
    payload = {
        "draw": 0,
        "recordsTotal": 2,
        "data": [
            {
                "No": 1,
                "IDFirm": "AD",
                "FirmName": "Sukadana Prima Sekuritas",
                "Volume": 35415900.0,
                "Value": 4351965300.0,
                "Frequency": 461.0,
            },
            {"No": 2, "IDFirm": "YP", "FirmName": "Valbury", "Volume": 100, "Value": 50, "Frequency": 3},
        ],
    }
    rows = parse_broker_rows(payload)
    assert len(rows) == 2
    assert rows[0] == {
        "broker_code": "AD",
        "broker_name": "Sukadana Prima Sekuritas",
        "volume": 35415900,
        "value": 4351965300.0,
        "frequency": 461,
    }


def test_parse_string_numbers_with_thousand_separators():
    payload = {"data": [{"IDFirm": "XX", "Volume": "1,234,567", "Value": "9,876,543.21", "Frequency": "42"}]}
    r = parse_broker_rows(payload)[0]
    assert r["volume"] == 1234567
    assert r["value"] == 9876543.21
    assert r["frequency"] == 42


def test_missing_fields_become_none_not_zero():
    payload = {"data": [{"IDFirm": "ZZ"}]}
    r = parse_broker_rows(payload)[0]
    assert r["broker_code"] == "ZZ"
    assert r["broker_name"] is None
    assert r["volume"] is None
    assert r["value"] is None
    assert r["frequency"] is None


def test_rows_without_idfirm_are_skipped():
    payload = {"data": [{"FirmName": "tanpa kode", "Volume": 1}, {"IDFirm": "OK"}, "bukan-dict", 42]}
    rows = parse_broker_rows(payload)
    assert [r["broker_code"] for r in rows] == ["OK"]


def test_empty_or_broken_payloads():
    assert parse_broker_rows(None) == []
    assert parse_broker_rows({}) == []
    assert parse_broker_rows({"data": "bukan-list"}) == []
    assert parse_broker_rows({"data": []}) == []


def test_code_whitespace_is_stripped():
    payload = {"data": [{"IDFirm": "  AB  "}]}
    assert parse_broker_rows(payload)[0]["broker_code"] == "AB"


def test_num_int_helpers():
    assert _num(1.5) == 1.5
    assert _num("2,000") == 2000.0
    assert _num("") is None
    assert _num(None) is None
    assert _num("bukan angka") is None
    assert _int("3.7") == 3
    assert _int(None) is None
