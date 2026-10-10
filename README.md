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

## Framework Analisis Saham (peran → modul → output)
Peta ini **mencerminkan kode yang benar-benar ada** — tiap baris menyebut modul
nyata dan output/tabel nyata. Yang belum ada didaftarkan terpisah di bawah,
supaya tidak diandalkan.

| Peran | Modul | Output nyata |
|-------|-------|--------------|
| **Data engine / ingester** | `client.py`, `cf_transport.py`, `storage.py`, `market_segment.py`, `live_capture.py`; penjadwal di `cli.py` | `raw_eod` (source of truth), `prices_pit`, `index_quotes` / `index_summary_daily`, `stock_quotes` / `stock_summary_daily`, `research.market_segment_daily`, `research.intraday_ticks`, `research.corporate_actions`, `research.quarantine_eod` |
| **Analis Teknikal** | `analysis.py` (SMA20/50, RSI, Bollinger, MACD, ADX; `generate_signal` / `signal_series`) | `research.signal_log` (riwayat BUY/SELL + outcome forward) |
| **Analis Rezim & base‑rate** | `research/regime.py`, `research/events.py` | `research.regime_daily`; event study (median forward & abnormal) dihitung saat diminta |
| **Analis Faktor & kalibrasi IC** | `research/factors.py`, `research/ic.py`, `research/ic_history.py`, `research/monthly_ic.py` | `research.factor_ic_history` (IC, ICIR, t‑stat, bobot) |
| **Analis Aliran (proksi broker)** | `research/broker_activity.py`, `research/orderbook.py`, `broker_flow.py`, `smart_money.py` | skor aktivitas broker + jejak smart money (dihitung); `research.broker_daily` = agregat **per firma se‑pasar**, bukan per saham |
| **Analis Sentimen (dari transaksi, bukan berita)** | `research/sentiment.py` | gauge pasar 0–100 + skor emiten −1..+1, dari arus asing + order book + breadth (dihitung) |
| **Analis Kepemilikan** | `ownership.py`, `ownership_store.py` | `research.ownership` (snapshot pemegang saham) |
| **Sintesis / Researcher** | `research/composite.py`, `services.get_hold_check`, `analytics.get_stock_decision`, `research/recommend.py` | verdict Hold Check, Ruang Keputusan, papan Rekomendasi Beli |
| **Evaluasi / track record** | `research/signal_log.py`, `research/recommendation_log.py`, track record `smart_money` | `research.signal_log`, `research.recommendation_log` + hit‑rate/abnormal per horizon |
| **Simulator (bukan eksekusi)** | `backtest.py`, `api/simulation.py` | equity curve, cost model, ledger rebalance — **tanpa persistensi & tanpa kirim order** |
| **API (read‑only)** | `api/` (`analytics.py`, `services.py`, `app.py`) | JSON lewat connection pool; **nol kalkulasi baru**, hanya membaca DB |
| **Frontend** | `idx-web/` (Next.js + Recharts + SWR) | presentasi saja; semua hitungan di backend |

### Belum ada (jangan diandalkan)

| Diklaim dokumen lama | Kenyataan |
|---|---|
| `research.fundamental`, `fundamental_score` | ❌ tidak ada. Tidak ada P/E–PBV di endpoint IDX gratis; `/valuation` hanya z‑score harga terhadap band‑nya sendiri |
| `research.news`, `news_log` (“Analis Berita”) | ❌ tidak ada sumber berita sama sekali |
| `research.tech_signal` | ❌ tidak ada; sinyal disimpan di `research.signal_log` |
| `research.risk_log` (“Risk Management”) | ❌ tidak ada. Sizing/stop baru ada sebagai perhitungan di `research/recommend.py`, belum dipersist |
| “Trader Agent” yang mengirim order | ❌ tidak ada. Yang tersedia hanya simulator backtest |

---

## Rekomendasi Beli (kandidat + level eksekusi + track record)

Menjawab "mana saham yang masuk akal dibeli, di harga berapa, stop di mana?".
Berbeda dari **Hold Check** (yang bertanya "masih layak dipegang?", band
`STRONG HOLD / HOLD / TRIM / EXIT`), modul ini menyusun **daftar kandidat beli
berperingkat se‑pasar** dengan level eksekusi.

* `research/recommend.py` (pure): skor 0–100, `entry_plan`
  (pullback / breakout / trend), stop, target, R/R, `position_size_pct`, dan
  `assign_grades` (peringkat pool harian).
* `research/recommendation_log.py`: log harian `research.recommendation_log` +
  evaluasi forward return T+1 & abnormal vs pasar per grade/horizon.
* **Hanya lapisan terukur yang menggerakkan skor.** Komposisi skor
  (`COMPONENT_WEIGHTS`, per horizon) datang dari walk-forward
  `research/score_eval.py` — gate |t| Newey-West ≥ 1,5 & |edge| ≥ 0,15% per
  fold, shrinkage 0,5, lalu stabilitas tanda lintas fold. Lapisan
  **faktor IC** dan **aliran broker** ikut HANYA bila lolos gate IC-nya sendiri
  — kalau belum, kontribusinya nol dan statusnya disebut apa adanya.
* Tiap kandidat **wajib** punya level pembatalan (stop) yang bisa dihitung;
  kalau tidak, emiten tidak ditampilkan.
* **Grade = peringkat relatif di dalam pool hari itu** (C 25% · B 10% · A 2%
  teratas), bukan ambang skor absolut dan bukan probabilitas. Ambang absolut
  pernah dicoba (kalibrasi 2026-10-06) dan gagal: distribusi skor
  ber-titik-massa membuat band B/C jatuh di ambang yang sama (terukur 28%/28%
  padahal labelnya 10%/25%). Lantai likuiditas tetap **Rp 500 jt** (≈ median
  nilai transaksi harian emiten yang benar-benar diperdagangkan).
* **Hasil walk-forward 2026-10-09 (190 sesi, 3 fold) ditulis apa adanya:**
  komposisi yang lolos gate semuanya berbobot negatif (penalti), jadi skor =
  "seberapa bersih nama dari ciri yang historis merugikan", bukan kekuatan
  kandidat. Bukti OOS pooled: h5 IC +0,010 · **h10 −0,019** · h21 +0,038
  (t 1,95) → `DEFAULT_HORIZON = 21`. Track record pasca-perubahan juga **tidak**
  monoton terhadap grade — itu temuan, bukan alasan menyembunyikan angkanya.
* **Pola jejak smart money tampil sebagai alasan, belum menimbang skor.**
  Bukan karena polanya lemah (`pattern_initiation` edge +3,19% di h21), tapi
  karena `foreign_net` baru tersedia sejak Jul 2026 (~50 sesi) sementara jendela
  train 100 sesi — tak ada fold yang memuat kejadiannya, jadi bobotnya belum
  bisa diukur. Harness-nya sudah siap (`MIN_EDGE_DAYS`) untuk mengukur begitu
  histori memanjang.
* Job harian mengisi log setelah `jejak sinyal`, dan alert Telegram dikirim
  sekali saat sebuah emiten **baru masuk grade A** (`RecommendationState`).
* Alat bantu riset, **bukan** rekomendasi keuangan.

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
| `research.recommendation_log` | Daily buy-candidate log (grade + execution levels) whose forward outcomes are measured. |

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
| `/recommendations` | GET | **Rekomendasi Beli** — market‑wide ranked buy candidates (score, grade, entry/stop/target, R/R, sizing). |
| `/stocks/{code}/recommendation` | GET | Buy‑candidate verdict + execution levels for one emiten. |
| `/recommendations/track` | GET | Candidate track record (hit rate, forward & abnormal return per grade/horizon). |

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
python -m idx_scraper.cli signals                # list BUY/SELL signalspython -m idx_scraper.cli signal-log            # view signal history
python -m idx_scraper.cli recommendations [--months N]  # backfill + track record kandidat beli
python -m idx_scraper.cli health                # health‑check (exit 0 = OK)
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

## Logo Emiten (TradingView)
* Script: `scripts/fetch_tradingview_logos.py` mengambil logo dari halaman `tradingview.com/symbols/IDX-{KODE}/` untuk setiap kode di `stock_summary_daily`.
* Output: `idx-web/public/logos/{KODE}.png|svg` dan `idx-web/public/logos/manifest.json` (status `ok` / `fallback` / `failed`).
* Jalankan (dari `idx-scraper/`): `.\.venv\Scripts\python.exe scripts\fetch_tradingview_logos.py`. Kode yang sudah `ok` dilewati; `--only BBCA,TLKM` untuk kode tertentu, `--force` untuk unduh ulang.
* UI (`idx-web/components/TickerLogo.tsx`) memakai logo lokal bila `ok`, selain itu monogram.
* **Hak cipta: [BELUM TERVERIFIKASI].** Logo milik masing-masing emiten dan TradingView. Gunakan hanya untuk tampilan internal; cek lisensi sebelum dipublikasikan.

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