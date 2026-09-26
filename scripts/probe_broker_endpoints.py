"""Probe endpoint broker summary IDX — verifikasi field live sekali jalan.

Hasil ditulis ke stdout (dipakai manual, bukan bagian pipeline).
"""

from __future__ import annotations

import json

from idx_scraper.client import IDXClient


def main() -> int:
    c = IDXClient()
    c.warmup()
    results: dict[str, object] = {}

    # 1) Verified: agregat per firm seluruh pasar.
    try:
        d = c._get_json("/primary/TradingSummary/GetBrokerSummary?date=20260925&start=0&length=5")
        results["broker_summary_total"] = d.get("recordsTotal")
        results["broker_summary_row"] = (d.get("data") or [None])[0]
    except Exception as e:  # probe: cetak saja, jangan crash
        results["broker_summary_err"] = repr(e)

    # 2) Eksperimental: harapannya per-stock bila endpoint-nya masih hidup.
    try:
        d2 = c._get_json("/primary/TradingSummary/GetStockBrokerSummary?date=20260925&start=0&length=5&stockCode=BBCA&board=RG&brokerTypes=ALL")
        results["stock_broker_keys"] = sorted(d2.keys()) if isinstance(d2, dict) else str(type(d2))
        if isinstance(d2, dict) and d2.get("data"):
            results["stock_broker_row"] = d2["data"][0]
            results["stock_broker_total"] = d2.get("recordsTotal")
    except Exception as e:  # probe: cetak saja, jangan crash
        results["stock_broker_err"] = repr(e)

    print(json.dumps(results, indent=1, default=str))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
