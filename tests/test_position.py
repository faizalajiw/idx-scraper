"""Tests rencana posisi lapis 3 (research/position.py): persen, lot, plafon.

Kontrak yang diuji (spec P5 revisi 3):

* Porsi dasar tetap milik ``recommend.position_size_pct`` (risiko / jarak stop),
  dan plafon per nama tetap 25% modal.
* Empat plafon bernama boleh memotong: per nama, likuiditas (2% median nilai
  transaksi 5 sesi terakhir), rezim (VOLATILE memotong separuh), portofolio
  (6 posisi terbuka, eksposur 80% modal).
* Modal adalah input pemilik. Tanpa modal, rencana hanya dalam persen: tidak ada
  rupiah, lot, atau rugi yang dikarang.
"""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.research.position import (
    DEFAULT_LIMITS,
    position_plan,
    typical_day_value,
    typical_day_values,
)

PEGE_ENTRY = 113.42
PEGE_STOP = 106.33


def pege(**kw):
    """Rencana PEGE dengan modal Rp 100 juta dan risiko 1% (keadaan lengkap).

    Keadaan portofolio sengaja lengkap (0 posisi, 0% eksposur) supaya ``note``
    hanya bicara soal plafon per nama/likuiditas/rezim. Tes keadaan kosong
    memanggil ``pege(open_positions=None, exposure_pct=None)``.
    """
    base = {
        "entry": PEGE_ENTRY,
        "stop": PEGE_STOP,
        "modal": 100_000_000.0,
        "risk_pct": 1.0,
        "open_positions": 0,
        "exposure_pct": 0.0,
    }
    base.update(kw)
    return position_plan(**base)


def test_pct_raw_memakai_aturan_lama():
    p = pege()
    assert p["pct_raw"] == 16.0
    assert p["pct"] == 16.0
    assert p["binding_cap"] is None
    assert p["note"] == "Tidak ada plafon yang memotong."


def test_tanpa_modal_hanya_persen():
    p = pege(modal=None)
    assert p["pct"] == 16.0
    assert p["rupiah"] is None
    assert p["lots"] is None
    assert p["risk_rupiah"] is None
    assert "tanpa modal" in p["note"].lower()


def test_modal_penuh_rupiah_lot_rugi():
    p = pege()
    assert p["lots"] == 1410
    assert p["rupiah"] == pytest.approx(15_992_220.0, rel=1e-9)
    assert p["risk_rupiah"] == pytest.approx(999_690.0, rel=1e-9)


def test_modal_tidak_valid_dianggap_tanpa_modal():
    for modal in (0, -5.0):
        p = pege(modal=modal)
        assert p["lots"] is None
        assert p["rupiah"] is None


def test_risk_pct_tidak_valid_pakai_default():
    assert pege(risk_pct=0)["pct_raw"] == 16.0
    assert pege(risk_pct=None)["pct_raw"] == 16.0


def test_stop_tidak_valid_tidak_ada_rencana():
    p = pege(stop=PEGE_ENTRY)
    assert p["pct"] is None
    assert p["pct_raw"] is None
    assert p["lots"] is None


def test_plafon_per_nama_memotong():
    p = pege(entry=100.0, stop=90.0, risk_pct=10.0)
    assert p["pct_raw"] == 100.0
    assert p["caps"]["per_name"] == 25.0
    assert p["pct"] == 25.0
    assert p["binding_cap"] == "per_name"


def test_plafon_likuiditas_memotong_dan_disebut():
    p = pege(typical_day_value=500_000_000.0)  # 2% x 500jt / 100jt = 10% modal
    assert p["caps"]["liquidity"] == 10.0
    assert p["pct"] == 10.0
    assert p["binding_cap"] == "liquidity"
    assert "likuiditas" in p["note"].lower()


def test_plafon_likuiditas_dilewati_tanpa_modal():
    p = pege(modal=None, typical_day_value=500_000_000.0)
    assert p["caps"]["liquidity"] is None
    assert p["pct"] == 16.0


def test_plafon_rezim_memotong_separuh():
    p = pege(vol_state="VOLATILE")
    assert p["caps"]["regime"] == 8.0
    assert p["pct"] == 8.0
    assert p["binding_cap"] == "regime"


def test_rezim_normal_tidak_memotong():
    assert pege(vol_state="NORMAL")["caps"]["regime"] is None


def test_buku_penuh_porsi_nol():
    p = pege(open_positions=6)
    assert p["pct"] == 0.0
    assert p["lots"] == 0
    assert p["rupiah"] == 0.0
    assert p["binding_cap"] == "portfolio"
    assert "penuh" in p["note"].lower()


def test_plafon_eksposur_memotong():
    p = pege(exposure_pct=78.0)
    assert p["caps"]["portfolio"] == 2.0
    assert p["pct"] == 2.0
    assert p["binding_cap"] == "portfolio"


def test_dua_plafon_memotong_menyebut_yang_paling_kecil():
    """Rezim memotong separuh, buku penuh memotong sampai nol (spec 5.1).

    Yang benar-benar memotong adalah plafon yang menyentuh angka akhir, bukan
    plafon pertama yang kebetulan lebih kecil dari porsi mentah.
    """
    p = pege(vol_state="VOLATILE", typical_day_value=950_034_000.0, open_positions=6)
    assert p["caps"]["regime"] == 8.0
    assert p["caps"]["portfolio"] == 0.0
    assert p["pct"] == 0.0
    assert p["binding_cap"] == "portfolio"
    assert "Sudah penuh: 6 posisi terbuka dari batas 6." in p["note"]


def test_buku_penuh_tidak_menyalahkan_modal():
    """Porsi nol karena buku penuh bukan karena modal kurang (spec 5.1 D14)."""
    p = pege(vol_state="VOLATILE", open_positions=6, exposure_pct=None)
    assert p["pct"] == 0.0
    assert p["lots"] == 0
    assert "Modal kurang untuk 1 lot." not in p["note"]
    assert "Eksposur belum diisi" in p["note"]


# --- Temuan reviewer konteks segar (19 Okt 2026): M-1, M-2, M-3, I-2 ---


def test_kalimat_keadaan_kosong_menyebut_bidangnya():
    """M-1: "plafon portofolio dilewati" tidak boleh muncul saat plafonnya bekerja."""
    p = pege(open_positions=6, exposure_pct=None)
    assert "Sudah penuh: 6 posisi terbuka dari batas 6." in p["note"]
    assert "Eksposur belum diisi" in p["note"]
    assert "plafon portofolio dilewati" not in p["note"]

    q = pege(open_positions=None, exposure_pct=78.0)
    assert q["caps"]["portfolio"] == 2.0
    assert q["pct"] == 2.0
    assert "Posisi terbuka belum diisi" in q["note"]
    assert "plafon portofolio dilewati" not in q["note"]


def test_buku_penuh_dengan_likuiditas_nol_tetap_menyebut_penuh():
    """M-2: plafon likuiditas bisa memenangkan urutan, "sudah penuh" tetap wajib."""
    p = pege(typical_day_value=1_000.0, open_positions=6)
    assert p["caps"]["liquidity"] == 0.0
    assert p["binding_cap"] == "liquidity"
    assert p["pct"] == 0.0
    assert "Sudah penuh: 6 posisi terbuka dari batas 6." in p["note"]


def test_keadaan_portofolio_negatif_dianggap_belum_diisi():
    """M-3: rute menolak negatif; fungsi murni tidak boleh menghitung risiko negatif."""
    p = pege(open_positions=-1, exposure_pct=-5.0)
    assert p["concurrent_risk_pct"] is None
    assert p["caps"]["portfolio"] is None
    assert "belum diisi" in p["note"]


def test_peta_likuiditas_sama_dengan_hitung_satu_emiten():
    """I-2: versi massal harus sama persis dengan versi per emiten."""
    frame = pd.DataFrame(
        [
            {"code": code, "date": pd.Timestamp(f"2026-10-{day:02d}"), "value": value}
            for code, value in (("PEGE", 1.0e9), ("MGRO", 5.0e8), ("UNTD", 2.0e8))
            for day in range(1, 9)
        ]
    )
    peta = typical_day_values(frame)
    for code in ("PEGE", "MGRO", "UNTD", "pege", "TIDAK ADA"):
        assert peta.get(str(code).upper()) == typical_day_value(frame, code)


def test_peta_likuiditas_menghormati_batas_sesi():
    """Versi massal memakai jendela sesi yang sama, termasuk nilai ``sessions``."""
    frame = pd.DataFrame(
        [
            {"code": "PEGE", "date": pd.Timestamp(f"2026-10-{day:02d}"), "value": float(day)}
            for day in range(1, 11)
        ]
    )
    peta = typical_day_values(frame, sessions=3)
    assert peta["PEGE"] == 9.0  # median 8, 9, 10
    assert peta["PEGE"] == typical_day_value(frame, "PEGE", sessions=3)


def test_peta_likuiditas_tahan_panel_kosong():
    """Panel kosong atau tanpa kolom yang dibutuhkan -> peta kosong, bukan meledak."""
    assert typical_day_values(pd.DataFrame(columns=["code", "date", "value"])) == {}
    assert typical_day_values(pd.DataFrame([{"code": "PEGE"}])) == {}


def test_keadaan_portofolio_kosong_dilewati():
    p = pege(open_positions=None, exposure_pct=None)
    assert p["caps"]["portfolio"] is None
    assert p["concurrent_risk_pct"] is None
    assert "batas jumlah posisi dan batas eksposur dilewati" in p["note"]


def test_risiko_serentak_diturunkan():
    p = pege(open_positions=3)
    assert p["concurrent_risk_pct"] == 3.0
    assert p["caps"]["portfolio"] == 80.0  # eksposur 0%: seluruh batas 80% masih ruang


def test_modal_kurang_untuk_satu_lot():
    p = pege(modal=50_000.0)  # 16% x 50rb = 8rb, di bawah 1 lot (Rp 11.342)
    assert p["lots"] == 0
    assert "1 lot" in p["note"]


def test_typical_day_value_median_lima_sesi():
    rows = [
        {"code": "AAAA", "date": pd.Timestamp(f"2026-10-{i:02d}"), "value": v}
        for i, v in enumerate([1e9, 2e9, 3e9, 4e9, 5e9, 6e9, 7e9], start=1)
    ]
    rows.append({"code": "BBBB", "date": pd.Timestamp("2026-10-01"), "value": 9e9})
    frame = pd.DataFrame(rows)
    assert typical_day_value(frame, "AAAA") == 5e9  # median dari 5 sesi terakhir
    assert typical_day_value(frame, "BBBB") == 9e9  # riwayat lebih pendek tetap dipakai
    assert typical_day_value(frame, "CCCC") is None


def test_typical_day_value_none_bila_tidak_ada():
    frame = pd.DataFrame([{"code": "AAAA", "date": pd.Timestamp("2026-10-01"), "value": None}])
    assert typical_day_value(frame, "AAAA") is None


def test_adv_pct_sama_dengan_model_biaya_backtest():
    from idx_scraper.api.simulation import DEFAULT_COSTS

    assert DEFAULT_LIMITS.adv_pct == DEFAULT_COSTS["max_adv_pct"]


# ------------------------------------------------- helper ekstraksi position_plan


def test_normalize_position_inputs_applies_defaults():
    from idx_scraper.research.position import _normalize_position_inputs

    risk, m, open_n, expo = _normalize_position_inputs(
        risk_pct=0.5,
        modal=50_000_000.0,
        open_positions=2,
        exposure_pct=30.0,
    )
    assert risk == 0.5
    assert m == 50_000_000.0
    assert open_n == 2
    assert expo == 30.0


def test_normalize_position_inputs_rejects_negatif():
    from idx_scraper.research.position import _normalize_position_inputs

    risk, m, open_n, expo = _normalize_position_inputs(
        risk_pct=-1.0,  # negatif -> None (default)
        modal=-100.0,  # negatif -> None
        open_positions=-1,  # negatif -> None
        exposure_pct=None,
    )
    from idx_scraper.research.recommend import DEFAULT_RISK_PCT

    assert risk == float(DEFAULT_RISK_PCT)
    assert m is None
    assert open_n is None
    assert expo is None


def test_normalize_position_inputs_none_is_passed_through():
    from idx_scraper.research.position import _normalize_position_inputs

    risk, m, open_n, expo = _normalize_position_inputs(
        risk_pct=None,
        modal=None,
        open_positions=None,
        exposure_pct=None,
    )
    from idx_scraper.research.recommend import DEFAULT_RISK_PCT

    assert risk == float(DEFAULT_RISK_PCT)
    assert m is None
    assert open_n is None
    assert expo is None


def test_resolve_position_caps_gives_binding():
    from idx_scraper.research.position import DEFAULT_LIMITS, _resolve_position_caps

    _, pct, binding = _resolve_position_caps(
        15.0,
        m=50_000_000.0,
        typical_day_value=1e9,
        vol_state="VOLATILE",
        open_n=None,
        expo=None,
        limits=DEFAULT_LIMITS,
    )
    assert "per_name" in _
    assert 5.0 <= pct <= 15.0  # volatile cuts it in half
    assert binding in ("regime", "per_name")


def test_resolve_position_caps_portfolio_full_gives_room_zero():
    from idx_scraper.research.position import DEFAULT_LIMITS, _resolve_position_caps

    caps, _, binding = _resolve_position_caps(
        10.0,
        m=50_000_000.0,
        typical_day_value=1e9,
        vol_state="NORMAL",
        open_n=10,
        expo=80.0,
        limits=DEFAULT_LIMITS,
    )
    assert binding == "portfolio"
    assert caps["portfolio"] == 0.0
