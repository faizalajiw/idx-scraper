"""Unit tests: agregasi foreign flow per emiten (pure, tanpa DB)."""

from __future__ import annotations

from idx_scraper.foreign_flow import (
    daily_flow,
    flip_summary,
    rank_in_peers,
    sector_peer_flow,
)


def _r(date: str, net, buy=None, sell=None) -> dict:
    return {"date": date, "net_idr": net, "buy_idr": buy, "sell_idr": sell}


# --------------------------------------------------------------------------- #
# daily_flow
# --------------------------------------------------------------------------- #


def test_daily_flow_sorted_and_shape():
    rows = [
        _r("2026-09-29", 100.0),
        _r("2026-09-26", -50.0),
        _r("2026-09-27", 0.0),
    ]
    flow = daily_flow(rows)
    assert [p["date"] for p in flow] == ["2026-09-26", "2026-09-27", "2026-09-29"]
    assert flow[0]["net"] == -50.0
    assert flow[1]["net"] == 0.0
    assert flow[2]["net"] == 100.0


def test_daily_flow_first_day_never_flips():
    flow = daily_flow([_r("2026-09-29", -100.0)])
    assert flow[0]["flip"] is False
    assert flow[0]["streak"] == 1


def test_daily_flow_flip_detection():
    rows = [
        _r("2026-09-25", -100.0),  # jual
        _r("2026-09-26", -50.0),  # masih jual
        _r("2026-09-29", 80.0),  # FLIP -> net buy
    ]
    flow = daily_flow(rows)
    assert [p["flip"] for p in flow] == [False, False, True]
    assert flow[2]["streak"] == 1


def test_daily_flow_zero_day_does_not_flip_but_resets_streak():
    rows = [
        _r("2026-09-25", -100.0),
        _r("2026-09-26", 0.0),  # diam: bukan flip, streak reset
        _r("2026-09-29", -30.0),  # tanda sama dgn 09-25, tapi streak mulai lagi
    ]
    flow = daily_flow(rows)
    assert [p["flip"] for p in flow] == [False, False, False]
    assert [p["streak"] for p in flow] == [1, 0, 1]


def test_daily_flow_zero_between_does_not_hide_direction_change():
    # Setelah hari nol, tanda berubah -> tetap flip (last_nonzero_sign dipakai).
    rows = [
        _r("2026-09-25", -100.0),
        _r("2026-09-26", 0.0),
        _r("2026-09-29", 30.0),
    ]
    flow = daily_flow(rows)
    assert flow[2]["flip"] is True
    assert flow[2]["streak"] == 1


def test_daily_flow_null_day_kept_as_none():
    rows = [_r("2026-09-25", -100.0), _r("2026-09-26", None), _r("2026-09-29", 50.0)]
    flow = daily_flow(rows)
    assert flow[1]["net"] is None
    assert flow[1]["streak"] is None
    assert flow[1]["flip"] is None
    # Hari null tidak memutus streak non-nol? Tidak: streak dihitung dari
    # baris bernilai; hari None tidak mereset last_nonzero_sign.
    assert flow[2]["flip"] is True


def test_daily_flow_non_finite_treated_as_missing():
    flow = daily_flow([_r("2026-09-29", float("nan")), _r("2026-09-30", float("inf"))])
    assert flow[0]["net"] is None
    assert flow[1]["net"] is None


def test_daily_flow_buysell_passthrough():
    flow = daily_flow([_r("2026-09-29", 100.0, buy=300.0, sell=200.0)])
    assert flow[0]["buy"] == 300.0
    assert flow[0]["sell"] == 200.0


# --------------------------------------------------------------------------- #
# flip_summary
# --------------------------------------------------------------------------- #


def test_flip_summary_after_buy_flip():
    flow = daily_flow(
        [
            _r("2026-09-25", -100.0),
            _r("2026-09-26", -50.0),
            _r("2026-09-29", 80.0),
            _r("2026-09-30", 20.0),
        ]
    )
    s = flip_summary(flow)
    assert s["last_flip"] == {"date": "2026-09-29", "to": "net_buy"}
    assert s["days_since_flip"] == 1
    assert s["current_side"] == "net_buy"
    assert s["current_streak"] == 2


def test_flip_summary_flip_on_last_day():
    flow = daily_flow([_r("2026-09-25", -100.0), _r("2026-09-29", 5.0)])
    s = flip_summary(flow)
    assert s["last_flip"] == {"date": "2026-09-29", "to": "net_buy"}
    assert s["days_since_flip"] == 0


def test_flip_summary_no_data():
    s = flip_summary([])
    assert s["last_flip"] is None
    assert s["days_since_flip"] is None
    assert s["current_streak"] is None
    assert s["current_side"] == "flat"


def test_flip_summary_all_none_values():
    s = flip_summary(daily_flow([_r("2026-09-29", None)]))
    assert s["current_side"] == "flat"
    assert s["last_flip"] is None


def test_flip_summary_never_flipped():
    flow = daily_flow([_r("2026-09-25", 10.0), _r("2026-09-29", 20.0)])
    s = flip_summary(flow)
    assert s["last_flip"] is None
    assert s["days_since_flip"] is None
    assert s["current_side"] == "net_buy"


# --------------------------------------------------------------------------- #
# sector_peer_flow
# --------------------------------------------------------------------------- #


def test_peer_flow_sum_window_and_sorted():
    self_rows = [_r("2026-09-29", 100.0), _r("2026-09-30", 50.0)]
    peers = {
        "AAAA": [_r("2026-09-29", 10.0), _r("2026-09-30", 20.0)],  # 30
        "BBBB": [_r("2026-09-29", 500.0)],  # 500
        "CCCC": [_r("2026-09-29", -80.0)],  # -80
    }
    out = sector_peer_flow(self_rows, peers, days=10)
    assert [p["code"] for p in out] == ["BBBB", "__self__", "AAAA", "CCCC"]
    assert out[1]["net_sum"] == 150.0
    assert out[1]["is_self"] is True
    assert out[0]["is_self"] is False


def test_peer_flow_window_slices_last_n():
    self_rows = [_r(f"2026-09-{d:02d}", float(d)) for d in range(1, 11)]  # net = 55 total
    peers = {"AAAA": [_r("2026-09-10", 999.0)]}
    out = sector_peer_flow(self_rows, peers, days=3)
    me = next(p for p in out if p["is_self"])
    assert me["net_sum"] == 27.0  # 8 + 9 + 10
    assert me["n_days"] == 3


def test_peer_flow_self_always_included_even_if_peers_empty():
    out = sector_peer_flow([_r("2026-09-29", 25.0)], {}, days=5)
    assert len(out) == 1
    assert out[0]["is_self"] is True
    assert out[0]["net_sum"] == 25.0


def test_peer_flow_peer_all_none_skipped():
    out = sector_peer_flow(
        [_r("2026-09-29", 25.0)],
        {"AAAA": [_r("2026-09-29", None)]},
        days=5,
    )
    assert [p["code"] for p in out] == ["__self__"]


def test_peer_flow_self_no_data_excluded():
    out = sector_peer_flow([_r("2026-09-29", None)], {"AAAA": [_r("2026-09-29", 5.0)]}, days=5)
    assert [p["code"] for p in out] == ["AAAA"]


# --------------------------------------------------------------------------- #
# rank_in_peers
# --------------------------------------------------------------------------- #


def test_rank_in_peers_basic_and_median():
    peers = [
        {"code": "A", "net_sum": 500.0, "is_self": False},
        {"code": "__self__", "net_sum": 150.0, "is_self": True},
        {"code": "B", "net_sum": 30.0, "is_self": False},
        {"code": "C", "net_sum": -80.0, "is_self": False},
    ]
    r = rank_in_peers(peers)
    assert r["rank"] == 2
    assert r["count"] == 4
    # median dari (30, 150) -> 90
    assert r["median_sum"] == 90.0


def test_rank_in_peers_odd_median():
    peers = [
        {"code": "A", "net_sum": 10.0, "is_self": False},
        {"code": "B", "net_sum": 20.0, "is_self": False},
        {"code": "__self__", "net_sum": 30.0, "is_self": True},
    ]
    r = rank_in_peers(peers)
    assert r["rank"] == 3
    assert r["median_sum"] == 20.0


def test_rank_in_peers_empty():
    r = rank_in_peers([])
    assert r == {"rank": None, "count": 0, "median_sum": None}
