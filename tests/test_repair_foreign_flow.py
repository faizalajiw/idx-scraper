from datetime import date
from types import SimpleNamespace as NS

from scripts.repair_foreign_flow import baris_perlu_tambal


def test_baris_tanpa_foreign_dilewati():
    rows = [NS(code="AAAA", foreign_buy=None, foreign_sell=None, foreign_net=None)]
    assert baris_perlu_tambal(rows, date(2026, 7, 14)) == []

def test_baris_dengan_foreign_dibentuk_jadi_payload():
    rows = [NS(code="BBRI", foreign_buy=1.0, foreign_sell=0.5, foreign_net=0.5)]
    assert baris_perlu_tambal(rows, date(2026, 7, 14)) == [(1.0, 0.5, 0.5, "BBRI", date(2026, 7, 14))]
