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

from idx_scraper.research.claim_eval import claim_verdict
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


def test_sebab_rezim_tidak_diulang_di_pesan_gerbang() -> None:
    """`regime.reason` sudah diawali "rezim menolak" - pesan gerbang tidak mengulanginya."""
    sebab = "rezim menolak: tren turun terkonfirmasi (ADX 49.0) - tahan posisi baru"
    out = claim_gate(gate_blocked=True, gate_reason=sebab)
    assert out["message"] == (
        "Gerbang rezim menolak klaim baru: tren turun terkonfirmasi (ADX 49.0) - tahan posisi baru."
    )
    assert "rezim menolak: rezim menolak" not in out["message"]


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


def test_klaim_terbuka_bukan_klaim_terukur() -> None:
    """Klaim yang belum selesai tidak boleh ikut dihitung sebagai bukti.

    Verdict berdiri di atas `measured_claims`; kalau baris terbuka ikut dihitung,
    ambang 30 klaim tercapai sebelum satu pun klaim sempat dinilai.
    """
    rows = [_row("2026-10-15", abn=None, status="terbuka")] + [
        _row("2026-10-15", abn=0.02, status="kena_target")
    ]
    out = summarize_claims(rows)
    assert out["claims"] == 2
    assert out["measured_claims"] == 1


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


def test_horizon_tidak_sah_ditolak() -> None:
    """Klaim tanpa horizon tidak pernah matang, jadi ditolak di muka.

    Spec lampiran A.1 minta satu tes per aturan pre-commit; horizon tadinya satu-satunya
    yang tidak punya, padahal `horizon_days` NULL membuat barisnya duduk terbuka selamanya.
    """
    assert (
        claim_reject_reason(
            entry=113.42, stop=106.33, target=127.60, size_pct=16.0, horizon_days=None
        )
        == "horizon"
    )
    assert (
        claim_reject_reason(
            entry=113.42, stop=106.33, target=127.60, size_pct=16.0, horizon_days=0
        )
        == "horizon"
    )


def test_ukuran_lewat_lot_saja_tetap_lolos() -> None:
    """Spec 6.2 menerima ukuran persen ATAU lot: salah satu kosong bukan cacat."""
    assert (
        claim_reject_reason(
            entry=113.42,
            stop=106.33,
            target=127.60,
            size_pct=None,
            size_lots=2,
            horizon_days=21,
        )
        is None
    )


def test_sesi_terukur_dipisah_dari_sesi_menulis() -> None:
    """Ambang "enam minggu" menghitung sesi terukur, bukan sesi yang cuma menulis.

    30 sesi menulis dengan 8 sesi terukur tidak boleh terbaca sebagai bukti 30 sesi:
    kalau ikut disuapkan ke verdict, rata-ratanya berdiri di atas 8 sesi sementara
    kalimatnya menyebut 30, dan kill-switch memutuskan dari sampel yang belum layak.
    """
    rows = [_row(f"2026-10-{d:02d}", abn=0.02, status="kena_target") for d in range(1, 9)]
    rows += [_row(f"2026-09-{d:02d}", abn=None, status="terbuka") for d in range(1, 23)]
    out = summarize_claims(rows)

    assert out["claims"] == 30
    assert out["measured_claims"] == 8
    assert out["sessions"] == 8
    assert out["claim_sessions"] == 30
    assert out["mean_abnormal"] == pytest.approx(0.02)

    verdict = claim_verdict(out)
    assert verdict["status"] == "belum cukup"
    assert "8 klaim terukur" in verdict["reason"]
    assert "30 sesi tercapai" not in verdict["reason"]


def test_baris_berhorizon_lain_tidak_masuk_rata_rata() -> None:
    """Rata-rata campur horizon bukan bukti: yang tidak sebanding dinyatakan, bukan dirata-ratakan.

    Barisnya tetap dihitung sebagai klaim dan tetap muncul di ``horizon_mismatch``
    supaya ketidakcocokannya terlihat di layar.
    """
    rows = [
        _row("2026-10-15", abn=0.50, status="kena_target", horizon=5),
        _row("2026-10-16", abn=-0.02, status="kena_stop", horizon=21),
    ]
    out = summarize_claims(rows)
    assert out["claims"] == 2
    assert out["horizon_mismatch"] == 1
    assert out["measured_claims"] == 1
    assert out["sessions"] == 1
    assert out["mean_abnormal"] == pytest.approx(-0.02)
