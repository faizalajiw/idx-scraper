"""Tes kolam kandidat: satu kolam, ambang ketebalan terukur (spec D4)."""

from __future__ import annotations

import pandas as pd

from idx_scraper.research.pool import (
    MIN_DAY_VALUE,
    PoolIndex,
    pool_members,
    thick_flags,
    value_threshold_for,
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


# --------------------------------------------------- kolam sekali hitung (P1 review)


def _lima_sesi():
    """Lima sesi bursa berturut-turut - fixture bersama tes kolam berjendela."""
    return list(pd.date_range("2026-09-25", periods=5, freq="B"))


def test_ambang_satu_sesi_sama_dengan_yang_dihitung_lintas_sesi():
    """Satu aturan: ambang satu sesi tidak punya definisi kedua."""
    dates = _lima_sesi()
    m = _frame(
        [{"code": f"C{i:03d}", "date": d, "value": float(i) + j} for j, d in enumerate(dates) for i in range(100)]
    )
    per_sesi = value_thresholds(m)
    stamped = pd.to_datetime(m["date"])
    for d in dates:
        assert value_threshold_for(m.loc[stamped == pd.Timestamp(d), "value"]) == per_sesi[pd.Timestamp(d)]


def test_index_kolam_sama_dengan_kolam_satu_tanggal():
    """PoolIndex harus jadi jalur yang sama, bukan definisi kedua."""
    dates = _lima_sesi()
    m = _frame(
        [{"code": "TEBAL", "date": d, "value": 9.0e8} for d in dates]
        + [{"code": "TIPIS", "date": d, "value": 1.0e8} for d in dates]
        + [{"code": "JUAL", "date": d, "value": 9.0e8, "signal": "SELL"} for d in dates]
    )
    index = PoolIndex(m, quarantined={"TIPIS"})
    assert index.members(dates[-1]) == {"TEBAL"}
    for d in dates:
        assert index.members(d) == pool_members(m, pd.Timestamp(d), quarantined={"TIPIS"})


def test_ambang_sesi_dihitung_sekali_untuk_banyak_tanggal(monkeypatch):
    """P1 (review): biaya ambang per sesi dibayar sekali, bukan per tanggal."""
    from idx_scraper.research import pool as pool_module

    dates = _lima_sesi()
    m = _frame([{"code": f"C{i:03d}", "date": d, "value": float(i)} for d in dates for i in range(60)])
    calls: list[int] = []
    asli = pool_module.value_threshold_for

    def dihitung(values):
        calls.append(1)
        return asli(values)

    monkeypatch.setattr(pool_module, "value_threshold_for", dihitung)
    index = PoolIndex(m)
    index.members(dates[-1])
    per_sesi = len(calls)
    index.members(dates[-1])  # tanggal yang sama: nol kerja ulang
    index.members(dates[-2])  # jendela tumpang tindih: nol kerja ulang
    assert per_sesi == len(dates)
    assert len(calls) == per_sesi


def test_build_records_tidak_menghitung_ambang_per_tanggal(monkeypatch):
    """Job jurnal (P1 review): ambang sesi dibayar sekali untuk seluruh jendela."""
    from idx_scraper.research import pool as pool_module
    from idx_scraper.research.recommendation_log import build_records

    dates = list(pd.date_range("2026-09-25", periods=6, freq="B"))
    m = _frame([{"code": f"C{i:03d}", "date": d, "value": float(i)} for d in dates for i in range(60)])
    calls: list[int] = []
    asli = pool_module.value_threshold_for

    def dihitung(values):
        calls.append(1)
        return asli(values)

    monkeypatch.setattr(pool_module, "value_threshold_for", dihitung)
    build_records(m, months=6)
    assert calls, "kolam jurnal tidak dihitung sama sekali"
    assert len(calls) <= len(dates), f"ambang dihitung {len(calls)}x untuk {len(dates)} tanggal"


def test_kolam_tidak_melihat_sesi_setelah_tanggal_target():
    """Anti-look-ahead (N7 review): tebal SESUDAH target tidak boleh menolong."""
    dates = _lima_sesi()
    m = _frame(
        [{"code": "TEBAL", "date": d, "value": 9.0e8} for d in dates]
        + [{"code": "BELAKANGAN", "date": d, "value": 1.0e8} for d in dates[:2]]
        + [{"code": "BELAKANGAN", "date": d, "value": 9.0e8} for d in dates[2:]]
    )
    index = PoolIndex(m)
    assert "BELAKANGAN" not in index.members(dates[1])
    assert "BELAKANGAN" in index.members(dates[-1])
