"""Tes kolam kandidat: satu kolam, ambang ketebalan terukur (spec D4)."""

from __future__ import annotations

import pandas as pd

from idx_scraper.research.pool import (
    MIN_DAY_VALUE,
    pool_members,
    thick_flags,
    value_thresholds,
)

_DATE = "2026-10-01"


def _frame(rows):
    """Baris ringkas -> metrics frame yang dibaca `research.pool`."""
    out = []
    for row in rows:
        out.append(
            {
                "code": row["code"],
                "name": row["code"],
                "date": pd.Timestamp(row["date"]),
                "close": 1000.0,
                "signal": row.get("signal", "BUY"),
                "trend_up": True,
                "rsi": 55.0,
                "mom_20d": 1.0,
                "vol_daily": 0.02,
                "dist_52w_pct": -5.0,
                "vol_ratio": 1.2,
                "bb_lower": 950.0,
                "range_low_20": 900.0,
                "value": row["value"],
                "z_score": 0.5,
                "hist_days": row.get("hist_days", 200),
                "pattern_buy": (),
                "pattern_sell": (),
            }
        )
    return pd.DataFrame(out)


def _metrics_frame(codes) -> pd.DataFrame:
    """Satu sesi, satu baris per kode — cukup untuk menghasilkan kandidat."""
    return _frame([{"code": c, "date": _DATE, "value": 5.0e9} for c in codes])


def test_ambang_tebal_adalah_persentil_enam_puluh_lintas_saham():
    m = _frame([{"code": f"C{i:03d}", "date": _DATE, "value": float(i)} for i in range(1, 101)])
    th = value_thresholds(m)[pd.Timestamp(_DATE)]
    assert 60.0 <= th < 61.0


def test_satu_lonjakan_tidak_cukup_tiga_dari_lima():
    dates = list(pd.date_range("2026-09-25", periods=5, freq="B"))
    m = _frame(
        [{"code": "TEBAL", "date": d, "value": 9.0e8} for d in dates]
        + [{"code": "LONJAKAN", "date": d, "value": 1.0e8} for d in dates[:-1]]
        + [{"code": "LONJAKAN", "date": dates[-1], "value": 9.0e9}]
    )
    pool = pool_members(m, dates[-1])
    assert "TEBAL" in pool and "LONJAKAN" not in pool


def test_konsisten_tiga_dari_lima_masuk_kolam():
    dates = list(pd.date_range("2026-09-25", periods=5, freq="B"))
    m = _frame(
        [{"code": "TEBAL", "date": d, "value": 9.0e8} for d in dates]
        + [{"code": "TIPIS", "date": d, "value": 1.0e8} for d in dates[:2]]
        + [{"code": "TIPIS", "date": d, "value": 9.0e8} for d in dates[2:]]
    )
    pool = pool_members(m, dates[-1])
    assert "TEBAL" in pool and "TIPIS" in pool


def test_kolam_jatuh_ke_lantai_absolut_kalau_lintas_saham_terlalu_tipis():
    m = _frame(
        [{"code": f"C{i}", "date": _DATE, "value": MIN_DAY_VALUE + i} for i in range(5)]
    )
    assert value_thresholds(m)[pd.Timestamp(_DATE)] == MIN_DAY_VALUE
    assert pool_members(m, _DATE) == {"C0", "C1", "C2", "C3", "C4"}


def test_kode_karantina_tidak_masuk_kolam():
    m = _frame(
        [
            {"code": "BERSIH", "date": _DATE, "value": 9.0e8},
            {"code": "KOTOR", "date": _DATE, "value": 9.0e8},
        ]
    )
    assert pool_members(m, _DATE, quarantined={"KOTOR"}) == {"BERSIH"}


def test_histori_pendek_dan_sinyal_sell_tidak_masuk_kolam():
    m = _frame(
        [
            {"code": "MUDA", "date": _DATE, "value": 9.0e8, "hist_days": 30},
            {"code": "JUAL", "date": _DATE, "value": 9.0e8, "signal": "SELL"},
            {"code": "SIAP", "date": _DATE, "value": 9.0e8},
        ]
    )
    assert pool_members(m, _DATE) == {"SIAP"}


def test_thick_flags_menandai_baris_di_bawah_ambang_sesinya():
    m = _frame(
        [
            {"code": "A", "date": _DATE, "value": 9.0e8},
            {"code": "B", "date": _DATE, "value": 1.0e8},
        ]
    )
    assert list(thick_flags(m)) == [True, False]


def test_kolam_memakai_satu_daftar_untuk_semua_grade():
    """Grade dihitung dari kolam yang sama, bukan dari daftar terpisah."""
    from idx_scraper.research.recommendation_log import candidates_for_date

    m = _metrics_frame(("AAAA", "BBBB"))
    both = candidates_for_date(m, pd.Timestamp(_DATE))
    assert {c["code"] for c in both} == {"AAAA", "BBBB"}
    only_a = candidates_for_date(m, pd.Timestamp(_DATE), pool={"AAAA"})
    assert [c["code"] for c in only_a] == ["AAAA"]
    assert only_a[0]["grade"] is not None
