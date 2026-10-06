# IDX Scraper

Near‑real‑time Indonesia Stock Exchange (IDX) data scraper for algorithmic research. All sources are public and free; no paid licenses are required.

## Quick Start

### 1. Install & Setup
```powershell
cd D:\Project\market-labs\idx-scraper
.venv\Scripts\python.exe -m pip install -e .
```

### 2. Environment (`.env`)
```dotenv
# Storage backend (sqlite or supabase)
IDX_STORAGE=sqlite
IDX_SQLITE_PATH=./data/idx.db

# Watch‑list (comma‑separated symbols)
IDX_WATCHLIST=BBCA,BBRI,BMRI,TLKM,ASII

# Polling intervals (seconds)
IDX_INDEX_INTERVAL=60
IDX_WATCHLIST_INTERVAL=60

# Market hours (WIB)
IDX_MARKET_OPEN=09:00
IDX_MARKET_CLOSE=16:00

# Chrome profile that has passed the Cloudflare challenge
IDX_CHROME_PROFILE_DIR=C:\Users\<you>\AppData\Local\idx-scraper-chrome-p3
```
> **Tip** – Set `IDX_STORAGE=supabase` and add `SUPABASE_DB_URL=...` when running in production.

### 3. Run the service
```powershell
python -m idx_scraper.cli serve
```
The scheduler starts automatically and runs only on weekdays between 09:00‑16:00 WIB.

### 4. Optional one‑off commands
```powershell
python -m idx_scraper.cli snapshot          # fetch a single snapshot
python -m idx_scraper.cli eod 20261005      # fetch EOD for a specific date
python -m idx_scraper.cli seed --days 365   # seed historic OHLCV from Yahoo Finance
```

---

## Framework Analisis Saham Multi‑Agen
| Team | Role | Output |
|------|------|--------|
| **Analis Fundamental** | Analisis laporan keuangan, penilaian nilai intrinsik | `fundamental_score` (table `research.fundamental`) |
| **Analis Sentimen** | Aggregasi berita & media sosial | `sentiment_score` (table `research.sentiment`) |
| **Analis Berita** | Makro‑ekonomi & peristiwa global | `news_log` (table `research.news`) |
| **Analis Teknikal** | Indikator MACD, RSI, pola candlestick | `technical_signal` (table `research.tech_signal`) |
| **Researcher Team** | Evaluasi insight, menyeimbangkan bullish/bearish | – |
| **Trader Agent** | Membuat order‑book virtual berdasarkan laporan | – |
| **Risk Management** | Memantau volatilitas, likuiditas, eksposur | `research.risk_log` |
| **Engineering** | Data‑engine (pipeline), execution‑engine, monitoring | – |

---

## Cloudflare Bypass (Chrome Profile)
IDX endpoints are protected by Cloudflare. The scraper uses Playwright‑Chrome in *persistent* mode and re‑uses cookies from a profile that has already solved the challenge.

1. Export a working Chrome profile (e.g. *Profile 3*) to a dedicated folder:
```powershell
$src = "$env:LOCALAPPDATA\Google\Chrome\User Data"
$dst = "$env:LOCALAPPDATA\idx-scraper-chrome-p3"
robocopy "$src\Profile 3" "$dst\Profile 3" /E /XD Cache "Code Cache" GPUCache
Copy-Item "$src\Local State" "$dst\Local State"
```
2. Point `IDX_CHROME_PROFILE_DIR` in `.env` to that folder.
3. The scheduler automatically launches Chrome with this profile; no further action is needed.

> **Gotchas**
> * Chrome refuses two simultaneous instances on the same `user-data-dir`. Use a different temporary profile for manual runs.
> * The first run may fail with `Execution context destroyed`; simply retry once.

---

## Storage & Schemas
* **SQLite** – default, file `./data/idx.db` (local development).
* **Supabase** – production; set `IDX_STORAGE=supabase` and `SUPABASE_DB_URL` in `.env`. Execute `sql/schema.sql` on the remote database.

### Core Tables (excerpt)
| Table | Description |
|-------|-------------|
| `raw_eod` | Raw end‑of‑day data from IDX (`GetStockSummary`). |
| `prices_pit` / `research.prices_asof_adj` | Point‑in‑time price series (bitemporal, no look‑ahead). |
| `index_quotes` / `index_summary_daily` | Live IHSG composite quote (official close). |
| `research.market_segment_daily` | Daily aggregation of **regular** vs **non‑regular** market volume/value. |
| `research.signal_log` | BUY/SELL signal history. |
| `research.factor_ic_history` | Information‑Coefficient for factor models. |

---

## API Reference (FastAPI – `/api/v1`)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/index` | GET | Current IHSG composite quote. |
| `/watchlist` | GET | Live quotes for symbols in `IDX_WATCHLIST`. |
| `/eod/{date}` | GET | End‑of‑day snapshot for all emiten (raw). |
| `/market-segment/{date}` | GET | Regular vs non‑regular market aggregation. |
| `/health` | GET | Simple health check (0 = OK). |
| `/signal-log` | GET | Latest BUY/SELL signals. |
| `/ownership/{code}` | GET | Top‑10 shareholders for a given emiten. |

All endpoints accept an optional API key (`X‑API‑KEY`) read from `.env` (`IDX_API_KEY`).

---

## Scheduler & Jobs
The scheduler (APScheduler, timezone WIB) starts with `serve`.

| Job ID | Schedule | Action |
|--------|----------|--------|
| `idx` | every 60 s (market hours) | Fetch index quote & rotate browser‑probe. |
| `wl` | every 60 s (market hours) | Fetch watch‑list quotes. |
| `_job_index_eod_close` | daily 16:05 WIB | Back‑fill official IHSG close (`scripts/backfill_index_eod_idx.py`). |
| `_job_backfill_market_segment` | daily 16:10 WIB | Compute `research.market_segment_daily`. |
| `health_check` | every 300 s | Verify today’s `raw_eod` & `index_quotes`; exit 1 if missing. |
| `prune_logs` | daily 02:00 WIB | Delete log files older than 7 days. |

---

## CLI Commands
```powershell
python -m idx_scraper.cli snapshot                # one‑off fetch
python -m idx_scraper.cli serve                   # start scheduler
python -m idx_scraper.cli eod [YYYYMMDD]           # fetch specific EOD
python -m idx_scraper.cli seed [--days N]        # seed historic OHLCV from Yahoo
python -m idx_scraper.cli signals                # list BUY/SELL signals
python -m idx_scraper.cli signal-log            # view signal history
python -m idx_scraper.cli health                # health‑check (exit 0 = OK)
python -m idx_scraper.cli ic [--days N]         # compute IC and store in factor table
python -m idx_scraper.cli alerts [--test]        # send Telegram alerts for signal changes
python -m idx_scraper.cli ownership [--codes AA,BB] [--all] [--date YYYY-MM-DD]
python -m idx_scraper.cli trade-summary [--date YYYY-MM-DD] [--rebuild]
python -m idx_scraper.cli watchlist               # fetch current watch‑list prices
python -m idx_scraper.cli source [YAHOO|IDX]      # view/set live data source
```

### Back‑fill Scripts (run manually or via scheduler)
* `scripts/backfill_broker_eod.py`
* `scripts/backfill_index_eod_idx.py`
* `scripts/backfill_corp_actions.py`
* `scripts/backfill_raw_eod.py`
* `scripts/backfill_eod_yahoo.py`
* `scripts/intraday_capture.py`
* `scripts/refresh_sector_map.py`
* `scripts/backfill_market_segment.py`

---

## Health‑Check & Monitoring
* `cli health` exits 0 when today’s data is present; otherwise 1.
* Telegram notifications (via `src/idx_scraper/notify.py`) are sent for:
  * Scheduler start / stop
  * Job failures
  * Data‑lag warnings
* Optional Prometheus metrics – enable with `IDX_PROMETHEUS=true` in `.env` (exposes `/metrics`).

---

## Glossary
* **EOD** – End‑of‑Day snapshot.
* **PIT** – Point‑In‑Time, bitemporal view without look‑ahead bias.
* **Regular market** – Trades during official IDX session (09:00‑16:00 WIB).
* **Non‑regular market** – After‑hours / pre‑market trades (captured via Yahoo fallback).
* **Chrome Transport** – Playwright‑Chrome process that re‑uses Cloudflare cookies.
* **Back‑fill** – Re‑compute historic aggregates after a schema or source change.

---

*Last updated: 2026‑10‑06*