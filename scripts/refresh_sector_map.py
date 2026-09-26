"""Refresh the generated ticker -> sector lookup from Yahoo Finance.

Why this exists
---------------
The free IDX endpoints polled by this project do not expose the official
IDX-IC sector classification, so `api/sector_map.py` carries a hand-curated
map for liquid names only (~250 of ~960 emiten). Everything else fell into
"Lainnya", which made the sector analysis and the RRG chart misleading.

This script closes that gap. It reads the emiten universe from the database,
asks Yahoo Finance for each ticker's `sector` + `industry`, folds those into
the same Indonesian buckets the curated map uses, and writes the result to
`src/idx_scraper/api/sector_lookup.json`. That JSON is checked in, so the API
stays fully offline at runtime — only this refresh job needs the network.

Precedence at runtime: curated `SECTOR_MAP` > generated `sector_lookup.json`
> "Lainnya".

Run (bash):
  cd /d/Project/idx-scraper && set -a && . ./.env && set +a \\
    && PYTHONPATH=src .venv/Scripts/python.exe -m scripts.refresh_sector_map
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yfinance as yf

from idx_scraper.api.sector_map import BUCKETS, SECTOR_MAP

SLEEP_SECONDS = 0.05  # Yahoo tolerates this easily; keeps the job ~2-4 min
OUT_PATH = Path(__file__).resolve().parents[1] / "src" / "idx_scraper" / "api" / "sector_lookup.json"

# Yahoo `industry` -> our bucket. Checked first and in order, so put the
# specific names ahead of the broad ones (e.g. "Coking Coal" before "Coal").
INDUSTRY_RULES: list[tuple[str, str]] = [
    # ---- Perbankan ----
    ("Banks", "Perbankan"),
    # ---- Asuransi ----
    ("Insurance", "Asuransi"),
    # ---- Keuangan Non-Bank ----
    ("Asset Management", "Keuangan Non-Bank"),
    ("Capital Markets", "Keuangan Non-Bank"),
    ("Financial - Credit Services", "Keuangan Non-Bank"),
    ("Credit Services", "Keuangan Non-Bank"),
    ("Financial - Mortgages", "Keuangan Non-Bank"),
    ("Financial - Diversified", "Keuangan Non-Bank"),
    ("Financial Data & Stock Exchanges", "Keuangan Non-Bank"),
    ("Financial Conglomerates", "Keuangan Non-Bank"),
    ("Shell Companies", "Keuangan Non-Bank"),
    # ---- Batubara ----
    ("Coking Coal", "Batubara"),
    ("Thermal Coal", "Batubara"),
    ("Coal", "Batubara"),
    # ---- Energi ----
    ("Oil & Gas", "Energi"),
    ("Uranium", "Energi"),
    ("Solar", "Utilitas"),
    # ---- Logam & Mineral ----
    ("Gold", "Logam & Mineral"),
    ("Silver", "Logam & Mineral"),
    ("Copper", "Logam & Mineral"),
    ("Aluminum", "Logam & Mineral"),
    ("Steel", "Logam & Mineral"),
    ("Other Industrial Metals & Mining", "Logam & Mineral"),
    ("Industrial Metals & Mining", "Logam & Mineral"),
    ("Other Precious Metals & Mining", "Logam & Mineral"),
    ("Metal Fabrication", "Industri Dasar & Kimia"),
    # ---- Properti ----
    ("REIT", "Properti"),
    ("Real Estate", "Properti"),
    # ---- Telkom & Internet ----
    ("Telecom Services", "Telkom & Internet"),
    ("Telecom", "Telkom & Internet"),
    # ---- Teknologi ----
    ("Software", "Teknologi"),
    ("Information Technology Services", "Teknologi"),
    ("Computer Hardware", "Teknologi"),
    ("Consumer Electronics", "Teknologi"),
    ("Scientific & Technical Instruments", "Teknologi"),
    ("Electronic Gaming & Multimedia", "Media & Hiburan"),
    ("Internet Content & Information", "Teknologi"),
    # ---- Media & Hiburan ----
    ("Entertainment", "Media & Hiburan"),
    ("Broadcasting", "Media & Hiburan"),
    ("Advertising Agencies", "Media & Hiburan"),
    ("Publishing", "Media & Hiburan"),
    ("Media", "Media & Hiburan"),
    # ---- Infrastruktur ----
    ("Infrastructure Operations", "Infrastruktur"),
    ("Airports & Air Services", "Transportasi & Logistik"),
    ("Water Utilities", "Utilitas"),
    ("Gas Utilities", "Utilitas"),
    # ---- Konstruksi ----
    ("Engineering & Construction", "Konstruksi"),
    ("Building Products & Equipment", "Konstruksi"),
    ("Building Materials", "Industri Dasar & Kimia"),
    # ---- Transportasi & Logistik ----
    ("Marine Shipping", "Transportasi & Logistik"),
    ("Marine", "Transportasi & Logistik"),
    ("Trucking", "Transportasi & Logistik"),
    ("Railroads", "Transportasi & Logistik"),
    ("Airlines", "Transportasi & Logistik"),
    ("Integrated Freight & Logistics", "Transportasi & Logistik"),
    ("Air Freight & Logistics", "Transportasi & Logistik"),
    ("Logistics", "Transportasi & Logistik"),
    ("Airports", "Transportasi & Logistik"),
    # ---- Agriculture ----
    ("Farm Products", "Agriculture"),
    ("Agricultural Inputs", "Agriculture"),
    ("Lumber & Wood Production", "Agriculture"),
    ("Forest Products", "Agriculture"),
    # ---- Konsumer Primer ----
    ("Packaged Foods", "Konsumer Primer"),
    ("Confectioners", "Konsumer Primer"),
    ("Food Distribution", "Konsumer Primer"),
    ("Beverages", "Konsumer Primer"),
    ("Tobacco", "Konsumer Primer"),
    ("Household & Personal Products", "Konsumer Primer"),
    ("Agricultural Farm Products", "Agriculture"),
    # ---- Konsumer Sekunder ----
    ("Apparel Manufacturing", "Konsumer Sekunder"),
    ("Footwear & Accessories", "Konsumer Sekunder"),
    ("Furnishings", "Konsumer Sekunder"),
    ("Resorts & Casinos", "Konsumer Sekunder"),
    ("Travel Services", "Konsumer Sekunder"),
    ("Leisure", "Konsumer Sekunder"),
    ("Gambling", "Konsumer Sekunder"),
    ("Restaurants", "Konsumer Sekunder"),
    ("Lodging", "Konsumer Sekunder"),
    ("Personal Services", "Konsumer Sekunder"),
    # ---- Otomotif ----
    ("Auto Manufacturers", "Otomotif"),
    ("Auto Parts", "Otomotif"),
    ("Auto & Truck Dealerships", "Otomotif"),
    ("Automobiles", "Otomotif"),
    ("Recreational Vehicles", "Otomotif"),
    # ---- Retail ----
    ("Retail", "Retail"),
    ("Department Stores", "Retail"),
    ("Discount Stores", "Retail"),
    ("Grocery Stores", "Retail"),
    ("Specialty Business", "Jasa & Industri"),
    ("Wholesale", "Retail"),
    # ---- Kesehatan ----
    ("Drug Manufacturers", "Kesehatan"),
    ("Pharmaceutical", "Kesehatan"),
    ("Medical", "Kesehatan"),
    ("Healthcare", "Kesehatan"),
    ("Health", "Kesehatan"),
    ("Diagnostics & Research", "Kesehatan"),
    ("Biotechnology", "Kesehatan"),
    # ---- Industri Dasar & Kimia ----
    ("Chemicals", "Industri Dasar & Kimia"),
    ("Paper & Paper Products", "Industri Dasar & Kimia"),
    ("Packaging & Containers", "Industri Dasar & Kimia"),
    ("Textile Manufacturing", "Industri Dasar & Kimia"),
    ("Apparel - Manufacturers", "Konsumer Sekunder"),
    ("Plastics", "Industri Dasar & Kimia"),
    ("Rubber", "Industri Dasar & Kimia"),
    ("Cement", "Industri Dasar & Kimia"),
    ("Glass", "Industri Dasar & Kimia"),
    ("Refractories", "Industri Dasar & Kimia"),
    ("Industrial Distribution", "Jasa & Industri"),
    ("Industrial Machinery", "Jasa & Industri"),
    ("Tools & Accessories", "Jasa & Industri"),
    ("Farm & Heavy Construction Machinery", "Jasa & Industri"),
    ("Construction Machinery", "Jasa & Industri"),
    # ---- Utilitas ----
    ("Utilities", "Utilitas"),
    # ---- Jasa & Industri ----
    ("Staffing & Employment Services", "Jasa & Industri"),
    ("Consulting & Outsourcing", "Jasa & Industri"),
    ("Specialty Business Services", "Jasa & Industri"),
    ("Business Equipment & Supplies", "Jasa & Industri"),
    ("Rental & Leasing Services", "Jasa & Industri"),
    ("Security & Protection Services", "Jasa & Industri"),
    ("Waste Management", "Jasa & Industri"),
    ("Environmental", "Jasa & Industri"),
    ("Facilities", "Jasa & Industri"),
    ("Education & Training Services", "Jasa & Industri"),
    ("Printing", "Jasa & Industri"),
    ("Publishing - Periodicals", "Media & Hiburan"),
    ("Conglomerates", "Konglomerat"),
    ("Diversified", "Konglomerat"),
]

# Coarse fallback when `industry` is missing or unrecognised. Yahoo's sector
# vocabulary is much smaller, so these are deliberately broad.
SECTOR_FALLBACK: dict[str, str] = {
    "Financial Services": "Keuangan Non-Bank",
    "Energy": "Energi",
    "Basic Materials": "Industri Dasar & Kimia",
    "Industrials": "Jasa & Industri",
    "Consumer Defensive": "Konsumer Primer",
    "Consumer Cyclical": "Konsumer Sekunder",
    "Healthcare": "Kesehatan",
    "Communication Services": "Telkom & Internet",
    "Technology": "Teknologi",
    "Utilities": "Utilitas",
    "Real Estate": "Properti",
}


def classify(sector: str | None, industry: str | None) -> str | None:
    """Fold a Yahoo (sector, industry) pair into one of our buckets."""
    if industry:
        low = industry.lower()
        for needle, bucket in INDUSTRY_RULES:
            if needle.lower() in low:
                return bucket
    if sector:
        low = sector.strip().lower()
        for name, bucket in SECTOR_FALLBACK.items():
            if name.lower() == low:
                return bucket
        # Yahoo sometimes returns the bucket-ish name already ("Banks - Regional").
        for needle, bucket in INDUSTRY_RULES:
            if needle.lower() in low:
                return bucket
    return None


def universe(conn) -> list[str]:
    """Distinct emiten codes we actually hold prices for."""
    rows = conn.execute("select distinct code from research.raw_eod order by code").fetchall()
    return [r[0] for r in rows]


def refresh() -> int:
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        codes = universe(conn)
    print(f"[sector] universe: {len(codes)} emiten")

    lookup: dict[str, str] = {}
    meta: dict[str, dict[str, str | None]] = {}
    unknown: list[str] = []
    failed: list[str] = []

    for i, code in enumerate(codes, 1):
        try:
            info = yf.Ticker(f"{code}.JK").info
        except Exception as e:
            failed.append(code)
            print(f"[warn] {code}: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(SLEEP_SECONDS)
            continue

        sector = info.get("sector")
        industry = info.get("industry")
        bucket = classify(sector, industry)
        meta[code] = {"sector": sector, "industry": industry}
        if bucket:
            lookup[code] = bucket
        else:
            unknown.append(code)
        if i % 50 == 0:
            print(f"  .. {i}/{len(codes)} mapped={len(lookup)} unknown={len(unknown)}")
        time.sleep(SLEEP_SECONDS)

    # A curated entry counts as mapped even when Yahoo returned nothing for the
    # ticker, so don't report it as unmapped. The curated map itself stays in
    # sector_map.py — this file is only the generated layer, and the runtime
    # consults the curated one first.
    unknown = [c for c in unknown if c not in SECTOR_MAP]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (yfinance) sector/industry, folded into IDX-IC style buckets",
        "note": (
            "Generated layer only. The curated SECTOR_MAP in sector_map.py wins "
            "over this file; regenerate with scripts/refresh_sector_map.py"
        ),
        "buckets": sorted(BUCKETS),
        "count": len(lookup),
        "unmapped": sorted(unknown),
        "failed": sorted(failed),
        "sectors": dict(sorted(lookup.items())),
        "raw": dict(sorted(meta.items())),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"[sector] done. mapped={len(lookup)}/{len(codes)} "
        f"unknown={len(unknown)} failed={len(failed)} -> {OUT_PATH}"
    )
    if unknown:
        print(f"[sector] unrecognised (fall back to Lainnya): {', '.join(unknown[:40])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(refresh())
