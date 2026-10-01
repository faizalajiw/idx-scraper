"""Unit tests: latch transisi verdict jejak smart money (anti-spam).

Semua tanpa DB — ``SmartMoneyState.evaluate`` diberi daftar verdict buatan.
Verifikasi fokus pada aturan "berubah bermakna": baru masuk, baru keluar,
balik arah, dan netral/kosong TIDAK memicu.
"""

from __future__ import annotations

from pathlib import Path

from idx_scraper.smart_money_state import SmartMoneyState


def _v(side: str, netval: float = 10.0, net_sum: float = 1.0e10,
       insufficient: bool = False, streak: int = 5, date: str = "2026-09-30") -> dict:
    return {
        "side": side,
        "strength": "menengah",
        "streak": streak,
        "streak_side": "net_buy" if side == "akumulasi" else "net_sell",
        "net_sum_idr": net_sum,
        "netval_pct": netval,
        "n_net_days": 10,
        "date": date,
        "insufficient": insufficient,
    }


def _fresh_state() -> SmartMoneyState:
    # Path unik per test (di /tmp) supaya tidak saling mengontaminasi lewat disk.
    import uuid

    return SmartMoneyState(f"/tmp/sm_state_{uuid.uuid4().hex}.json")


def test_first_run_seeds_baseline_without_alert():
    # Run pertama untuk sebuah kode hanya mencatat baseline (tidak membanjiri
    # Telegram dengan semua emiten non-netral sekaligus).
    s = _fresh_state()
    ev = s.evaluate([{"code": "BBCA", "name": "BCA", "verdict": _v("akumulasi")}])
    assert ev == []
    # ...tapi baseline tercatat: perubahan berikutnya terdeteksi.
    ev2 = s.evaluate([{"code": "BBCA", "verdict": _v("distribusi", net_sum=-1.0e10)}])
    assert len(ev2) == 1
    assert ev2[0].code == "BBCA"
    assert ev2[0].side == "distribusi"
    assert ev2[0].previous_side == "akumulasi"


def test_baseline_distribution_then_reversal_fires():
    s = _fresh_state()
    assert s.evaluate([{"code": "BMRI", "verdict": _v("distribusi", net_sum=-1.0e10)}]) == []
    ev = s.evaluate([{"code": "BMRI", "verdict": _v("akumulasi")}])
    assert len(ev) == 1
    assert ev[0].side == "akumulasi"
    assert ev[0].previous_side == "distribusi"


def test_same_side_again_does_not_refire():
    s = _fresh_state()
    s.evaluate([{"code": "BBCA", "verdict": _v("akumulasi")}])  # seed
    ev2 = s.evaluate([{"code": "BBCA", "verdict": _v("akumulasi")}])  # masih sama
    assert ev2 == []  # anti-spam: tidak bunyi lagi


def test_reversal_fires():
    s = _fresh_state()
    s.evaluate([{"code": "GOTO", "verdict": _v("akumulasi")}])  # seed akumulasi
    ev = s.evaluate([{"code": "GOTO", "verdict": _v("distribusi", net_sum=-1.0e10)}])
    assert len(ev) == 1
    assert ev[0].side == "distribusi"
    assert ev[0].previous_side == "akumulasi"


def test_neutral_does_not_fire():
    s = _fresh_state()
    # netral dari kosong -> tidak di-alert.
    ev = s.evaluate([{"code": "X", "verdict": _v("netral", netval=0.0)}])
    assert ev == []


def test_neutral_to_accumulation_fires():
    s = _fresh_state()
    s.evaluate([{"code": "X", "verdict": _v("netral", netval=0.0)}])  # seed netral
    ev = s.evaluate([{"code": "X", "verdict": _v("akumulasi")}])
    assert len(ev) == 1
    assert ev[0].side == "akumulasi"
    # netral sebelumnya tidak dianggap "sisi" -> previous_side None
    assert ev[0].previous_side is None


def test_insufficient_treated_as_neutral():
    s = _fresh_state()
    # insufficient dari kosong -> tidak di-alert.
    ev = s.evaluate([{"code": "NEW", "verdict": _v("akumulasi", insufficient=True)}])
    assert ev == []


def test_acc_to_neutral_no_event_then_acc_again_fires():
    s = _fresh_state()
    s.evaluate([{"code": "A", "verdict": _v("akumulasi")}])  # seed
    ev_neutral = s.evaluate([{"code": "A", "verdict": _v("netral", netval=0.0)}])
    assert ev_neutral == []  # kembali netral bukan berita
    # lalu akumulasi lagi -> fire (state netral -> "mulai").
    ev_again = s.evaluate([{"code": "A", "verdict": _v("akumulasi")}])
    assert len(ev_again) == 1
    assert ev_again[0].previous_side is None


def test_multiple_codes_independent():
    s = _fresh_state()
    # Run pertama: semua di-seed, tak ada event.
    assert s.evaluate(
        [
            {"code": "AAA", "verdict": _v("akumulasi")},
            {"code": "BBB", "verdict": _v("distribusi", net_sum=-1.0e10)},
            {"code": "CCC", "verdict": _v("netral", netval=0.0)},
        ]
    ) == []
    # Hanya BBB yang berbalik -> hanya BBB yang di-alert.
    ev = s.evaluate(
        [
            {"code": "AAA", "verdict": _v("akumulasi")},
            {"code": "BBB", "verdict": _v("akumulasi")},
            {"code": "CCC", "verdict": _v("netral", netval=0.0)},
        ]
    )
    assert {e.code for e in ev} == {"BBB"}


def test_persistence_across_instances():
    import uuid

    path = f"/tmp/sm_state_persist_{uuid.uuid4().hex}.json"
    s1 = SmartMoneyState(path)
    s1.evaluate([{"code": "BBRI", "verdict": _v("akumulasi")}])  # seed + simpan
    # Instance baru, path sama -> state terbaca, tidak fire (masih akumulasi).
    s2 = SmartMoneyState(path)
    assert s2.evaluate([{"code": "BBRI", "verdict": _v("akumulasi")}]) == []
    # ...tapi jika berubah, fire.
    ev2 = s2.evaluate([{"code": "BBRI", "verdict": _v("distribusi", net_sum=-1.0e10)}])
    assert len(ev2) == 1 and ev2[0].previous_side == "akumulasi"
    Path(path).unlink(missing_ok=True)
