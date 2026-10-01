"""Unit tests: jejak smart money — verdict, pola, level, track record, narasi.

Semua pure (tanpa DB) — pola yang sama dengan ``test_foreign_flow.py``.
Angka uji memakai skala yang sama dengan kalibrasi data: net asing diukur
sebagai porsi nilai transaksi (p50|.| ≈ 2%, ambang bermakna 5%).
"""

from __future__ import annotations

from idx_scraper.smart_money import (
    build_narrative,
    collect_pattern_episodes,
    consolidation_range,
    detect_patterns,
    pattern_track_record,
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
