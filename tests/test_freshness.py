"""Tes kesegaran: tiga status (spec D1) — murni, tanpa DB."""

from __future__ import annotations

from datetime import datetime

from idx_scraper.freshness import (
    BASI,
    SEGAR,
    TAK_DIKETAHUI,
    current,
    expected_session,
    format_message,
    verdict,
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
