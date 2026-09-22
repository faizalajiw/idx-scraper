# IDX Scraper

Near-real-time Indonesia Stock Exchange (IDX) data scraper for algorithmic research.

**Free + Legal** — uses unofficial public IDX endpoints + Yahoo Finance (no paid license needed).

## Quick Start

### 1. Install & Setup

```bash
cd /d/Project/idx-scraper
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
| **Historis & backup** | Yahoo Finance (`.JK`) | ~15 menit delay, free tier |
| **Crypto real-time** | Binance/OKX API | Free, tanpa API key |

**Catatan penting:** IDX **tidak punya API real-time publik gratis**. Semua data IDX gratis itu **delayed** (~5–15 menit). Untuk trading frekuensi tinggi (HFT), kamu butuh lisensi berbayar. Untuk riset algoritmik/positional, ini cukup.

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

## CLI Commands

```bash
python -m idx_scraper.cli snapshot          # one-off fetch
python -m idx_scraper.cli serve             # continuous polling loop
python -m idx_scraper.cli eod [YYYYMMDD]    # fetch EOD summary (default: today)
python -m idx_scraper.cli seed [--days N]   # seed historical OHLCV from Yahoo Finance
python -m idx_scraper.cli signals           # show BUY/SELL signals from stored data
python -m idx_scraper.cli alerts [--test]   # kirim alert Telegram utk sinyal berubah
python -m idx_scraper.cli watchlist         # fetch current prices for watchlist
```

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

## Dashboard
```bash
python -m streamlit run dashboard/app_pro.py     # full dashboard (charts, signals, movers)
python -m streamlit run dashboard/app.py         # simple dashboard
```

Untuk chart & sinyal teknikal, seed dulu data historis:
```bash
python -m idx_scraper.cli eod              # data EOD hari ini
python -m idx_scraper.cli seed             # + historis 180 hari (Yahoo)
```

## Architecture

```
src/idx_scraper/
├── client.py        # IDX API wrappers (curl_cffi bypass Cloudflare) + Yahoo Finance
├── models.py        # Pydantic data models
├── storage.py       # SQLite / Supabase storage layer
├── analysis.py      # Technical indicators (MA, RSI, Bollinger, MACD) + signals
├── cli.py           # CLI entry point + scheduler
└── __init__.py
dashboard/           # Streamlit dashboards (app.py simple, app_pro.py full)
mcp/                 # MCP server untuk query DB dari AI assistant
```

**Key features:**
- ✅ Bypass Cloudflare using `curl_cffi` (TLS fingerprint imitation)
- ✅ Auto-scheduling (only during market hours)
- ✅ Multi-backend storage (SQLite default, Supabase ready)
- ✅ Structured data models (Pydantic validation)
- ✅ Rate-limited & polite polling

## License

MIT. For personal research/educational use only. Not affiliated with IDX or Yahoo.
