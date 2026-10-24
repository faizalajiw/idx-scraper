"""Tests penempelan rencana posisi ke payload API (sesudah cache, sesudah penahanan).

Kontrak (spec P5 bagian 5.3): rencana dihitung SESUDAH cache dibaca/ditulis dan
SESUDAH penahanan D1 + gerbang rezim, jadi cache papan tidak dipecah per angka
modal dan baris yang ditahan tidak pernah membawa ukuran posisi.
"""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.api.analytics import _with_position_plan


def pege_row(**kw):
    row = {
        "code": "PEGE",
        "score": 56.7,
        "entry_ref": 113.42,
        "stop": 106.33,
        "target": 127.60,
        "position_pct": 16.0,
    }
    row.update(kw)
    return row


def frame(values=(1.0e9,)):
    return pd.DataFrame(
        [
            {"code": "PEGE", "date": pd.Timestamp(f"2026-10-{i:02d}"), "value": v}
            for i, v in enumerate(values, start=1)
        ]
    )


EMPTY_FRAME = pd.DataFrame(columns=["code", "date", "value"])


def test_baris_dapat_plan():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(
        out,
        modal=100_000_000.0,
        risk_pct=1.0,
        open_positions=0,
        exposure_pct=0.0,
        metrics=frame(),
    )
    plan = res["rows"][0]["plan"]
    assert plan["pct"] == 16.0
    assert plan["lots"] == 1410
    assert plan["rupiah"] == pytest.approx(15_992_220.0, rel=1e-9)
    assert plan["note"] == "Tidak ada plafon yang memotong."


def test_dict_masuk_tidak_termutasi():
    out = {"rows": [pege_row()], "gate": None}
    _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert "plan" not in out["rows"][0]


def test_baris_ditahan_tidak_dapat_plan():
    out = {"rows": [pege_row(grade=None, withheld=True)], "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["rows"][0]["plan"] is None


def test_modal_kosong_persen_saja():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(out, metrics=frame())
    assert res["rows"][0]["plan"]["pct"] == 16.0
    assert res["rows"][0]["plan"]["lots"] is None


def test_gerbang_volatile_memotong_separuh():
    out = {"rows": [pege_row()], "gate": {"blocked": False, "vol_state": "VOLATILE"}}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["rows"][0]["plan"]["pct"] == 8.0


def test_riwayat_kosong_tidak_memotong():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=EMPTY_FRAME)
    assert res["rows"][0]["plan"]["caps"]["liquidity"] is None
    assert res["rows"][0]["plan"]["pct"] == 16.0


def test_kandidat_emiten_dapat_plan():
    out = {"candidate": pege_row(), "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["candidate"]["plan"]["lots"] == 1410


def test_kandidat_emiten_ditahan_tidak_dapat_plan():
    out = {"candidate": pege_row(withheld=True), "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["candidate"]["plan"] is None


def test_papan_dan_emiten_sepakat():
    state = {
        "modal": 100_000_000.0,
        "risk_pct": 1.0,
        "open_positions": 2,
        "exposure_pct": 40.0,
    }
    panel = _with_position_plan({"rows": [pege_row()], "gate": None}, metrics=frame(), **state)
    stock = _with_position_plan({"candidate": pege_row(), "gate": None}, metrics=frame(), **state)
    assert panel["rows"][0]["plan"] == stock["candidate"]["plan"]
