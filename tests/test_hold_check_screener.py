"""Unit test pemecahan FAI-21: predikat screener + scorer hold-check."""

from __future__ import annotations

from idx_scraper.api.analytics import (
    _KRITERIA_SCREENER,
    _FilterScreener,
    _lolos_asing_masuk,
    _lolos_broker,
    _lolos_hari,
    _lolos_momentum_max,
    _lolos_momentum_min,
    _lolos_nilai,
    _lolos_rsi_max,
    _lolos_rsi_min,
    _lolos_signal,
    _lolos_vol_ratio,
    get_screener,
)
from idx_scraper.api.services import (
    _hold_skor_bb,
    _hold_skor_dasar,
    _hold_skor_macd,
    _hold_skor_rsi,
    _hold_skor_sinyal,
    _hold_skor_tren,
    _hold_skor_valuasi,
    _hold_verdict,
)

BARIS_NETRAL = {
    "code": "AAA",
    "signal": "HOLD",
    "rsi": 50.0,
    "momentum_20d": 0.0,
    "value": 1.0e9,
    "foreign_net": 0.0,
    "vol_ratio": 1.0,
    "broker_score": 50.0,
    "hist_days": 40,
}


def baris(**ubah) -> dict:
    b = dict(BARIS_NETRAL)
    b.update(ubah)
    return b


def filter_(**kw) -> _FilterScreener:
    return _FilterScreener(**kw)


# --------------------------------------------------------------------------- #
# predikat screener
# --------------------------------------------------------------------------- #


def test_screener_tanpa_filter_lolos_semua():
    f = filter_()
    assert all(k(baris(), f) for k in _KRITERIA_SCREENER)


def test_lolos_min_days_hanya_di_atas_bawaan():
    pendek = baris(hist_days=25)
    assert _lolos_hari(pendek, filter_(min_days=30))
    assert not _lolos_hari(pendek, filter_(min_days=40))
    # perilaku lama: min_days <= 30 tidak pernah memfilter
    assert _lolos_hari(baris(hist_days=5), filter_(min_days=30))


def test_lolos_signal():
    b = baris(signal="BUY")
    assert _lolos_signal(b, filter_(signal="BUY"))
    assert not _lolos_signal(b, filter_(signal="SELL"))
    assert _lolos_signal(b, filter_())


def test_batas_rsi_vol_dan_nilai_tak_terketahui_gagal():
    # nilai None selalu gagal ketika batas aktif (semantik lama)
    assert not _lolos_rsi_min(baris(rsi=None), filter_(rsi_min=30))
    assert _lolos_rsi_min(baris(rsi=45), filter_(rsi_min=30))
    assert not _lolos_rsi_min(baris(rsi=29.9), filter_(rsi_min=30))
    assert _lolos_rsi_max(baris(rsi=70), filter_(rsi_max=75))
    assert not _lolos_rsi_max(baris(rsi=None), filter_(rsi_max=70))
    assert _lolos_vol_ratio(baris(vol_ratio=2.0), filter_(min_vol_ratio=2.0))
    assert not _lolos_vol_ratio(baris(vol_ratio=None), filter_(min_vol_ratio=1))


def test_lolos_momentum_dan_nilai():
    assert not _lolos_momentum_min(baris(momentum_20d=None), filter_(min_momentum=0))
    assert _lolos_momentum_max(baris(momentum_20d=5.0), filter_(max_momentum=10.0))
    assert _lolos_momentum_max(baris(momentum_20d=None), filter_(max_momentum=None))
    assert _lolos_nilai(baris(value=5.0e9), filter_(min_value=1.0e9))
    assert not _lolos_nilai(baris(value=1.0e6), filter_(min_value=1.0e10))


def test_lolos_asing_masuk_dan_broker():
    assert _lolos_asing_masuk(baris(foreign_net=10.0), filter_(foreign_in_only=True))
    for fnet in (None, 0.0, -5.0):
        assert not _lolos_asing_masuk(baris(foreign_net=fnet), filter_(foreign_in_only=True))
    # tanpa foreign_in_only, foreign_net negatif tetap lolos
    assert _lolos_asing_masuk(baris(foreign_net=-5.0), filter_())
    assert _lolos_broker(baris(broker_score=80.0), filter_(min_broker_score=50))
    assert not _lolos_broker(baris(broker_score=None), filter_(min_broker_score=0))


def test_lolos_screener_gabungan_dan(monkeypatch):
    rows = [
        baris(code="AAA", signal="BUY", rsi=70.0, momentum_20d=5.0),
        baris(code="MID", signal="HOLD", rsi=50.0, momentum_20d=1.0),
        baris(code="JUAL", signal="SELL", rsi=20.0, momentum_20d=-9.0),
    ]
    from idx_scraper.api.analytics import screener

    monkeypatch.setattr(screener, "_screener_rows", lambda: [dict(r) for r in rows])
    assert [r["code"] for r in get_screener(signal="BUY")] == ["AAA"]
    assert [r["code"] for r in get_screener(rsi_min=60, rsi_max=80)] == ["AAA"]
    assert [r["code"] for r in get_screener(min_momentum=0.0)] == ["AAA", "MID"]
    assert len(get_screener()) == 3
    assert [r["code"] for r in get_screener(limit=1)] == ["AAA"]


# --------------------------------------------------------------------------- #
# golden: keluaran JSON baris hold-check identik
# --------------------------------------------------------------------------- #


def test_hold_baris_golden(monkeypatch):
    """Satu baris hold-check: kunci & tipe, tanpa dependensi ranking/broker aktual."""
    import idx_scraper.api.services as svc
    import idx_scraper.research.broker_activity as ba
    import idx_scraper.research.composite as comp

    tetap = _m(signal="BUY", rsi=62.0, z=1.8, trend_up=True, macd_bullish=True, bb_pos="inside")
    monkeypatch.setattr(svc, "_hold_metrics", lambda cur, code, min_days: dict(tetap))
    # Faktor/broker layer tanpa efek: golden test murni skor teknikal + shape.
    monkeypatch.setattr(comp, "apply_factor_layer", lambda s, v, al, base, w: (s, v, al, 0.0))
    monkeypatch.setattr(ba, "apply_broker_layer", lambda s, v, al, bs, val, *a: (s, v, al, 0.0))

    hasil = svc._hold_baris(
        cur=None,
        code="AAAA",
        nama={"AAAA": "Contoh Emiten"},
        market_ranks={},
        weights=None,
        broker_by_code={},
        broker_validated=False,
        min_days=40,
    )
    assert hasil is not None
    assert list(hasil.keys()) == [
        "code",
        "name",
        "signal",
        "trend_up",
        "rsi",
        "macd_bullish",
        "bb_position",
        "z_score",
        "below_target_pct",
        "foreign_net",
        "score",
        "base_score",
        "factor_adj",
        "factor_pct",
        "broker_score",
        "broker_adj",
        "verdict",
        "reasons",
    ]
    assert hasil["code"] == "AAAA"
    assert hasil["name"] == "Contoh Emiten"
    assert hasil["signal"] == "BUY"
    assert hasil["rsi"] == 62.0
    assert hasil["factor_pct"] is None
    assert hasil["broker_score"] is None
    assert hasil["foreign_net"] is None
    # BUY(+20) + tren naik(+10) + macd(+5) + RSI 62 (diam) + z 1.8(-8) + BB inside(0) = 77
    assert hasil["base_score"] == 77
    assert hasil["factor_adj"] == 0.0
    assert hasil["broker_adj"] == 0.0
    assert hasil["reasons"][0] == "Sinyal teknikal BUY (SMA20 > SMA50, RSI sehat)"
    assert len(hasil["reasons"]) == 4


def test_hold_baris_riwayat_kurang_mengembalikan_none(monkeypatch):
    import idx_scraper.api.services as svc

    monkeypatch.setattr(svc, "_hold_metrics", lambda cur, code, min_days: None)
    assert svc._hold_baris(None, "AAAA", {}, {}, None, {}, False, 40) is None


# --------------------------------------------------------------------------- #
# scorer hold-check
# --------------------------------------------------------------------------- #


def _m(**kw) -> dict:
    dasar = {
        "signal": "HOLD",
        "div_cash": 0.0,
        "div_mech": False,
        "trend_up": True,
        "rsi": 50.0,
        "macd_bullish": True,
        "bb_pos": "inside",
        "z": 0.0,
        "below_target": None,
    }
    dasar.update(kw)
    return dasar


def test_hold_skor_dasar_neutral():
    sk, al = _hold_skor_dasar(_m())
    # HOLD (0) + tren naik (+10) + macd bullish (+5) + dalam band = 65
    assert sk == 65.0
    assert len(al) == 4
    # komponen netral penuh (tren/momentum macd rata) kembali ke dasar 50
    sk2, al2 = _hold_skor_dasar(_m(trend_up=False, macd_bullish=False))
    assert sk2 == 40.0
    assert len(al2) == 4


def test_hold_skor_sinyal_semua_cabang():
    sk, al = _hold_skor_sinyal(50.0, [], _m(signal="BUY"))
    assert sk == 70.0
    assert "Sinyal teknikal BUY (SMA20 > SMA50, RSI sehat)" in al
    sk, _ = _hold_skor_sinyal(50.0, [], _m(signal="SELL"))
    assert sk == 25.0
    sk, al = _hold_skor_sinyal(50.0, [], _m(signal="HOLD", div_mech=True, div_cash=21.5))
    assert sk == 50.0
    assert "Rp 22/saham" in al[0]
    sk, al = _hold_skor_sinyal(50.0, [], _m(signal="HOLD", div_mech=False))
    assert sk == 50.0
    assert al == ["Sinyal teknikal HOLD (tidak ada konfirmasi kuat)"]


def test_hold_skor_tren_macd_bb():
    sk, _ = _hold_skor_tren(0.0, [], _m(trend_up=True))
    assert sk == 10.0
    sk, _ = _hold_skor_macd(0.0, [], _m(macd_bullish=False))
    assert sk == -5.0
    sk, _ = _hold_skor_bb(0.0, [], _m(bb_pos="above"))
    assert sk == -5.0
    sk, _ = _hold_skor_bb(0.0, [], _m(bb_pos="below"))
    assert sk == -10.0
    sk, _ = _hold_skor_bb(0.0, [], _m(bb_pos="inside"))
    assert sk == 0.0
    sk, _ = _hold_skor_bb(0.0, [], _m(bb_pos=None))
    assert sk == 0.0


def test_hold_skor_rsi_batas():
    sk, _ = _hold_skor_rsi(0.0, [], _m(rsi=80.0))
    assert sk == -10.0
    sk, _ = _hold_skor_rsi(0.0, [], _m(rsi=20.0))
    assert sk == -10.0
    sk, _ = _hold_skor_rsi(0.0, [], _m(rsi=65.0))
    assert sk == -3.0
    sk, _ = _hold_skor_rsi(0.0, [], _m(rsi=64.9))
    assert sk == 0.0
    sk, _ = _hold_skor_rsi(0.0, [], _m(rsi=None))
    assert sk == 0.0


def test_hold_skor_valuasi():
    sk, _ = _hold_skor_valuasi(0.0, [], _m(z=2.5))
    assert sk == -15.0
    sk, _ = _hold_skor_valuasi(0.0, [], _m(z=1.6))
    assert sk == -8.0
    sk, _ = _hold_skor_valuasi(0.0, [], _m(z=-1.5, trend_up=True))
    assert sk == 5.0  # kualitas dip
    sk, _ = _hold_skor_valuasi(0.0, [], _m(z=-1.5, trend_up=False))
    assert sk == -5.0
    sk, al = _hold_skor_valuasi(0.0, [], _m(z=0.0))
    assert sk == 0.0
    assert al == ["Harga masih dalam band wajar 60-hari"]
    sk, _ = _hold_skor_valuasi(0.0, [], _m(z=None))
    assert sk == 0.0


def test_hold_verdict_batas():
    assert _hold_verdict(75.0) == "STRONG HOLD"
    assert _hold_verdict(55.0) == "HOLD"
    assert _hold_verdict(35.0) == "TRIM"
    assert _hold_verdict(34.9) == "EXIT"
    assert _hold_verdict(0.0) == "EXIT"
