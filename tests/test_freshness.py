"""Tes kesegaran: tiga status (spec D1) + penahanan verdict — murni, tanpa DB."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from idx_scraper.freshness import (
    BASI,
    SEGAR,
    TAK_DIKETAHUI,
    current,
    expected_session,
    format_message,
    verdict,
    withhold,
)
from idx_scraper.health import WIB


def test_kesegaran_segar_saat_panel_sama_dengan_sesi_diharapkan():
    v = verdict("2026-10-16", "2026-10-16")
    assert v["status"] == SEGAR and v["withhold"] is False


def test_kesegaran_basi_menyebut_tanggal_dan_sebabnya():
    v = verdict("2026-10-14", "2026-10-16")
    assert v["status"] == BASI and v["withhold"] is True
    assert "2026-10-14" in v["reason"] and "2026-10-16" in v["reason"]


def test_kesegaran_tak_diketahui_saat_sesi_diharapkan_tak_bisa_ditentukan():
    v = verdict("2026-10-16", None)
    assert v["status"] == TAK_DIKETAHUI and v["withhold"] is True


def test_kesegaran_panel_kosong_bukan_segar():
    v = verdict(None, "2026-10-16")
    assert v["status"] == TAK_DIKETAHUI and v["withhold"] is True


def test_penanda_indeks_terpisah_tidak_menularkan_status_ke_halaman():
    v = verdict("2026-10-16", "2026-10-16", index_latest="2026-10-09")
    assert v["status"] == SEGAR and v["index_status"] == BASI


def test_sesi_hari_ini_belum_wajib_sebelum_jam_batas():
    # Jumat 10:00 WIB: sesi yang diharapkan masih Kamis, bukan Jumat.
    now = datetime(2026, 10, 16, 10, 0, tzinfo=WIB)
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-15"


def test_sesi_hari_ini_wajib_setelah_jam_batas():
    now = datetime(2026, 10, 16, 17, 0, tzinfo=WIB)
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-16"


def test_akhir_pekan_mundur_ke_jumat():
    now = datetime(2026, 10, 18, 9, 0, tzinfo=WIB)  # Minggu
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-16"


class _Cur:
    """Cursor palsu: fetchone mengembalikan nilai berikutnya, berurutan."""

    def __init__(self, values):
        self._values = list(values)
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchone(self):
        return {"d": self._values.pop(0)}


def test_current_memakai_basis_panel_untuk_verdict():
    cur = _Cur(["2026-10-16", "2026-10-09"])  # panel segar, indeks tertinggal
    out = current(cur, now=datetime(2026, 10, 18, 9, 0, tzinfo=WIB))
    assert out["status"] == SEGAR and out["index_status"] == BASI
    assert out["expected"] == "2026-10-16"


def test_current_menanyakan_dua_basis_yang_berbeda():
    cur = _Cur(["2026-10-16", "2026-10-16"])
    current(cur, now=datetime(2026, 10, 18, 9, 0, tzinfo=WIB))
    assert any("latest_pit" in q for q in cur.queries)
    assert any("index_quotes" in q for q in cur.queries)


def test_pesan_kesegaran_menyebut_tanggal_dan_alasan():
    v = verdict("2026-10-14", "2026-10-16")
    assert "2026-10-14" in format_message(v) and "2026-10-16" in format_message(v)


# ------------------------------------------------------- penahanan verdict D1


def _row(**extra):
    return {
        "code": "BBRI",
        "score": 52.0,
        "grade": "A",
        "position_pct": 12.5,
        "entry_ref": 4200.0,
        "stop": 3900.0,
        "target": 4900.0,
        **extra,
    }


def test_baris_kehilangan_grade_dan_ukuran_saat_basi():
    out = withhold([_row()], verdict("2026-10-14", "2026-10-16"))
    assert out[0]["grade"] is None and out[0]["position_pct"] is None


def test_angka_dan_level_tetap_terlihat_saat_basi():
    out = withhold([_row()], verdict("2026-10-14", "2026-10-16"))
    assert out[0]["score"] == 52.0 and out[0]["stop"] == 3900.0


def test_baris_utuh_saat_segar():
    out = withhold([_row()], verdict("2026-10-16", "2026-10-16"))
    assert out[0]["grade"] == "A" and out[0]["position_pct"] == 12.5


def test_penahanan_tidak_menghapus_baris_hanya_labelnya():
    assert len(withhold([_row(), _row(code="TLKM")], verdict(None, "2026-10-16"))) == 2


def test_penahanan_tidak_mengubah_dict_masukan():
    rows = [_row()]
    withhold(rows, verdict("2026-10-14", "2026-10-16"))
    assert rows[0]["grade"] == "A"


def _metrics_frame() -> pd.DataFrame:
    """Satu baris metrik yang cukup menghasilkan satu kandidat beli.

    Gaya fixture `tests/test_recommendation_log.py:38-60` (tanpa DB).
    """
    return pd.DataFrame(
        [
            {
                "code": "AAAA",
                "name": "Anak A",
                "date": pd.Timestamp("2026-05-01"),
                "close": 1000.0,
                "signal": "BUY",
                "trend_up": True,
                "rsi": 60.0,
                "mom_20d": 4.0,
                "vol_daily": 0.02,
                "dist_52w_pct": -1.0,
                "vol_ratio": 2.0,
                "bb_lower": 980.0,
                "range_low_20": None,
                "value": 5.0e9,
                "z_score": 0.5,
                "hist_days": 200,
                "pattern_buy": (),
                "pattern_sell": (),
            }
        ]
    )


def test_penahanan_diterapkan_sesudah_cache_dibaca(monkeypatch):
    """Cache 30 menit tidak boleh mengunci status segar/basi (Review Focus 5)."""
    from idx_scraper.api import analytics

    ctx = {
        "metrics": _metrics_frame(),
        "histories": {},
        "factor_adj": {},
        "factor_validated": False,
        "factor_note": "-",
        "broker_adj": {},
        "broker_validated": False,
        "broker_note": "-",
    }
    cache: dict = {}
    monkeypatch.setattr(analytics, "_recommendation_context", lambda: ctx)
    monkeypatch.setattr(analytics, "_research_cache_get", lambda key: cache.get(key))
    monkeypatch.setattr(
        analytics, "_research_cache_put", lambda key, val, ttl=None: cache.__setitem__(key, val)
    )
    monkeypatch.setattr(
        "idx_scraper.api.services.get_freshness",
        lambda: verdict("2026-10-14", "2026-10-16"),
    )

    out = analytics.get_recommendations()
    assert out.get("withheld") is True
    assert out["rows"] and all(r["grade"] is None for r in out["rows"])
    assert all(r.get("withheld") is True for r in out["rows"])
    # Yang tersimpan di cache tetap utuh: status berikutnya tak mewarisi penahanan.
    assert "withheld" not in cache["recommendations:20:C"]["rows"][0]
