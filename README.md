# IDX Scraper

Near-real-time Indonesia Stock Exchange (IDX) data scraper for algorithmic research.

**Free + Legal** — uses unofficial public IDX endpoints + Yahoo Finance (no paid license needed).

## Quick Start

### 1. Install & Setup

```bash
cd /d/Project/market-labs/idx-scraper
.venv\Scripts\python.exe -m pip install -e .
```

### 2. Configure (`.env`)

```env
IDX_STORAGE=sqlite
IDX_SQLITE_PATH=./data/idx.db

# Watchlist emiten
IDX_WATCHLIST=BBCA,BBRI,BMRI,TLKM,ASII

# Polling intervals (seconds)
IDX_INDEX_INTERVAL=15
IDX_WATCHLIST_INTERVAL=30

# Market hours (WIB)
IDX_MARKET_OPEN=09:00
IDX_MARKET_CLOSE=16:00

# Chrome profile yang sudah lolos Cloudflare (wajib untuk scrape IDX)
IDX_CHROME_PROFILE_DIR=C:\Users\<you>\AppData\Local\idx-scraper-chrome-p3
```

### 3. Test One-Off Snapshot

```bash
python -m idx_scraper.cli snapshot
```

### 4. Run Continuous Polling

```bash
python -m idx_scraper.cli serve
```

Polling hanya aktif Senin–Jumat 09:00–16:00 WIB (jam bursa).

## Data Sources

| Kebutuhan | Solusi | Catatan |
|---|---|---|
| **Near-real-time index** | `GetIndexList` (internal IDX) | ~5–15 menit delay, polling tiap 15s |
| **Near-real-time per-emiten** | `GetTradingInfoDaily` | ~5–15 menit delay, polling tiap 30s |
| **EOD semua emiten** | `GetStockSummary?date=YYYYMMDD` | End-of-day, sekali hari |
| **Close resmi IHSG** | `GetIndexList` → `Current` | Angka resmi IDX (bukan Yahoo), ditulis 16:00 WIB |
| **Historis & backup** | Yahoo Finance (`.JK`) | ~15 menit delay, free tier |

**Sumber angka IHSG:** close IHSG di dashboard diambil **langsung dari IDX**
(`GetIndexList`, field `Current`) lewat `scripts/backfill_index_eod_idx.py`, bukan lagi
dari Yahoo `^JKSE` (yang selisih beberapa poin dari close resmi). Job harian
`_job_index_eod_close` sudah memakai jalur IDX-live ini.


**Catatan penting:** IDX **tidak punya API real-time publik gratis**. Semua data IDX gratis itu **delayed** (~5–15 menit). Untuk trading frekuensi tinggi (HFT), kamu butuh lisensi berbayar. Untuk riset algoritmik/positional, ini cukup.

## Cloudflare Bypass (Chrome Profile)

Endpoint IDX diproteksi Cloudflare → profil baru/anonim kena **403**. `cf_transport.py`
menjalankan Chrome persisten (Playwright, `channel="chrome"`, non-headless) di event
loop background, lalu menembak JSON IDX lewat `page.evaluate(fetch(...))` supaya ikut
memakai cookie Cloudflare yang sudah lolos challenge.

**Cara yang berhasil — pakai profil Chrome yang sudah login:**

1. Clone salah satu profil Chrome asli yang sudah lolos Cloudflare (mis. "Profile 3")
   ke folder khusus, lewati folder `Cache`, dan **ikut salin `Local State`** (dibutuhkan
   untuk dekripsi cookie):

   ```powershell
   $src = "$env:LOCALAPPDATA\Google\Chrome\User Data"
   $dst = "$env:LOCALAPPDATA\idx-scraper-chrome-p3"
   robocopy "$src\Profile 3" "$dst\Profile 3" /E /XD Cache "Code Cache" GPUCache
   Copy-Item "$src\Local State" "$dst\Local State"
   ```

2. Arahkan `.env` ke folder itu:

   ```env
   IDX_CHROME_PROFILE_DIR=C:\Users\<you>\AppData\Local\idx-scraper-chrome-p3
   ```

   `cli.py` memanggil `dotenv.load_dotenv()` saat import, jadi scheduler (`idx serve`)
   dan semua job scrape otomatis memakai profil ini — tak perlu set env manual.

**Gotcha:**
- Chrome **menolak dua instance** pada `user-data-dir` yang sama. Kalau scheduler sedang
  memegang profil ini, scrape manual harus di-override ke folder profil lain
  (mis. default `%LOCALAPPDATA%\idx-scraper-chrome-profile`).
- Run pertama kadang gagal `Execution context destroyed` (halaman sedang menjalani
  challenge) → cukup **ulangi sekali**, biasanya langsung berhasil.

## Storage


### SQLite (default)

Data tersimpan di `./data/idx.db`. Tanpa setup, cocok untuk:
- Riset lokal
- Dev/testing
- Offline-first

### Supabase (production)

1. Buat project di [supabase.com](https://supabase.com) (gratis)
2. Jalankan [sql/schema.sql](sql/schema.sql) di SQL Editor
3. Isi `.env`:

```env
IDX_STORAGE=supabase
SUPABASE_DB_URL=postgresql://postgres.REF:PASSWORD@REGION.pooler.supabase.com:6543/postgres
```

**Penting:** Pakai **Transaction pooler** (port 6543), bukan direct (5432) — koneksi lebih stabil untuk polling.

### Skema tabel inti

| Tabel | Isi |
|---|---|
| `raw_eod` | Source of truth EOD mentah dari IDX `GetStockSummary` |
| `prices_pit` / `research.prices_asof_adj` | Harga point-in-time (bitemporal, bebas look-ahead) + adjusted |
| `index_quotes` / `index_summary_daily` | Kuotasi & close resmi IHSG (IDX-live) |
| `research.broker_daily` | Broker summary EOD (per firma, seluruh pasar) |
| `research.signal_log` | Jejak sinyal BUY/SELL + forward return |
| `research.factor_ic_history` | Bobot faktor hasil analisis IC |
| `research.*` | Layer PIT: faktor, regime, event study, sentimen, order book |

## CLI Commands

```bash
python -m idx_scraper.cli snapshot          # one-off fetch
python -m idx_scraper.cli serve             # continuous polling loop
python -m idx_scraper.cli eod [YYYYMMDD]    # fetch EOD summary (default: today)
python -m idx_scraper.cli seed [--days N]   # seed historical OHLCV from Yahoo Finance
python -m idx_scraper.cli signals           # show BUY/SELL signals from stored data
python -m idx_scraper.cli signal-log        # backfill + tampilkan track record sinyal (jejak sinyal)
python -m idx_scraper.cli ic [--days N]     # analisis IC + simpan bobot faktor ke research.factor_ic_history
python -m idx_scraper.cli alerts [--test]   # kirim alert Telegram utk sinyal berubah
python -m idx_scraper.cli watchlist         # fetch current prices for watchlist
python -m idx_scraper.cli source [YAHOO|IDX]  # lihat / set sumber data live
```

### Backfill (scripts)

```bash
python -m scripts.backfill_broker_eod --date YYYYMMDD   # broker summary EOD → research.broker_daily
python -m scripts.backfill_index_eod_idx                # close resmi IHSG dari IDX-live → index_quotes + index_summary_daily
python -m scripts.backfill_corp_actions                 # aksi korporasi (split, bonus, rights, dividen)
python -m scripts.backfill_raw_eod                      # isi ulang raw_eod dari sumber
python -m scripts.backfill_eod_yahoo                    # historis OHLCV dari Yahoo (.JK)
python -m scripts.intraday_capture                      # snapshot intraday order book
python -m scripts.refresh_sector_map                    # perbarui pemetaan sektor emiten
```

> `backfill_index_eod_idx` menggantikan `backfill_index_eod` (Yahoo) sebagai sumber
> close IHSG. Butuh `IDX_CHROME_PROFILE_DIR` (lihat bagian Cloudflare Bypass).


## Track Record Sinyal (jejak sinyal)

Sinyal BUY/SELL yang dihasilkan dashboard tidak pernah "diukur". Perintah
`idx signal-log` mengisi `research.signal_log` dari layer PIT (rule yang sama
dengan dashboard, entry di close T+1) lalu melaporkan kinerjanya:

- **Hit rate & mean/median forward return** per horizon (5/10/21 hari bursa)
- **Abnormal vs pasar** — selisih terhadap pasar equal-weight pada window yang sama
- **MFE/MAE** — puncak kenaikan & penurunan terburuk selama horizon
- **Breakdown per regime IHSG** saat sinyal terbentuk

Disiplin yang dipegang: sinyal hanya dari data `<= T`; SELL palsu ex-dividend
tidak dicatat; sinyal di hari emiten tidak diperdagangkan dibuang (harga
carry-over, tidak bisa dieksekusi); job harian memakai window 1 bulan (cukup
menutup koreksi telat), backfill manual 18 bulan.

## Alert Telegram (opsional)

Dapat notifikasi BUY/SELL otomatis saat sinyal **berubah** (anti-spam, ada dedup state):

1. Bikin bot via [@BotFather](https://t.me/BotFather) → salin token
2. Kirim `/start` ke bot lo, lalu ambil chat_id via `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Isi `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
IDX_ALERT_INTERVAL=300   # scan tiap 5 menit saat `idx serve`
```

```bash
python -m idx_scraper.cli alerts --test   # cek koneksi bot
python -m idx_scraper.cli alerts          # kirim alert yang pending (sekali)
python -m idx_scraper.cli serve           # alert otomatis jalan selama jam bursa
```

Sinyal terakhir disimpan di `.signal_state.json` — alert hanya dikirim saat sinyal flip
(mis. HOLD→BUY, BUY→SELL), jadi tidak spam berulang.

## Frontend Dashboard

Frontend Next.js ada di repo terpisah `idx-web`:

```bash
cd ../idx-web
npm install && npm run dev   # http://localhost:3000
```

API backend yang dikonsumsi frontend:

```bash
uvicorn idx_scraper.api.app:app --port 8000
```

Endpoint (read-only), semuanya nol kalkulasi (baca DB saja):

- **Market:** `/api/market/overview`, `/api/market/regime`, `/api/market/regime/history`,
  `/api/market/session-movers`, `/api/market/leaders`, `/api/market/top-brokers`,
  `/api/market/narration`
- **Watchlist & sinyal:** `/api/watchlist` (GET/POST), `/api/signals`,
  `/api/signals/track` (track record sinyal), `/api/hold-check`
- **Per-emiten:** `/api/stocks/{code}/history`, `/api/stocks/{code}/technical`,
  `/api/stocks/{code}/brokers`, `/api/stocks/{code}/events`, `/api/stocks/{code}/dividends`
- **Analitik:** `/api/screener` (+ filter `min_broker_score`), `/api/valuation`,
  `/api/foreign-flow`,
  `/api/sectors`, `/api/sectors/rrg`, `/api/sectors/rotation`, `/api/sentiment`,
  `/api/factors/overview`,
  `/api/broker-activity`
- **Dividen & aksi korporasi:** `/api/dividends/overview`, `/api/dividends/stocks`,
  `/api/corporate-actions`
- **Kualitas data:** `/api/quality/overview`, `/api/quality/quarantine`,
  `/api/quality/quarantine-reasons`, `/api/quality/coverage-gaps`,
  `/api/quality/thin-days`, `/api/quality/corp-actions`, `/api/quality/duplicates`
- **Alert Telegram:** `/api/alerts` (GET/POST/DELETE), `/api/alerts/test`
- **Backtest:** `GET /api/backtest/config`, `POST /api/backtest/run`
- **Utilitas:** `/health`, `POST /api/cache/clear`

Backtest memakai layer `research.prices_asof_adj` (bitemporal, bebas look-ahead)
dan memperhitungkan biaya nyata: komisi per sisi, pajak jual, slippage, serta cap
likuiditas terhadap nilai transaksi harian. Frekuensi rebalance (`daily`,
`weekly`, `monthly`) bisa dipilih — di antara hari rebalance posisi dibiarkan
mengikuti pasar, sehingga turnover dan biaya tidak meledak. Hasilnya selalu
menyertakan perbandingan "sebelum biaya" vs "setelah biaya", benchmark buy & hold,
serta **ledger rebalance**: daftar trade (emiten, aksi, volume, harga eksekusi,
fee) dan posisi akhir tiap sesi — sehingga churn bisa ditelusuri, bukan cuma
terlihat sebagai angka turnover.

Untuk chart & sinyal teknikal, seed dulu data historis:
```bash
python -m idx_scraper.cli eod              # data EOD hari ini
python -m idx_scraper.cli seed             # + historis 180 hari (Yahoo)
```

## Architecture

```
src/idx_scraper/
├── client.py        # IDX API wrappers + Yahoo Finance
├── cf_transport.py  # Cloudflare bypass: Chrome persisten (Playwright) berbagi cookie CF
├── models.py        # Pydantic data models
├── storage.py       # SQLite / Supabase storage layer
├── analysis.py      # Technical indicators (MA, RSI, Bollinger, MACD) + signals
├── research/        # layer PIT: composite, factors, ic, regime, events,
│                    #   signal_log, sentiment, orderbook, broker_activity
├── cli.py           # CLI entry point + scheduler (APScheduler, WIB)
├── api/             # FastAPI read-only API untuk frontend idx-web
│                    #   (analytics, dividends, quality, simulation, alerts, ...)
└── __init__.py
scripts/             # utilitas DB + backfill (broker/index/corp-actions/raw EOD,
│                    #   intraday capture, sector map, migrasi Postgres)
sql/                 # schema Postgres + PIT functions + signal_log/intraday schema
tests/               # test signal_log, sentiment, dll
```

**Key features:**
- ✅ Bypass Cloudflare via Chrome profil login (Playwright, berbagi cookie yang sudah lolos challenge)
- ✅ Close IHSG resmi dari IDX-live (bukan Yahoo)
- ✅ Auto-scheduling (only during market hours)
- ✅ Multi-backend storage (SQLite default, Supabase ready)
- ✅ Structured data models (Pydantic validation)
- ✅ Rate-limited & polite polling

## License

MIT. For personal research/educational use only. Not affiliated with IDX or Yahoo.
