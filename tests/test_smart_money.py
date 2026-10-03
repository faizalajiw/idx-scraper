"""Unit tests: jejak smart money — verdict, pola, level, track record, narasi.

Semua pure (tanpa DB) — pola yang sama dengan ``test_foreign_flow.py``.
Angka uji memakai skala yang sama dengan kalibrasi data: net asing diukur
sebagai porsi nilai transaksi (p50|.| ≈ 2%, ambang bermakna 5%).
"""

from __future__ import annotations

from idx_scraper.smart_money import (
    MIN_HISTORY_N,
    build_alert_message,
    build_narrative,
    collect_pattern_episodes,
    consolidation_range,
    detect_patterns,
    market_breadth,
    market_context_sentence,
    pattern_evidence_sentence,
    pattern_history,
    pattern_track_record,
    patterns_history,
    rank_patterns,
    size_label,
    verdict,
)


def _row(
    date: str,
    net,
    value=1000.0,
    close=100.0,
    high=None,
    low=None,
    volume=1_000_000.0,
) -> dict:
    return {
        "date": date,
        "net_idr": net,
        "value": value,
        "close": close,
        "high": high if high is not None else close * 1.005,
        "low": low if low is not None else close * 0.995,
        "volume": volume,
    }


def _dates(n: int, start: str = "2026-09-01") -> list[str]:
    from datetime import date, timedelta

    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _flat_rows(n: int, net: float | None = None, value: float = 1000.0) -> list[dict]:
    """Deret datar (range 1%) dengan net konstan — dasar utk uji pola."""
    return [_row(d, net, value=value) for d in _dates(n)]


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #


def test_verdict_accumulation_menengah():
    # net 100/hari dari value 1000/hari selama 10 sesi -> netval 10% ->
    # akumulasi menengah (>=5, <16).
    rows = _flat_rows(10, net=100.0)
    v = verdict(rows)
    assert v["side"] == "akumulasi"
    assert v["strength"] == "menengah"
    assert v["netval_pct"] == 10.0
    assert v["insufficient"] is False
    assert v["date"] == _dates(10)[-1]


def test_verdict_accumulation_besar():
    rows = _flat_rows(10, net=200.0)  # netval 20% -> besar (>=16)
    v = verdict(rows)
    assert v["side"] == "akumulasi"
    assert v["strength"] == "besar"


def test_verdict_distribution():
    rows = _flat_rows(10, net=-150.0)  # netval -15% -> distribusi menengah
    v = verdict(rows)
    assert v["side"] == "distribusi"
    assert v["strength"] == "menengah"
    assert v["net_sum_idr"] == -1500.0


def test_verdict_neutral_small_flow():
    rows = _flat_rows(10, net=10.0)  # netval 1% -> netral
    v = verdict(rows)
    assert v["side"] == "netral"
    assert v["strength"] is None


def test_verdict_insufficient_when_thin():
    # Hanya 4 hari ber-net dari 10 -> tidak boleh verdict (anti data tipis),
    # walau angka agregatnya "boleh-boleh saja".
    rows = [
        _row(d, None) for d in _dates(6)
    ] + [
        _row(d, 500.0) for d in _dates(4, start="2026-09-07")
    ]
    v = verdict(rows)
    assert v["insufficient"] is True
    assert v["side"] == "netral"
    assert v["strength"] is None
    assert v["netval_pct"] == 20.0  # angka ada, tapi tak boleh dipakai


def test_verdict_empty_rows_safe():
    v = verdict([])
    assert v["insufficient"] is True
    assert v["date"] is None
    assert v["streak"] == 0


def test_verdict_streak_counts_consecutive_same_side():
    # 3 negatif, lalu 4 positif terakhir -> streak 4 net_buy.
    rows = [_row(d, -100.0) for d in _dates(3)] + [
        _row(d, 100.0) for d in _dates(4, start="2026-09-04")
    ]
    v = verdict(rows)
    assert v["streak"] == 4
    assert v["streak_side"] == "net_buy"


def test_verdict_streak_reset_by_zero_day():
    rows = (
        [_row(d, 100.0) for d in _dates(3)]
        + [_row("2026-09-04", 0.0)]
        + [_row(d, 100.0) for d in _dates(2, start="2026-09-05")]
    )
    v = verdict(rows)
    assert v["streak"] == 2


def test_verdict_ignores_nan_and_unsorted():
    rows = [
        _row("2026-09-03", 100.0),
        _row("2026-09-01", float("nan")),  # nan -> treated None
        _row("2026-09-02", 100.0),
        _row("2026-09-04", 100.0),
        _row("2026-09-05", 100.0),
        _row("2026-09-06", 100.0),
        _row("2026-09-07", 100.0),
    ]
    v = verdict(rows)
    # 6 hari ber-net, netval = 6*100 / 7*1000 ≈ 8.57% -> akumulasi.
    assert v["insufficient"] is False
    assert v["side"] == "akumulasi"
    assert v["n_net_days"] == 6


def test_verdict_window_only_uses_last_n_days():
    # 12 sesi: 2 pertama net BUY besar, 10 terakhir net SELL bermakna.
    # Verdict harus mengikuti 10 sesi terakhir (distribusi), bukan sejarah.
    rows = [
        _row(d, 500.0) for d in _dates(2)
    ] + [
        _row(d, -100.0) for d in _dates(10, start="2026-09-03")
    ]
    v = verdict(rows)
    assert v["side"] == "distribusi"


# --------------------------------------------------------------------------- #
# size_label
# --------------------------------------------------------------------------- #


def test_size_label_bands():
    assert size_label(20.0) == "besar"
    assert size_label(-16.0) == "besar"
    assert size_label(6.0) == "bermakna"
    assert size_label(-5.0) == "bermakna"
    assert size_label(2.0) is None
    assert size_label(None) is None


# --------------------------------------------------------------------------- #
# detect_patterns
# --------------------------------------------------------------------------- #


def _pids(patterns: list[dict]) -> list[str]:
    return [p["id"] for p in patterns]


def test_pattern_silent_accumulation():
    # Datar (range ~1%) + net buy 10% dari nilai -> akumulasi diam-diam.
    rows = _flat_rows(12, net=100.0)
    pats = detect_patterns(rows)
    assert "silent_accumulation" in _pids(pats)


def test_pattern_silent_distribution():
    rows = _flat_rows(12, net=-100.0)
    pats = detect_patterns(rows)
    assert "silent_distribution" in _pids(pats)
    assert "silent_accumulation" not in _pids(pats)


def test_pattern_distribution_on_rally():
    # Naik jelas: +1,5%/sesi -> +14% dalam 10 sesi (> 8%), range ~13%
    # (> 10% -> bukan "datar"), tapi net sell bermakna.
    chg = 0.015
    rows = [
        _row(
            _dates(12)[i],
            net=-100.0,
            close=100.0 * (1 + chg) ** i,
            high=100.0 * (1 + chg) ** i * 1.002,
            low=100.0 * (1 + chg) ** i * 0.998,
        )
        for i in range(12)
    ]
    pats = detect_patterns(rows)
    assert "distribution_on_rally" in _pids(pats)
    assert "silent_distribution" not in _pids(pats)  # harga tak datar


def test_pattern_rally_with_foreign_buy_is_not_distribution():
    chg = 0.015
    rows = [
        _row(
            _dates(12)[i],
            net=100.0,
            close=100.0 * (1 + chg) ** i,
            high=100.0 * (1 + chg) ** i * 1.002,
            low=100.0 * (1 + chg) ** i * 0.998,
        )
        for i in range(12)
    ]
    pats = detect_patterns(rows)
    assert "distribution_on_rally" not in _pids(pats)
    # Tidak ada pola lain pun: tidak datar (bukan silent_*), volume normal
    # (bukan inisiasi).
    assert _pids(pats) == []


def test_pattern_initiation_needs_volume_spike():
    rows = _flat_rows(11, net=100.0)  # volume normal -> tanpa inisiasi
    assert "initiation" not in _pids(detect_patterns(rows))

    rows[-1] = _row(_dates(12)[-1], net=100.0, volume=5_000_000.0)  # 5x volume
    assert "initiation" in _pids(detect_patterns(rows))


def test_pattern_initiation_blocked_on_panic_dump():
    # Volume melonjak + net buy, TAPI harga jeblok > 3% di 10 sesi -> bukan
    # inisiasi (itu bukan volume pendorong).
    rows = _flat_rows(12, net=100.0)
    # Turunkan close 4 hari terakhir ~5% (range tetap < 10%).
    for i in range(8, 12):
        rows[i] = _row(_dates(12)[i], net=100.0, close=95.0, low=94.5, high=95.5)
    rows[-1] = _row(_dates(12)[-1], net=100.0, close=95.0, low=94.5, high=95.5,
                    volume=5_000_000.0)
    assert "initiation" not in _pids(detect_patterns(rows))


def test_pattern_short_history_returns_empty():
    rows = _flat_rows(5, net=100.0)  # < 11 baris
    assert detect_patterns(rows) == []


def test_patterns_empty_and_neutral_flow():
    # Net kecil -> tidak ada pola (semua butuh netval bermakna).
    rows = _flat_rows(12, net=10.0)
    assert detect_patterns(rows) == []


def test_pattern_note_mentions_range_number():
    rows = _flat_rows(12, net=-100.0)
    pats = detect_patterns(rows)
    note = next(p["note"] for p in pats if p["id"] == "silent_distribution")
    assert "%" in note  # menyebut angka range yang diukur


# --------------------------------------------------------------------------- #
# consolidation_range
# --------------------------------------------------------------------------- #


def test_consolidation_range_bounds():
    # 25 baris; window 20 sesi terakhir = i 5..24.
    # high = 101 + (i%3) -> max i%3 di [5..24] = 2 -> 103.0
    # low  = 98 - (i%2)  -> max i%2 di [5..24] = 1 -> 97.0
    rows = [
        _row(d, 0.0, close=100.0, high=101.0 + (i % 3), low=98.0 - (i % 2))
        for i, d in enumerate(_dates(25))
    ]
    rng = consolidation_range(rows)
    assert rng is not None
    assert rng["lookback"] == 20
    assert rng["low"] == 97.0
    assert rng["high"] == 103.0
    assert rng["support"] == rng["low"]
    assert rng["resistance"] == rng["high"]
    assert rng["range_pct"] > 0


def test_consolidation_range_too_short_returns_none():
    rows = [
        _row(d, 0.0, close=100.0, high=101.0, low=99.0) for d in _dates(4)
    ]
    assert consolidation_range(rows) is None


def test_consolidation_range_ignores_rows_without_price():
    # high/low benar-benar NULL (bukan auto-fill helper) -> dilewati.
    from idx_scraper.smart_money import _clean_rows

    rows = [
        {"date": d, "net_idr": 0.0, "value": 1000.0, "close": 100.0,
         "high": None, "low": None, "volume": 1e6}
        for d in _dates(6)
    ] + [
        _row(d, 0.0, close=100.0, high=105.0, low=95.0)
        for d in _dates(5, "2026-09-07")
    ]
    rng = consolidation_range(rows)
    assert rng is not None
    assert rng["lookback"] == 5
    # helper _clean_rows tidak membuang baris (keputusan ada di consolidation).
    assert len(_clean_rows(rows)) == 11


# --------------------------------------------------------------------------- #
# pattern_track_record
# --------------------------------------------------------------------------- #


def test_track_record_hit_rate_and_stats():
    occ = [
        # 5 kejadian silent_accumulation (n>=5 supaya tstat terhitung —
        # konvensi sama dengan events._nw_tstat); fwd5: 4 naik 1 turun.
        {"code": "A", "date": "2026-01-01", "pattern": "silent_accumulation",
         "fwd5": 2.0, "fwd10": None, "fwd21": None},
        {"code": "B", "date": "2026-01-05", "pattern": "silent_accumulation",
         "fwd5": 5.0, "fwd10": 1.0, "fwd21": None},
        {"code": "C", "date": "2026-01-08", "pattern": "silent_accumulation",
         "fwd5": -1.0, "fwd10": -2.0, "fwd21": 0.5},
        {"code": "D", "date": "2026-01-12", "pattern": "silent_accumulation",
         "fwd5": 3.0, "fwd10": None, "fwd21": None},
        {"code": "F", "date": "2026-01-16", "pattern": "silent_accumulation",
         "fwd5": 1.0, "fwd10": 0.5, "fwd21": -0.5},
        # Pola lain -> tidak tercampur.
        {"code": "E", "date": "2026-01-15", "pattern": "initiation",
         "fwd5": 10.0, "fwd10": None, "fwd21": None},
    ]
    tr = pattern_track_record(occ)
    sa = next(t for t in tr if t["pattern"] == "silent_accumulation")
    assert sa["n"] == 5
    assert sa["n_resolved"] == 5
    h5 = sa["horizons"]["fwd5"]
    assert h5["n"] == 5
    assert abs(h5["hit_rate"] - 0.8) < 1e-9
    assert abs(h5["mean"] - (2.0 + 5.0 - 1.0 + 3.0 + 1.0) / 5) < 1e-9
    assert h5["median"] == 2.0
    assert h5["tstat"] is not None
    # resolusi fwd10 hanya 3 dari 5.
    assert sa["horizons"]["fwd10"]["n"] == 3
    ini = next(t for t in tr if t["pattern"] == "initiation")
    assert ini["n"] == 1
    assert ini["horizons"]["fwd5"]["hit_rate"] == 1.0
    assert ini["horizons"]["fwd5"]["tstat"] is None  # n=1 < 5


def test_track_record_empty_and_unknown_pattern():
    assert pattern_track_record([]) == []
    tr = pattern_track_record([
        {"pattern": "pola_gaib", "fwd5": 1.0, "fwd10": None, "fwd21": None},
    ])
    assert tr == []


def test_track_record_none_horizons_are_safe():
    tr = pattern_track_record([
        {"pattern": "initiation", "fwd5": None, "fwd10": None, "fwd21": None},
    ])
    assert len(tr) == 1
    assert tr[0]["horizons"]["fwd5"]["n"] == 0
    assert tr[0]["horizons"]["fwd5"]["hit_rate"] is None


def test_track_record_direction_aware_interpretation():
    # Pola buy-side: aligned = hit_rate (pola "berfungsi" bila harga naik).
    # Pola sell-side: aligned = 1 - hit_rate (pola "berfungsi" bila harga turun),
    # dan effective_alpha dibalik tanda.
    buy = [
        {"code": "A", "date": "d1", "pattern": "silent_accumulation",
         "fwd5": 2.0, "abn5": 1.0},
        {"code": "B", "date": "d2", "pattern": "silent_accumulation",
         "fwd5": -1.0, "abn5": -0.5},
    ]
    sell = [
        # Harga TURUN setelah distribusi -> pola sell-side "berfungsi".
        {"code": "C", "date": "d1", "pattern": "silent_distribution",
         "fwd5": -2.0, "abn5": -1.0},
        {"code": "D", "date": "d2", "pattern": "silent_distribution",
         "fwd5": -1.0, "abn5": -0.5},
    ]
    tr = pattern_track_record(buy + sell)
    b = next(t for t in tr if t["pattern"] == "silent_accumulation")
    s = next(t for t in tr if t["pattern"] == "silent_distribution")
    # buy-side: hit 1/2, aligned sama, alpha rata-rata (1.0 + -0.5)/2 = 0.25
    assert b["direction"] == "buy-side"
    assert b["horizons"]["fwd5"]["hit_rate"] == 0.5
    assert b["horizons"]["fwd5"]["aligned_hit_rate"] == 0.5
    assert abs(b["horizons"]["fwd5"]["alpha"] - 0.25) < 1e-9
    assert abs(b["horizons"]["fwd5"]["effective_alpha"] - 0.25) < 1e-9
    # sell-side: hit (fwd>0) = 0/2 = 0, TAPI aligned = 1 - 0 = 1.0 (keduanya turun)
    # dan effective_alpha = -(mean abn) = -((-1.0 + -0.5)/2) = +0.75
    assert s["direction"] == "sell-side"
    assert s["horizons"]["fwd5"]["hit_rate"] == 0.0
    assert s["horizons"]["fwd5"]["aligned_hit_rate"] == 1.0
    assert abs(s["horizons"]["fwd5"]["alpha"] - (-0.75)) < 1e-9
    assert abs(s["horizons"]["fwd5"]["effective_alpha"] - 0.75) < 1e-9


def test_track_record_alpha_none_when_absent():
    # Tanpa field abn -> alpha & effective_alpha None, tak error.
    tr = pattern_track_record([
        {"pattern": "initiation", "fwd5": 1.0, "fwd10": None, "fwd21": None},
    ])
    h5 = tr[0]["horizons"]["fwd5"]
    assert h5["alpha"] is None
    assert h5["effective_alpha"] is None


# --------------------------------------------------------------------------- #
# build_narrative
# --------------------------------------------------------------------------- #


def test_narrative_insufficient_is_honest():
    v = verdict([])
    out = build_narrative("GOTO", v, [], None)
    assert len(out) == 1
    assert "belum cukup" in out[0]


def test_narrative_accumulation_includes_money_and_size():
    # Realistis: nilai 100 M/sesi, net asing +20 M/sesi -> netval 20% (besar).
    rows = _flat_rows(10, net=2.0e10, value=1.0e11)
    v = verdict(rows)
    pats = detect_patterns(_flat_rows(12, net=2.0e10, value=1.0e11))
    rng = consolidation_range(_flat_rows(25, net=2.0e10, value=1.0e11))
    out = build_narrative("BBCA", v, pats, rng)
    joined = " ".join(out)
    assert "ditimbun" in joined
    assert "Rp 200.0 M" in joined  # 10 sesi x 20 M
    assert "besar" in joined
    assert "berturut" in joined  # streak 10 >= 3
    assert "rentang" in joined  # kalimat level ada


def test_narrative_distribution_money_is_positive_rupiah():
    v = verdict(_flat_rows(10, net=-2.0e10, value=1.0e11))
    out = build_narrative("BMRI", v, [], None)
    joined = " ".join(out)
    assert "dibuang" in joined
    assert "Rp 200.0 M" in joined
    # nilai yang ditampilkan tidak boleh negatif
    assert "-Rp" not in joined
    # streak tidak disebut karena < 3? streak = 10 -> disebut.
    assert "berturut" in joined


def test_narrative_neutral_mentions_seimbang():
    v = verdict(_flat_rows(10, net=1.0e9, value=1.0e11))  # netval 1% -> netral
    out = build_narrative("TLKM", v, [], None)
    joined = " ".join(out)
    assert "seimbang" in joined
    assert "berturut" not in joined  # streak tidak relevan utk netral


# --------------------------------------------------------------------------- #
# build_radar
# --------------------------------------------------------------------------- #


def test_radar_ranks_by_absolute_rupiah_not_percent():
    # BIG: net 5% dari Rp 1 T/sesi -> net_sum 5e11 (uang jauh lebih besar)
    # SMLA: net 20% dari Rp 6 M/sesi -> net_sum 1.2e10
    big = {
        "code": "BIG",
        "name": "Emiten Besar",
        "rows": _flat_rows(10, net=5.0e10, value=1.0e12),
    }
    small = {
        "code": "SMLA",
        "name": "Emiten Kecil",
        "rows": _flat_rows(10, net=1.2e9, value=6.0e9),
    }
    from idx_scraper.smart_money import build_radar

    radar = build_radar({"BIG": big, "SMLA": small})
    assert [r["code"] for r in radar] == ["BIG", "SMLA"]
    assert radar[0]["side"] == "akumulasi"
    assert radar[1]["netval_pct"] == 20.0


def test_radar_excludes_neutral_insufficient_and_illiquid():
    from idx_scraper.smart_money import build_radar

    neutral = {
        "code": "NEUT",
        "rows": _flat_rows(10, net=1.0e8, value=1.0e11),  # netval 0,1%
    }
    thin = {  # 2 hari bernilai dari 10 -> insufficient utk jendela 10
        "code": "THIN",
        "rows": [
            _row(d, None) for d in _dates(8)
        ] + [
            _row(d, 1.0e11, value=1.0e11) for d in _dates(2, start="2026-09-09")
        ],
    }
    illiquid = {  # netval 7,5% (akumulasi) tapi nilai transaksi cuma 20 M/hari
        "code": "ILLQ",
        "rows": _flat_rows(10, net=1.5e6, value=2.0e7),  # total 200 M < 500 M
    }
    good = {
        "code": "GOOD",
        "rows": _flat_rows(10, net=1.0e10, value=1.0e11),
    }
    radar = build_radar(
        {"NEUT": neutral, "THIN": thin, "ILLQ": illiquid, "GOOD": good}
    )
    assert [r["code"] for r in radar] == ["GOOD"]


def test_radar_sides_and_streak_present():
    from idx_scraper.smart_money import build_radar

    radar = build_radar(
        {
            "IN": {"rows": _flat_rows(10, net=1.0e10, value=1.0e11)},
            "OUT": {"rows": _flat_rows(10, net=-1.0e10, value=1.0e11)},
        }
    )
    by_code = {r["code"]: r for r in radar}
    assert by_code["IN"]["side"] == "akumulasi"
    assert by_code["OUT"]["side"] == "distribusi"
    assert by_code["IN"]["streak"] == 10
    assert by_code["OUT"]["streak"] == 10
    assert by_code["IN"]["window_value"] == 1.0e12


def test_radar_short_window_respects_days():
    from idx_scraper.smart_money import build_radar

    # Jendela 2 hari: emiten dengan 2 hari bernilai cukup (min(5, 2) = 2).
    rows2 = _flat_rows(2, net=1.0e10, value=1.0e11)
    radar = build_radar({"TWO": {"rows": rows2}}, days=2)
    assert len(radar) == 1
    assert radar[0]["side"] == "akumulasi"


# --------------------------------------------------------------------------- #
# track record — pengumpul episode
# --------------------------------------------------------------------------- #


def test_episodes_empty_panel_and_short_history():
    # Panel kosong -> tak ada kejadian.
    assert collect_pattern_episodes({}) == []
    # < 11 baris tidak cukup untuk ``_pattern_conditions`` -> tak ada kejadian.
    assert collect_pattern_episodes({"X": {"rows": _flat_rows(10, net=100.0)}}) == []


def test_episodes_detects_silent_accumulation_and_dedupes():
    # Datar + net buy bermakna (netval10 10%) di seluruh deret -> pola aktif
    # di tiap T yang punya history. T yang lolos pertama = index 10; T 11-14
    # masih satu episode (jarak < 10) -> ditolak dedup.
    rows = _flat_rows(15, net=100.0, value=1000.0)
    panel = {"ACC": {"name": "Acc", "rows": rows}}
    eps = collect_pattern_episodes(panel)
    assert eps, "diharapkan minimal satu episode"
    assert all(e["pattern"] == "silent_accumulation" for e in eps)
    assert all(e["code"] == "ACC" for e in eps)
    # 15 baris -> hanya 1 episode (T=10; T=11..14 tumpang-tindih).
    assert len(eps) == 1
    assert eps[0]["date"] == rows[10]["date"]


def test_episodes_two_non_overlapping_windows_are_two_episodes():
    # 25 sesi akumulasi berkelanjutan: jendela T=10 (hari 1-10) dan T=20
    # (hari 11-20) tidak tumpang-tindih -> 2 episode, bukan 1.
    rows = _flat_rows(25, net=100.0, value=1000.0)
    eps = collect_pattern_episodes({"ACC": {"rows": rows}})
    assert [e["date"] for e in eps] == [rows[10]["date"], rows[20]["date"]]


def test_episodes_detects_silent_distribution():
    # Datar + net sell bermakna -> silent_distribution.
    rows = _flat_rows(20, net=-100.0, value=1000.0)
    eps = collect_pattern_episodes({"DIST": {"rows": rows}})
    assert eps and eps[0]["pattern"] == "silent_distribution"
    assert len(eps) == 1


def test_episodes_no_signal_when_flat_net_neutral():
    # Datar tapi net ~0 (netval jauh di bawah ambang 5%) -> tanpa pola.
    rows = _flat_rows(25, net=1.0, value=1000.0)  # netval ~0.1%
    assert collect_pattern_episodes({"NEU": {"rows": rows}}) == []


def test_episodes_distribution_on_rally():
    # Naik > 8% dalam 10 sesi + net sell bermakna -> distribusi saat naik.
    rows = []
    base = 100.0
    for i, d in enumerate(_dates(25)):
        # tumbuh +1.5% per sesi -> +16.5% dalam 10 sesi (>= 8%)
        close = base * (1.015 ** i)
        rows.append(_row(d, net=-100.0, value=1000.0, close=close))
    eps = collect_pattern_episodes({"RLY": {"rows": rows}})
    pats = {e["pattern"] for e in eps}
    assert "distribution_on_rally" in pats, f"dapat {pats}"


# --------------------------------------------------------------------------- #
# build_alert_message
# --------------------------------------------------------------------------- #


def test_alert_message_new_accumulation():
    v = verdict(_flat_rows(10, net=100.0, value=1000.0))  # akumulasi
    msg = build_alert_message("BBCA", "Bank Central Asia", v, [], None, None)
    assert "BBCA" in msg and "Bank Central Asia" in msg
    assert "mulai ditimbun" in msg
    assert "net buy" in msg


def test_alert_message_new_distribution():
    v = verdict(_flat_rows(10, net=-100.0, value=1000.0))  # distribusi
    msg = build_alert_message("BMRI", None, v, [], None, None)
    assert "mulai dibuang" in msg
    assert "net sell" in msg
    # tanpa name -> tidak ada label kosong aneh
    assert "BMRI" in msg


def test_alert_message_reversal():
    # Sekarang distribusi, sebelumnya akumulasi -> "berbalik".
    v = verdict(_flat_rows(10, net=-100.0, value=1000.0))
    msg = build_alert_message("GOTO", None, v, [], None, previous_side="akumulasi")
    assert "berbalik" in msg
    assert "dibuang" in msg


def test_alert_message_stable_side_uses_sedang():
    # Sisi sama dengan sebelumnya -> "sedang" (bukan "mulai"/"berbalik").
    v = verdict(_flat_rows(10, net=100.0, value=1000.0))
    msg = build_alert_message("BBCA", None, v, [], None, previous_side="akumulasi")
    assert "sedang ditimbun" in msg
    assert "mulai" not in msg and "berbalik" not in msg


def test_alert_message_includes_streak_and_pattern_note():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    v = verdict(rows)
    patterns = detect_patterns(rows)  # silent_accumulation (butuh >= 11 baris)
    assert patterns, "diharapkan ada pola"
    msg = build_alert_message("BBCA", None, v, patterns, None, None)
    # streak >= 3 (semua net buy) -> disebut
    assert "berturut-turut" in msg
    # catatan pola pertama ikut
    assert any(p["note"] and p["note"] in msg for p in patterns)


def test_alert_message_insufficient_is_honest():
    v = verdict(_flat_rows(2, net=100.0, value=1000.0))  # kurang data
    assert v["insufficient"] is True
    msg = build_alert_message("NEW", None, v, [], None, None)
    assert "belum cukup" in msg


# --------------------------------------------------------------------------- #
# sejak kapan (streak start date)
# --------------------------------------------------------------------------- #


def test_verdict_streak_start_date_is_first_session_of_streak():
    rows = _flat_rows(20, net=100.0, value=1000.0)  # semua net buy
    v = verdict(rows)
    assert v["streak"] == 20
    assert v["streak_start_date"] == "2026-09-01"


def test_verdict_streak_start_date_is_none_without_streak():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    rows[-1] = _row(rows[-1]["date"], -100.0)  # hari terakhir membalik arah
    v = verdict(rows)
    assert v["streak"] == 1
    assert v["streak_start_date"] == "2026-09-20"


def test_verdict_streak_start_date_none_when_flat_only():
    v = verdict(_flat_rows(20, net=None, value=1000.0))
    assert v["streak"] == 0
    assert v["streak_start_date"] is None


# --------------------------------------------------------------------------- #
# arti historis pola (track record -> horizon + angka)
# --------------------------------------------------------------------------- #


def _track_row(pid: str, **horizons) -> dict:
    """Entri tiruan keluaran ``pattern_track_record``.

    Nilai per horizon: ``(n, aligned_hit_rate[, tstat[, effective_alpha]])``.
    """
    clean = {}
    for key, val in horizons.items():
        clean[key] = {
            "n": val[0],
            "hit_rate": None,
            "aligned_hit_rate": val[1],
            "mean": None,
            "median": None,
            "tstat": val[2] if len(val) > 2 else 2.0,
            "alpha": None,
            "effective_alpha": val[3] if len(val) > 3 else 1.0,
        }
    return {
        "pattern": pid,
        "label": "Distribusi diam-diam",
        "direction": "sell-side",
        "n": 553,
        "n_resolved": 484,
        "horizons": clean,
    }


def test_pattern_history_prefers_the_horizon_with_best_evidence():
    # Distribusi: kuat di 5 hari (63,6%), melemah di 21 hari (51,1%) ->
    # horizon terpilih harus 5 hari, bukan yang terpanjang.
    row = _track_row(
        "silent_distribution",
        fwd5=(484, 0.636),
        fwd10=(419, 0.580),
        fwd21=(284, 0.511),
    )
    h = pattern_history(row, window_sessions=120)
    assert h is not None
    assert h["horizon"] == "fwd5"
    assert h["horizon_days"] == 5
    assert h["aligned_hit_rate"] == 0.636
    assert h["n_resolved_horizon"] == 484
    assert h["reliable"] is True
    assert h["window_sessions"] == 120


def test_pattern_history_flags_small_sample_as_unreliable():
    row = _track_row("initiation", fwd5=(8, 0.9), fwd10=(12, 0.8))
    h = pattern_history(row)
    assert h is not None
    assert h["reliable"] is False
    # horizon dengan n terbesar yang dipakai saat tak ada yang lolos ambang
    assert h["horizon"] == "fwd10"
    assert h["n_resolved_horizon"] == 12


def test_pattern_history_ignores_horizons_without_rate():
    row = _track_row("initiation", fwd5=(500, None), fwd10=(40, 0.5))
    h = pattern_history(row)
    assert h is not None
    assert h["horizon"] == "fwd10"


def test_pattern_history_none_when_no_data():
    assert pattern_history(None) is None
    assert pattern_history({}) is None
    assert pattern_history({"horizons": {}}) is None
    assert pattern_history({"horizons": {"fwd5": {"n": 10, "aligned_hit_rate": None}}}) is None


def test_patterns_history_only_covers_detected_patterns():
    pats = [{"id": "silent_accumulation"}, {"id": "tak_dikenal"}]
    tr = [_track_row("silent_accumulation", fwd21=(213, 0.554))]
    out = patterns_history(pats, tr, window_sessions=120)
    assert set(out) == {"silent_accumulation"}
    assert out["silent_accumulation"]["horizon_days"] == 21


def test_pattern_evidence_sentence_names_horizon_and_sample():
    h = pattern_history(_track_row("silent_distribution", fwd5=(484, 0.636)), window_sessions=120)
    s = pattern_evidence_sentence("Distribusi diam-diam", h, "sell-side")
    assert s is not None
    assert "64%" in s
    assert "484" in s
    assert "5 hari bursa" in s
    assert "turun" in s
    assert "120 sesi terakhir" in s


def test_pattern_evidence_sentence_direction_comes_from_pattern_not_rate():
    # Regresi: 64% pada pola JUAL berarti harga TURUN. Menyimpulkan arah dari
    # besarnya angka akan membalik artinya jadi "harga naik".
    sell = pattern_history(_track_row("silent_distribution", fwd5=(484, 0.636)))
    s_sell = pattern_evidence_sentence("Distribusi diam-diam", sell, "sell-side")
    assert s_sell is not None and "harga turun" in s_sell and "harga naik" not in s_sell

    # Sebaliknya, pola beli yang rekornya buruk (40,7%) tetap "harga naik" —
    # angkanya yang rendah, bukan arahnya yang dibalik.
    buy = pattern_history(_track_row("silent_accumulation", fwd5=(329, 0.407)))
    s_buy = pattern_evidence_sentence("Akumulasi diam-diam", buy, "buy-side")
    assert s_buy is not None and "harga naik" in s_buy and "41%" in s_buy


def test_pattern_evidence_sentence_neutral_when_direction_unknown():
    h = pattern_history(_track_row("initiation", fwd5=(100, 0.6)))
    s = pattern_evidence_sentence("Inisiasi volume + asing", h)
    assert s is not None
    assert "sesuai arah polanya" in s
    assert "harga naik" not in s and "harga turun" not in s


def test_pattern_evidence_sentence_marks_small_sample():
    h = pattern_history(_track_row("initiation", fwd5=(9, 0.9)))
    s = pattern_evidence_sentence("Inisiasi volume + asing", h, "buy-side")
    assert s is not None
    assert "indikatif" in s


def test_pattern_evidence_sentence_none_without_history():
    assert pattern_evidence_sentence("Akumulasi diam-diam", None) is None
    assert pattern_evidence_sentence("Akumulasi diam-diam", {}) is None


def test_build_narrative_includes_since_date_and_evidence():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    v = verdict(rows)
    pats = detect_patterns(rows)  # silent_accumulation
    assert pats and pats[0]["id"] == "silent_accumulation"
    hist = patterns_history(
        pats,
        [_track_row("silent_accumulation", fwd5=(329, 0.407), fwd21=(213, 0.554))],
        window_sessions=120,
    )
    out = build_narrative("BBCA", v, pats, None, pattern_history=hist)
    joined = " ".join(out)
    assert "sejak 1 Sep 2026" in joined
    assert "21 hari bursa" in joined  # horizon terbaik = 21 hari, bukan 5
    assert "harga naik" in joined  # pola akumulasi = sisi beli


def test_build_narrative_without_history_still_works():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    v = verdict(rows)
    pats = detect_patterns(rows)
    out = build_narrative("BBCA", v, pats, None)
    assert out and "sejak 1 Sep 2026" in " ".join(out)


def test_min_history_n_threshold_is_documented_constant():
    assert MIN_HISTORY_N == 30


# --------------------------------------------------------------------------- #
# urutan tampil: kekuatan bukti, bukan urutan deteksi
# --------------------------------------------------------------------------- #


def test_rank_patterns_puts_evidenced_first_then_confidence_then_effect():
    pats = [{"id": "no_history"}, {"id": "weak"}, {"id": "strong"}]
    hist = {
        "weak": {"confidence": "sedang", "edge_pct": 0.4},
        "strong": {"confidence": "tinggi", "edge_pct": 2.6},
    }
    assert [p["id"] for p in rank_patterns(pats, hist)] == ["strong", "weak", "no_history"]


def test_rank_patterns_breaks_ties_by_effect_size():
    pats = [{"id": "small"}, {"id": "big"}]
    hist = {
        "small": {"confidence": "tinggi", "edge_pct": 0.5},
        "big": {"confidence": "tinggi", "edge_pct": 3.0},
    }
    assert [p["id"] for p in rank_patterns(pats, hist)] == ["big", "small"]


def test_rank_patterns_is_stable_without_history():
    pats = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert [p["id"] for p in rank_patterns(pats, {})] == ["a", "b", "c"]
    assert rank_patterns([], {"x": {"confidence": "tinggi"}}) == []


# --------------------------------------------------------------------------- #
# keyakinan (|t|) & besar efek
# --------------------------------------------------------------------------- #


def test_pattern_history_confidence_and_edge_pct():
    row = _track_row(
        "silent_distribution",
        fwd5=(484, 0.636, -0.25, -0.44),
        fwd21=(284, 0.511, 3.33, -1.26),
    )
    h = pattern_history(row)
    assert h is not None
    assert h["horizon"] == "fwd5"  # horizon terbaik menurut rate
    assert h["confidence"] == "tinggi"  # |t| terbaik lintas horizon = 3.33
    assert h["best_tstat"] == 3.33
    assert h["edge_pct"] == 0.44  # |alpha| di horizon terpilih, selalu positif


def test_pattern_history_confidence_ignores_thin_horizons():
    # |t| besar dari sampel tipis tidak boleh menentukan kata keyakinan.
    row = _track_row("x", fwd5=(9, 0.9, 9.9), fwd10=(50, 0.55, 1.2))
    h = pattern_history(row)
    assert h is not None
    assert h["best_tstat"] == 1.2
    assert h["confidence"] == "sedang"


def test_pattern_history_confidence_lemah_without_significant_tstat():
    row = _track_row("x", fwd5=(100, 0.55, 0.3))
    h = pattern_history(row)
    assert h is not None
    assert h["confidence"] == "lemah"


# --------------------------------------------------------------------------- #
# konteks pasar
# --------------------------------------------------------------------------- #


def _panel_with(acc: int, dist: int, neutral: int, thin: int = 0) -> dict:
    """Panel uji: n emiten akumulasi / distribusi / netral / data kurang."""
    panel: dict[str, dict] = {}
    for i in range(acc):
        panel[f"ACC{i}"] = {"name": f"ACC{i}", "rows": _flat_rows(11, net=100.0, value=1000.0)}
    for i in range(dist):
        panel[f"DIS{i}"] = {"name": f"DIS{i}", "rows": _flat_rows(11, net=-100.0, value=1000.0)}
    for i in range(neutral):
        panel[f"NEU{i}"] = {"name": f"NEU{i}", "rows": _flat_rows(11, net=10.0, value=1000.0)}
    for i in range(thin):
        panel[f"THIN{i}"] = {"name": f"THIN{i}", "rows": _flat_rows(2, net=100.0, value=1000.0)}
    return panel


def test_market_breadth_counts_sides_and_share():
    b = market_breadth(_panel_with(acc=1, dist=2, neutral=1, thin=1))
    assert b is not None
    assert b["total"] == 5
    assert (b["accumulating"], b["distributing"], b["neutral"], b["insufficient"]) == (1, 2, 1, 1)
    # penyebut "sided" = 3 (netral & data kurang keluar) -> 2/3
    assert b["sided"] == 3
    assert b["distributing_share"] == 66.7
    # porsi terhadap SEMUA emiten ber-data -> 2/5
    assert b["distributing_pct"] == 40.0
    assert b["date"] == "2026-09-11"


def test_market_breadth_none_when_no_emitters():
    assert market_breadth({}) is None
    assert market_breadth({"X": {"name": "X", "rows": []}}) is None


def test_market_breadth_share_none_when_all_neutral():
    b = market_breadth(_panel_with(acc=0, dist=0, neutral=3))
    assert b is not None
    assert b["sided"] == 0
    assert b["distributing_share"] is None


def test_market_context_sentence_with_tide_is_honest():
    # 66,7% emiten ber-verdict dibuang asing -> distribusi = searah mayoritas
    b = {"distributing_share": 66.7}
    s = market_context_sentence("distribusi", b)
    assert s is not None
    assert "searah mayoritas pasar" in s
    assert "67%" in s
    assert "belum tentu ciri khas" in s


def test_market_context_sentence_against_tide():
    b = {"distributing_share": 30.0}
    s = market_context_sentence("distribusi", b)
    assert s is not None
    assert "melawan arus pasar" in s
    # akumulasi melihat sisi sebaliknya: 70% ditimbun -> searah mayoritas
    s_acc = market_context_sentence("akumulasi", b)
    assert s_acc is not None
    assert "searah mayoritas pasar" in s_acc and "70%" in s_acc


def test_market_context_sentence_neutral_side_describes_split():
    s = market_context_sentence("netral", {"distributing_share": 61.2})
    assert s is not None
    assert "terbelah" in s
    assert "searah" not in s and "melawan" not in s


def test_market_context_sentence_none_without_breadth():
    assert market_context_sentence("distribusi", None) is None
    assert market_context_sentence("distribusi", {}) is None
    assert market_context_sentence("distribusi", {"distributing_share": None}) is None


def test_build_narrative_includes_market_context():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    v = verdict(rows)
    pats = detect_patterns(rows)
    out = build_narrative("BBCA", v, pats, None, market={"distributing_share": 30.0})
    joined = " ".join(out)
    # akumulasi, hanya 30% pasar dibuang -> 70% ditimbun -> searah mayoritas
    assert "Konteks pasar" in joined
    assert "70% emiten ber-verdict juga sedang ditimbun asing" in joined
    # ditempatkan setelah kalimat bukti pola, dan jadi kalimat terakhir bila
    # tidak ada level (rng=None di uji ini)
    assert out[-1].startswith("Konteks pasar")


def test_build_narrative_without_market_has_no_market_line():
    rows = _flat_rows(20, net=100.0, value=1000.0)
    v = verdict(rows)
    pats = detect_patterns(rows)
    joined = " ".join(build_narrative("BBCA", v, pats, None))
    assert "Konteks pasar" not in joined
