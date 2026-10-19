"""Unit tests for idx_scraper.research.journal (aturan buku klaim).

Tidak menyentuh DB atau jaringan.

Yang dikunci di sini adalah aturan yang kalau longgar akan membuat buku bukti
berbohong: gerbang menolak dengan sebab pertama dalam urutan tetap (manual,
data, rezim, verdict), klaim tanpa stop/target/ukuran ditolak dan bukan
diperingatkan, dan rata-rata hasil dihitung per sesi dulu lalu antar sesi,
bukan per klaim, supaya satu sesi dengan banyak nama tidak mendominasi.
"""

from __future__ import annotations

import pytest

from idx_scraper.research.journal import (
    CLAIM_COLUMNS,
    GATE_REASONS,
    RULE_PAPAN,
    claim_from_candidate,
    claim_gate,
    claim_reject_reason,
    summarize_claims,
)


def _kandidat(**ubah: object) -> dict[str, object]:
    """Satu kandidat lengkap, bentuknya seperti baris recommendation_log."""
    kandidat: dict[str, object] = {
        "code": "PEGE",
        "entry_ref": 113.42,
        "stop": 106.33,
        "target": 127.60,
        "horizon_days": 21,
        "grade": "A",
        "size_pct": 16.0,
    }
    kandidat.update(ubah)
    return kandidat


def _row(
    trade_date: str, *, abn: float | None = None, status: str = "terbuka", horizon: int = 21
) -> dict[str, object]:
    """Satu baris buku klaim seadanya, cukup untuk summarize_claims."""
    return {
        "trade_date": trade_date,
        "outcome_abn": abn,
        "status": status,
        "horizon_days": horizon,
    }


def test_gerbang_manual_menang_atas_sebab_lain() -> None:
    """Halt manual disebut lebih dulu, walau data juga basi dan rezim menolak."""
    out = claim_gate(
        manual_halt=True,
        halt_reason="berhenti sejenak",
        freshness_status="basi",
        gate_blocked=True,
        gate_reason="rezim TRENDING_DOWN menolak",
    )
    assert out["allowed"] is False
    assert out["reason"] == "manual"
    assert "berhenti sejenak" in out["message"]


def test_gerbang_urutan_sebab_manual_data_rezim_verdict() -> None:
    """Urutan tetap: tiap keadaan sendirian memulangkan sebabnya sendiri."""
    assert GATE_REASONS == ("manual", "data", "regime", "verdict")
    assert claim_gate(manual_halt=True, halt_reason="x")["reason"] == "manual"
    assert claim_gate(freshness_status="basi")["reason"] == "data"
    assert claim_gate(gate_blocked=True, gate_reason="rezim menolak")["reason"] == "regime"
    assert claim_gate(verdict="tidak terbukti")["reason"] == "verdict"


def test_gerbang_mengizinkan_saat_tidak_ada_sebab() -> None:
    out = claim_gate(freshness_status="segar", verdict="belum cukup")
    assert out["allowed"] is True
    assert out["reason"] is None


def test_gerbang_mengizinkan_saat_kesegaran_tak_diketahui() -> None:
    """Kesegaran None bukan basi: penahanan P4 yang memutuskan, bukan gerbang ini."""
    assert claim_gate()["allowed"] is True


def test_klaim_tanpa_stop_ditolak() -> None:
    assert (
        claim_reject_reason(entry=113.42, stop=None, target=127.60, size_pct=16.0)
        == "tanpa_stop"
    )


def test_klaim_tanpa_target_ditolak() -> None:
    assert (
        claim_reject_reason(entry=113.42, stop=106.33, target=None, size_pct=16.0)
        == "tanpa_target"
    )


def test_klaim_tanpa_ukuran_ditolak() -> None:
    assert claim_reject_reason(entry=113.42, stop=106.33, target=127.60) == "tanpa_ukuran"


def test_geometri_tidak_masuk_akal_ditolak() -> None:
    assert (
        claim_reject_reason(entry=113.42, stop=113.42, target=127.60, size_pct=16.0)
        == "geometri"
    )
    assert (
        claim_reject_reason(entry=113.42, stop=106.33, target=113.42, size_pct=16.0)
        == "geometri"
    )
    assert claim_reject_reason(entry=0.0, stop=-1.0, target=1.0, size_pct=16.0) == "geometri"


def test_klaim_lengkap_lolos_dan_bentuknya_sesuai_kolom() -> None:
    klaim = claim_from_candidate(_kandidat(), trade_date="2026-10-16", ref_adj=112.98)
    assert klaim is not None
    assert klaim["code"] == "PEGE"
    assert klaim["rule"] == RULE_PAPAN
    assert klaim["trade_date"] == "2026-10-16"
    assert klaim["direction"] == "BUY"
    assert klaim["ref_raw"] == pytest.approx(113.42)
    assert klaim["stop_raw"] == pytest.approx(106.33)
    assert klaim["target_raw"] == pytest.approx(127.60)
    assert klaim["ref_adj"] == pytest.approx(112.98)
    assert klaim["horizon_days"] == 21
    assert klaim["size_pct"] == pytest.approx(16.0)
    assert klaim["status"] == "terbuka"
    assert klaim["outcome_abn"] is None
    assert klaim["touch_date"] is None
    assert set(klaim) == set(CLAIM_COLUMNS)


def test_klaim_tidak_lengkap_tidak_dikembalikan() -> None:
    assert claim_from_candidate(_kandidat(stop=None), trade_date="2026-10-16") is None


def test_agregat_dirata_per_sesi_bukan_per_klaim() -> None:
    """Dua sesi: sesi pertama 3 klaim (+1% rata-rata), sesi kedua 1 klaim (-3%).

    Rata-rata per sesi = -1,0%, bukan rata-rata per klaim = 0,0%.
    """
    rows = [_row("2026-10-15", abn=0.01, status="kena_target")] * 3 + [
        _row("2026-10-16", abn=-0.03, status="kena_stop")
    ]
    out = summarize_claims(rows)
    assert out["claims"] == 4
    assert out["sessions"] == 2
    assert out["hit_target"] == 3
    assert out["hit_stop"] == 1
    assert out["mean_abnormal"] == pytest.approx(-0.01)
    assert [s["trade_date"] for s in out["per_session"]] == ["2026-10-15", "2026-10-16"]


def test_buku_kosong_bukan_nol() -> None:
    out = summarize_claims([])
    assert out["claims"] == 0
    assert out["mean_abnormal"] is None
    assert out["sessions"] == 0
    assert out["per_session"] == []


def test_hasil_belum_terukur_tidak_dihitung_sebagai_nol() -> None:
    """Klaim terbuka tidak punya hasil: rata-rata tetap None, bukan 0,0%."""
    out = summarize_claims([_row("2026-10-16", abn=None, status="terbuka")])
    assert out["claims"] == 1
    assert out["open"] == 1
    assert out["mean_abnormal"] is None
