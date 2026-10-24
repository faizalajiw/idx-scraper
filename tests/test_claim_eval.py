"""Unit tests for idx_scraper.research.claim_eval (hasil satu klaim).

Tidak menyentuh DB atau jaringan.

Yang dikunci di sini adalah aturan pengukuran yang kalau salah akan membuat
seluruh rekaman mesin berbohong: level (stop/target) dibaca dari **harga mentah**
dan hasilnya diukur pada **harga terkoreksi**; kalau satu sesi menyentuh stop dan
target sekaligus, yang dicatat **stop** (asumsi terburuk); dan sesi entry sendiri
tidak bisa memicu apa pun karena levelnya ditetapkan dari close sebelumnya.
"""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.research.claim_eval import (
    VERDICT_HORIZON,
    claim_outcomes,
    claim_verdict,
    format_record_line,
    session_aggregate,
)
from idx_scraper.research.signal_log import DEFAULT_HORIZONS

_SESSIONS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def _panel(rows: list[tuple[str, float, float, float]], code: str = "AAA") -> pd.DataFrame:
    """``rows`` = (tanggal, high, low, close)."""
    return pd.DataFrame(
        [{"code": code, "date": d, "high": h, "low": lo, "close": c} for d, h, lo, c in rows]
    )


def _quiet_raw(extra: list[tuple[str, float, float, float]] | None = None) -> pd.DataFrame:
    """Panel mentah yang tidak menyentuh level apa pun, kecuali ``extra``."""
    rows = {d: (101.0, 99.0, 100.0) for d in _SESSIONS}
    for d, h, lo, c in extra or []:
        rows[d] = (h, lo, c)
    return _panel([(d, *rows[d]) for d in _SESSIONS])


def _flat_adj(codes: tuple[str, ...] = ("AAA",), sessions: list[str] | None = None) -> pd.DataFrame:
    """Panel terkoreksi datar — cukup untuk mengisi kolom ``abn_{k}``."""
    frames = [
        _panel([(d, 100.0, 100.0, 100.0) for d in sessions or _SESSIONS], code=code)
        for code in codes
    ]
    return pd.concat(frames, ignore_index=True)


def _claim(
    date: str,
    *,
    target: float = 110.0,
    stop: float = 95.0,
    horizon: int = 1,
    code: str = "AAA",
    direction: str = "BUY",
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": code,
                "date": date,
                "direction": direction,
                "stop": stop,
                "target": target,
                "horizon_days": horizon,
            }
        ]
    )


def test_stop_wins_when_both_touched_in_same_session():
    """Satu sesi menyentuh target DAN stop: yang dicatat stop (asumsi terburuk),
    karena urutan intra-sesi tidak diketahui dari data harian."""
    raw = _quiet_raw([("2026-01-07", 111.0, 94.0, 100.0)])

    out = claim_outcomes(_claim("2026-01-05", horizon=1), raw=raw, adj=_flat_adj(), horizons=(1,))

    assert out.loc[0, "outcome"] == "stop"
    assert bool(out.loc[0, "hit_stop"]) is True
    assert bool(out.loc[0, "hit_target"]) is True
    assert out.loc[0, "touch_date"] == "2026-01-07"


def test_target_hit_before_stop_is_target():
    raw = _quiet_raw([("2026-01-07", 111.0, 99.0, 105.0), ("2026-01-08", 105.0, 90.0, 95.0)])

    out = claim_outcomes(_claim("2026-01-05", horizon=2), raw=raw, adj=_flat_adj(), horizons=(2,))

    assert out.loc[0, "outcome"] == "target"
    assert out.loc[0, "touch_date"] == "2026-01-07"
    # stop kena SESUDAH target: klaim sudah ditutup di target, jadi tidak dihitung
    assert bool(out.loc[0, "hit_stop"]) is False


def test_stop_hit_before_target_is_stop():
    raw = _quiet_raw([("2026-01-07", 100.0, 90.0, 92.0), ("2026-01-08", 111.0, 92.0, 110.0)])

    out = claim_outcomes(_claim("2026-01-05", horizon=2), raw=raw, adj=_flat_adj(), horizons=(2,))

    assert out.loc[0, "outcome"] == "stop"
    assert out.loc[0, "touch_date"] == "2026-01-07"


def test_timeout_when_horizon_passes_without_touch():
    out = claim_outcomes(
        _claim("2026-01-05", horizon=2), raw=_quiet_raw(), adj=_flat_adj(), horizons=(2,)
    )

    assert out.loc[0, "outcome"] == "timeout"
    assert out.loc[0, "touch_date"] is None or pd.isna(out.loc[0, "touch_date"])
    assert bool(out.loc[0, "hit_stop"]) is False
    assert bool(out.loc[0, "hit_target"]) is False


def test_unresolved_when_horizon_not_reached_and_no_touch():
    """Klaim yang jendelanya baru sebagian berjalan belum punya hasil: unresolved,
    dan abnormalnya NaN — bukan nol yang ikut terhitung sebagai "tidak untung"."""
    out = claim_outcomes(
        _claim("2026-01-07", horizon=2), raw=_quiet_raw(), adj=_flat_adj(), horizons=(2,)
    )

    assert out.loc[0, "outcome"] == "unresolved"
    assert pd.isna(out.loc[0, "abn_2"])


def test_claim_date_missing_from_panel_is_unresolved():
    """Data bolong: klaim di tanggal yang tidak punya bar tidak bisa diukur.
    Harus unresolved — bukan diukur dari sesi lain yang kebetulan ada."""
    out = claim_outcomes(
        _claim("2026-01-04", horizon=1), raw=_quiet_raw(), adj=_flat_adj(), horizons=(1,)
    )

    assert out.loc[0, "outcome"] == "unresolved"
    assert pd.isna(out.loc[0, "abn_1"])


def test_missing_raw_session_does_not_shift_the_window():
    """Sesi harga mentah yang hilang tidak boleh membuat jendela bergeser.

    Kalau bar satu sesi tidak ada, hari itu tidak teramati: level bisa saja
    tersentuh di sana. Melewatinya diam-diam lalu menyimpulkan hasil dari sesi
    berikutnya berarti melaporkan angka dari hari yang bukan miliknya.
    """
    raw = _quiet_raw()
    raw = raw[raw["date"] != "2026-01-07"]

    out = claim_outcomes(_claim("2026-01-05", horizon=2), raw=raw, adj=_flat_adj(), horizons=(2,))

    assert out.loc[0, "outcome"] == "unresolved"


def test_touch_before_a_missing_session_still_resolves():
    """Kalau level sudah tersentuh SEBELUM sesi yang hilang, hasilnya tetap sah —
    kejadiannya sudah tercatat, yang tidak teramati datang belakangan."""
    raw = _quiet_raw([("2026-01-08", 111.0, 99.0, 110.0)])
    raw = raw[raw["date"] != "2026-01-09"]

    out = claim_outcomes(_claim("2026-01-05", horizon=3), raw=raw, adj=_flat_adj(), horizons=(3,))

    assert out.loc[0, "outcome"] == "target"
    assert out.loc[0, "touch_date"] == "2026-01-08"


def test_touch_after_a_missing_session_does_not_resolve():
    """Sentuhan yang datang SESUDAH sesi yang hilang belum bisa dipercaya.

    Stop bisa saja kena lebih dulu di hari yang tidak teramati, jadi melaporkan
    "target" untuk klaim ini berarti mengabaikan kemungkinan yang lebih buruk.
    """
    raw = _quiet_raw([("2026-01-08", 111.0, 99.0, 110.0)])
    raw = raw[raw["date"] != "2026-01-07"]

    out = claim_outcomes(_claim("2026-01-05", horizon=2), raw=raw, adj=_flat_adj(), horizons=(2,))

    assert out.loc[0, "outcome"] == "unresolved"


def test_entry_session_low_does_not_trigger_stop():
    """Level ditetapkan dari close sebelumnya: sesi entry tidak boleh memicunya.

    Entry di close T+1, jadi low sesi T+1 terjadi SEBELUM posisi ada."""
    raw = _quiet_raw([("2026-01-06", 101.0, 90.0, 100.0)])  # low 90 < stop 95

    out = claim_outcomes(_claim("2026-01-05", horizon=1), raw=raw, adj=_flat_adj(), horizons=(1,))

    assert bool(out.loc[0, "hit_stop"]) is False
    assert out.loc[0, "outcome"] == "timeout"


def test_abnormal_uses_adjusted_panel_while_levels_use_raw_panel():
    """Dua dasar harga, dua peran: level dari harga nyata, hasil dari harga terkoreksi."""
    raw = _panel(
        [
            ("2026-01-05", 101.0, 99.0, 100.0),
            ("2026-01-06", 101.0, 99.0, 100.0),  # entry
            ("2026-01-07", 101.0, 90.0, 95.0),  # low 90 -> stop kena
        ]
    )
    adj = pd.concat(
        [
            _panel(
                [
                    ("2026-01-05", 100.0, 100.0, 100.0),
                    ("2026-01-06", 100.0, 100.0, 100.0),
                    ("2026-01-07", 120.0, 120.0, 120.0),
                ],
                code="AAA",
            ),
            _panel(
                [
                    ("2026-01-05", 100.0, 100.0, 100.0),
                    ("2026-01-06", 100.0, 100.0, 100.0),
                    ("2026-01-07", 100.0, 100.0, 100.0),
                ],
                code="BBB",
            ),
        ],
        ignore_index=True,
    )

    out = claim_outcomes(_claim("2026-01-05", horizon=1), raw=raw, adj=adj, horizons=(1,))

    assert bool(out.loc[0, "hit_stop"]) is True
    assert out.loc[0, "abn_1"] > 0  # AAA +20% vs pasar +10% (dari panel terkoreksi)


def test_missing_code_in_panel_is_unresolved_not_zero():
    out = claim_outcomes(
        _claim("2026-01-05", horizon=1, code="ZZZ"),
        raw=_quiet_raw(),
        adj=_flat_adj(),
        horizons=(1,),
    )

    assert out.loc[0, "outcome"] == "unresolved"
    assert pd.isna(out.loc[0, "abn_1"])


def test_sell_direction_raises_value_error():
    """Menghitung stop/target untuk arah SELL dengan aturan BUY akan menghasilkan
    angka terbalik. Lebih baik menolak daripada diam-diam salah."""
    with pytest.raises(ValueError, match="SELL"):
        claim_outcomes(
            _claim("2026-01-05", horizon=1, direction="SELL"),
            raw=_quiet_raw(),
            adj=_flat_adj(),
            horizons=(1,),
        )


# --- agregat per sesi (spec §6.1, R7) ---


def _outcomes(rows: list[tuple[str, float, str]]) -> pd.DataFrame:
    """``rows`` = (tanggal, abn_21, outcome) — frame kecil seperti keluaran
    ``claim_outcomes``."""
    return pd.DataFrame(
        [
            {"code": f"C{i:03d}", "date": d, "abn_21": a, "outcome": o}
            for i, (d, a, o) in enumerate(rows)
        ]
    )


def test_three_claims_in_one_session_count_as_one_session():
    out = session_aggregate(
        _outcomes(
            [
                ("2026-01-05", 0.02, "target"),
                ("2026-01-05", -0.01, "stop"),
                ("2026-01-05", 0.01, "timeout"),
                ("2026-01-06", 0.03, "timeout"),
            ]
        ),
        horizon=21,
    )

    assert out["claims"] == 4
    assert out["measured_claims"] == 4
    assert out["sessions"] == 2
    assert out["hit_target"] == 1
    assert out["hit_stop"] == 1
    assert out["timeout"] == 2
    assert out["unresolved"] == 0


def test_mean_is_of_session_means_not_row_mean():
    """2 klaim +1% di satu sesi dan 1 klaim -3% di sesi lain: rata-ratanya -1,0%
    (rata-rata dari rata-rata sesi), BUKAN -0,33% (rata-rata seluruh baris)."""
    out = session_aggregate(
        _outcomes(
            [
                ("2026-01-05", 0.01, "timeout"),
                ("2026-01-05", 0.01, "timeout"),
                ("2026-01-06", -0.03, "stop"),
            ]
        ),
        horizon=21,
    )

    assert out["mean_abnormal"] == -0.01
    assert [s["n"] for s in out["per_session"]] == [2, 1]
    assert [s["mean_abnormal"] for s in out["per_session"]] == [0.01, -0.03]


def test_dispersion_none_for_single_session():
    out = session_aggregate(_outcomes([("2026-01-05", 0.01, "timeout")]), horizon=21)

    assert out["sessions"] == 1
    assert out["session_dispersion"] is None


def test_unresolved_excluded_from_abnormal_but_still_counted():
    out = session_aggregate(
        _outcomes(
            [
                ("2026-01-05", 0.02, "timeout"),
                ("2026-01-05", float("nan"), "unresolved"),
            ]
        ),
        horizon=21,
    )

    assert out["claims"] == 2
    assert out["measured_claims"] == 1
    assert out["unresolved"] == 1
    assert out["mean_abnormal"] == 0.02  # NaN tidak dianggap nol
    assert out["per_session"][0]["n"] == 1  # hanya yang terukur


def test_per_session_is_sorted_by_date():
    out = session_aggregate(
        _outcomes(
            [
                ("2026-01-07", 0.03, "timeout"),
                ("2026-01-05", 0.01, "timeout"),
                ("2026-01-06", 0.02, "timeout"),
            ]
        ),
        horizon=21,
    )

    assert [s["date"] for s in out["per_session"]] == ["2026-01-05", "2026-01-06", "2026-01-07"]


def test_empty_claims_is_safe():
    empty = pd.DataFrame(columns=["code", "date", "abn_21", "outcome"])

    out = session_aggregate(empty, horizon=21)

    assert out["claims"] == 0
    assert out["measured_claims"] == 0
    assert out["sessions"] == 0
    assert out["mean_abnormal"] is None
    assert out["median_session_abnormal"] is None
    assert out["session_dispersion"] is None
    assert out["per_session"] == []


# --- verdict & baris rekaman (spec §6.1, §8 Q4) ---


def _agg(
    claims: int = 0,
    sessions: int = 0,
    mean: float | None = None,
    target: int = 0,
    stop: int = 0,
    measured: int | None = None,
) -> dict:
    """Agregat seperti keluaran ``session_aggregate`` — cukup untuk menguji verdict.

    ``measured`` default-nya sama dengan ``claims`` (semua klaim punya angka);
    isi lebih kecil untuk menguji klaim yang belum matang.
    """
    return {
        "claims": claims,
        "measured_claims": claims if measured is None else measured,
        "sessions": sessions,
        "mean_abnormal": mean,
        "median_session_abnormal": mean,
        "session_dispersion": None,
        "hit_target": target,
        "hit_stop": stop,
        "timeout": claims - target - stop,
        "unresolved": 0,
        "per_session": [],
    }


def test_verdict_horizon_is_a_measured_horizon():
    """Verdict memakai horizon yang punya bukti OOS — bukan horizon karangan."""
    assert VERDICT_HORIZON == 21
    assert VERDICT_HORIZON in DEFAULT_HORIZONS


def test_verdict_not_ready_below_both_thresholds():
    verdict = claim_verdict(_agg(claims=29, sessions=29, mean=0.01))

    assert verdict["status"] == "belum cukup"
    assert "29" in verdict["reason"]


def test_verdict_ready_at_thirty_claims_even_in_few_sessions():
    """Ambangnya "30 klaim ATAU 6 minggu (±30 sesi), mana yang lebih dulu"."""
    verdict = claim_verdict(_agg(claims=30, sessions=8, mean=0.02))

    assert verdict["status"] == "terbukti"
    assert verdict["sessions"] == 8


def test_verdict_ready_at_thirty_sessions_even_with_few_claims():
    verdict = claim_verdict(_agg(claims=12, sessions=30, mean=0.02))

    assert verdict["status"] == "terbukti"


def test_verdict_not_proven_when_mean_not_above_zero():
    for mean in (-0.004, 0.0):
        verdict = claim_verdict(_agg(claims=30, sessions=30, mean=mean))
        assert verdict["status"] == "tidak terbukti", mean


def test_verdict_proven_when_mean_above_zero():
    verdict = claim_verdict(_agg(claims=30, sessions=30, mean=0.0001))

    assert verdict["status"] == "terbukti"


def test_verdict_not_ready_when_nothing_measured_yet():
    """30 klaim tercatat tapi belum satu pun punya hasil: belum bisa dinilai."""
    verdict = claim_verdict(_agg(claims=30, sessions=0, mean=None))

    assert verdict["status"] == "belum cukup"
    assert "terukur" in verdict["reason"]


def test_verdict_reason_states_which_threshold_was_reached():
    by_claims = claim_verdict(_agg(claims=30, sessions=8, mean=0.02))
    by_sessions = claim_verdict(_agg(claims=12, sessions=30, mean=0.02))

    assert "30 klaim terukur tercapai" in by_claims["reason"]
    assert "30 sesi tercapai" in by_sessions["reason"]


def test_verdict_not_ready_when_measurements_are_fewer_than_claims():
    """30 klaim yang tercatat tapi baru 4 yang punya angka: belum cukup.

    Ini lubang yang nyata: klaim baru matang setelah horizonnya berjalan, jadi
    dengan 3–5 klaim per sesi, ambang \"30 klaim\" akan tercapai sebelum satu sesi
    pun selesai — verdict akan berdiri di atas satu hari.
    """
    verdict = claim_verdict(_agg(claims=100, sessions=1, mean=0.02, measured=4))

    assert verdict["status"] == "belum cukup"
    assert verdict["measured_claims"] == 4
    assert "4 klaim terukur" in verdict["reason"]


def test_record_line_exact_copy():
    line = format_record_line(_agg(claims=12, sessions=6, mean=0.014, target=5, stop=4))

    assert line == (
        "Aturan ini: 12 klaim · 5 kena target · 4 kena batas rugi · rata-rata +1,4% vs pasar"
    )


def test_record_line_when_no_claims():
    assert format_record_line(_agg(claims=0)) == "Aturan ini: belum punya rekaman"


def test_record_line_negative_average_has_no_plus():
    line = format_record_line(_agg(claims=6, sessions=6, mean=-0.008, target=1, stop=3))

    assert "rata-rata -0,8% vs pasar" in line
    assert "+-" not in line


def test_record_line_when_nothing_measured_yet():
    line = format_record_line(_agg(claims=4, sessions=0, mean=None, target=0, stop=0))

    assert "4 klaim" in line
    assert "belum terukur" in line
