"""Unit tests: faktor order-book (research.orderbook) — pure computation."""

from __future__ import annotations

from itertools import pairwise

import pandas as pd

from idx_scraper.research.orderbook import (
    OB_FACTOR_COLUMNS,
    attach_orderbook_factors,
    compute_daily,
)

COLS = ["code", "ts", "board", "bid", "bid_volume", "offer", "offer_volume", "close"]


def _snaps(
    code: str,
    ts: list[pd.Timestamp],
    bid_vol: list[float],
    off_vol: list[float],
    closes: list[float],
    board: str = "RG",
) -> pd.DataFrame:
    n = len(ts)
    return pd.DataFrame(
        {
            "code": [code] * n,
            "ts": ts,
            "board": [board] * n,
            "bid": closes,
            "bid_volume": bid_vol,
            "offer": closes,
            "offer_volume": off_vol,
            "close": closes,
        }
    )


def _morning(n: int = 10, day: str = "2026-09-25") -> list[pd.Timestamp]:
    return list(pd.date_range(f"{day} 09:50", periods=n, freq="1min"))


def test_ob_imbalance_constant_thick_bid():
    # Bid selalu 3x offer -> imbalance +0.5; harga datar -> absorption NaN.
    snaps = _snaps(
        "AAAA", _morning(10),
        [150] * 10, [50] * 10, [100.0] * 10,
    )
    out = compute_daily(snaps)
    assert len(out) == 1
    r = out.iloc[0]
    assert r["ob_imbalance"] == 0.5
    assert pd.isna(r["ob_absorption"])  # tidak ada gerakan -> tidak bermakna


def test_ob_absorption_confirms_price_direction():
    # Buku mengikuti arah tick: dir=+1 -> bid tebal, dir=-1 -> offer tebal.
    closes = [100.0, 101.0, 100.0, 101.0, 100.0]
    dirs = [0.0] + [c2 - c1 for c1, c2 in pairwise(closes)]
    bid_vol = [100.0 + 50 * d for d in dirs]
    off_vol = [100.0 - 50 * d for d in dirs]
    snaps = _snaps("AAAA", _morning(5), bid_vol, off_vol, closes)
    out = compute_daily(snaps, min_snaps=3)
    r = out.iloc[0]
    # Signed contribution +0.5 di 4 interval bergerak -> absorption +0.5.
    assert r["ob_absorption"] == 0.5
    # Rata-rata imbalance mentah = 0 (simetris naik/turun).
    assert r["ob_imbalance"] == 0.0


def test_ob_absorption_negative_when_book_fights_price():
    # Absorption: harga naik tapi offer justru lebih tebal (dan sebaliknya).
    closes = [100.0, 101.0, 100.0, 101.0, 100.0]
    dirs = [0.0] + [c2 - c1 for c1, c2 in pairwise(closes)]
    bid_vol = [100.0 - 50 * d for d in dirs]
    off_vol = [100.0 + 50 * d for d in dirs]
    snaps = _snaps("AAAA", _morning(5), bid_vol, off_vol, closes)
    out = compute_daily(snaps, min_snaps=3)
    assert out.iloc[0]["ob_absorption"] == -0.5


def test_min_snaps_gate_yields_nan():
    snaps = _snaps("AAAA", _morning(5), [150] * 5, [50] * 5, [100.0] * 5)
    out = compute_daily(snaps)  # default min_snaps=8, hanya 5 snapshot
    assert len(out) == 1
    assert pd.isna(out.iloc[0]["ob_imbalance"])
    # Gate longgar -> valid lagi.
    out2 = compute_daily(snaps, min_snaps=4)
    assert out2.iloc[0]["ob_imbalance"] == 0.5


def test_dedup_prefers_regular_board():
    # Dua baris per timestamp (RG + non-RG): RG harus jadi wakil snapshot.
    ts = _morning(8)
    snaps = pd.concat(
        [
            _snaps("AAAA", ts, [150] * 8, [50] * 8, [100.0] * 8, board="RG"),
            _snaps("AAAA", ts, [50] * 8, [150] * 8, [100.0] * 8, board="NG"),
        ],
        ignore_index=True,
    )
    out = compute_daily(snaps)
    assert out.iloc[0]["ob_imbalance"] == 0.5  # bukan -0.5 dari baris NG


def test_multi_day_rows_are_separate():
    snaps = pd.concat(
        [
            _snaps("AAAA", _morning(9, "2026-09-24"), [150] * 9, [50] * 9, [100.0] * 9),
            _snaps("AAAA", _morning(9, "2026-09-25"), [50] * 9, [150] * 9, [100.0] * 9),
        ],
        ignore_index=True,
    )
    out = compute_daily(snaps)
    assert len(out) == 2
    by_date = out.set_index(out["date"].dt.strftime("%Y-%m-%d"))
    assert by_date.loc["2026-09-24", "ob_imbalance"] == 0.5
    assert by_date.loc["2026-09-25", "ob_imbalance"] == -0.5


def test_rows_without_book_are_ignored():
    # Snapshot tanpa buku (volume 0/None) dibuang, bukan menjatuhkan n.
    snaps = _snaps("AAAA", _morning(10), [150] * 10, [50] * 10, [100.0] * 10)
    snaps.loc[snaps.index[:4], "bid_volume"] = 0  # 4 baris buku kosong
    out = compute_daily(snaps, min_snaps=4)
    assert out.iloc[0]["ob_imbalance"] == 0.5  # dihitung dari 6 baris valid


def test_compute_daily_empty():
    out = compute_daily(pd.DataFrame(columns=COLS))
    assert out.empty
    assert list(out.columns) == ["code", "date", *OB_FACTOR_COLUMNS]


def test_attach_merge_and_nan_fill():
    panel = pd.DataFrame(
        {
            "code": ["AAAA", "AAAA", "BBBB"],
            "date": pd.to_datetime(["2026-09-25", "2026-09-24", "2026-09-25"]),
            "close": [100.0, 99.0, 50.0],
        }
    )
    ob = pd.DataFrame(
        {
            "code": ["AAAA"],
            "date": pd.to_datetime(["2026-09-25"]),
            "ob_imbalance": [0.4],
            "ob_absorption": [0.1],
        }
    )
    merged = attach_orderbook_factors(panel, ob)
    assert merged.loc[0, "ob_imbalance"] == 0.4
    assert pd.isna(merged.loc[1, "ob_imbalance"])  # tanggal tanpa data
    assert pd.isna(merged.loc[2, "ob_imbalance"])  # kode tanpa data

    # ob_daily kosong -> kolom NaN ditambahkan, panel utuh.
    empty = attach_orderbook_factors(panel, pd.DataFrame(columns=["code", "date", *OB_FACTOR_COLUMNS]))
    assert len(empty) == len(panel)
    assert empty["ob_absorption"].isna().all()
