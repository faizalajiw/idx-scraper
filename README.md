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
# Storage backend: sqlite (default) or postgres
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

# Transport IDX: auto (HTTP dulu, Chrome cadangan) | http (tanpa browser) | browser
IDX_TRANSPORT=auto

# Hanya perlu bila memakai Chrome (IDX_TRANSPORT=browser atau fallback auto):
# IDX_CHROME_PROFILE_DIR=C:\Users\<you>\AppData\Local\idx-scraper-chrome-p3
```
> **Tip** – Set `IDX_STORAGE=postgres` and add `DATABASE_URL=...` when running in production (PostgreSQL lokal di server yang sama; lihat `DEPLOY-LINUX.md` §4).

### 3. Run the service
```powershell
python -m idx_scraper.cli serve
```
`serve` menjalankan dua hal sekaligus di dalam satu proses:

- **loop polling** — hanya pada hari bursa di dalam `IDX_MARKET_OPEN`–`IDX_MARKET_CLOSE` (default 09:00–16:00 WIB).
- **scheduler cron (APScheduler)** — job 08:50, 16:10, 17:45, 19:20, dan job akhir pekan, jadi tetap bekerja di luar jam polling.

Scheduler hidup di proses `idx serve`, **bukan** di proses API. Kalau hanya
`python -m idx_scraper.api.serve` yang jalan, API tetap melayani data terakhir
tetapi tidak ada data baru yang masuk.

#### Cara menjalankan backend (agar data selalu update)

Backend terdiri dari dua proses yang harus jalan bersamaan, di folder `idx-scraper` dengan venv aktif:

| Proses | Perintah | Fungsi |
| --- | --- | --- |
| API (FastAPI) | `python -m idx_scraper.api.serve --port 8000` | Melayani endpoint `/api/*` untuk frontend (`idx-web/web`, `PUBLIC_API_BASE_URL=http://localhost:8000`) |
| Scheduler | `python -m idx_scraper.cli serve` | Menarik dan menyimpan data IDX (harga, broker, foreign flow) ke database |

```powershell
# Terminal 1: API
python -m idx_scraper.api.serve --port 8000

# Terminal 2: Scheduler (wajib jalan agar data terisi)
python -m idx_scraper.cli serve
```

- Jika hanya API yang jalan, dashboard tetap tampil tetapi data tidak bertambah.
- Loop polling hanya bekerja Senin–Jumat 09:00–16:00 WIB; job cron tetap jalan di luar jam itu (mis. `refresh_preopen` 08:50, `refresh_s2` 16:10, `health_eod` 17:45). Di luar jam pasar, API melayani data EOD terakhir dari `research.latest_pit`.
- Data broker mengikuti hari bursa. Pada Sabtu/Minggu atau hari libur, log `broker summary ...: 0 baris` adalah perilaku normal, bukan error.
- Cek API: `curl http://localhost:8000/health`.

> **Deploy produksi (Linux):** lihat [DEPLOY-LINUX.md](DEPLOY-LINUX.md) — spec server,
> PostgreSQL + urutan skema, unit & timer systemd, nginx + TLS, backup/restore, dan runbook kegagalan.

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
| **Frontend** | `idx-web/web/` (SvelteKit 3 + Tailwind 4, `adapter-static`) | presentasi saja; semua hitungan di backend |

### Belum ada (jangan diandalkan)

| Diklaim dokumen lama | Kenyataan |
|---|---|
| `research.fundamental`, `fundamental_score` | ❌ tidak ada. Tidak ada P/E–PBV di endpoint IDX gratis; `/valuation` hanya z‑score harga terhadap band‑nya sendiri |
| `research.news`, `news_log` (“Analis Berita”) | ❌ tabel itu belum ada. Berita kini masuk ke `news_items` (lihat *API Berita*) |
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

## IDX & Cloudflare (tanpa browser)
Endpoint JSON IDX memang di belakang Cloudflare, tapi **tidak butuh browser**.
curl_cffi sudah cukup: dengan `impersonate="chrome124"` seluruh endpoint yang
dipakai (`GetIndexList`, `GetStockSummary`, `GetBrokerSummary`,
`GetTradingInfoDaily`, `GetSecuritiesStock`) menjawab 200 tanpa kuki apa pun.
Diuji 15 Okt 2026: 23/23 permintaan 200 selama 45 menit tanpa satu pun proses
browser hidup. (Catatan lama di dokumen ini — "wajib Chrome headful" — sudah
tidak berlaku.)

| `IDX_TRANSPORT` | Perilaku |
|---|---|
| `auto` (default) | HTTP dulu; Chrome dibuka **hanya** kalau IDX menolak, lalu HTTP diistirahatkan `IDX_HTTP_COOLDOWN_SEC` (default 600s) |
| `http` | Tanpa browser sama sekali — mode server Linux, tanpa Chrome & Xvfb |
| `browser` | Selalu lewat Chrome (perilaku lama) |

`IDX_IMPERSONATE` (default `chrome124`) adalah satu-satunya tombol bila suatu
hari Cloudflare memperketat pemeriksaan fingerprint.

**Uji 16 Okt 2026**: keluarga Chrome sudah ditolak (`chrome124`, `chrome131`,
`chrome136` → `403` di `GetIndexList`), sedangkan `safari18_0` dan `firefox135`
dijawab `200`. Kalau EOD tiba-tiba 0 baris dengan `status 403`, ganti ke
`IDX_IMPERSONATE=safari18_0` dulu sebelum menuduh yang lain — dan catat tanggal
ujinya, karena fingerprint bisa tertutup lagi kapan saja.

### Cadangan: Chrome headful (opsional)
Hanya terpakai bila `IDX_TRANSPORT=auto` dan IDX menolak jalur HTTP, atau bila
`IDX_TRANSPORT=browser`. Butuh Google Chrome terpasang dan Playwright
(`pip install -e ".[browser]"`); di Linux tanpa display jalankan di bawah `xvfb-run`.

1. Export a working Chrome profile (e.g. *Profile 3*) to a dedicated folder:
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
* **PostgreSQL** – production; set `IDX_STORAGE=postgres` and `DATABASE_URL` in `.env`. The schema is created on first connection (`sql/schema.sql` is the same DDL, for manual runs).

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

## API Reference (FastAPI – `/api`)

Base URL lokal: `http://localhost:8000`. Sumber kebenaran selalu
`GET /openapi.json`; tabel di bawah adalah ringkasan endpoint yang dipakai
`idx-web`.

**Tidak ada autentikasi.** API ini dirancang untuk jaringan lokal/tepercaya
(lihat docstring `src/idx_scraper/api/app.py`): jangan diekspos ke internet tanpa
menambah autentikasi dan rate limit.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Simple health check (exit 0 = OK). |
| `/api/data-health` | GET | Empat pemeriksa data yang sama dengan `idx health`. |
| `/api/freshness` | GET | Kesegaran data per sumber. |
| `/api/market/overview` | GET | IHSG: quote terbaru, breadth, nilai transaksi. |
| `/api/market/regime` | GET | Rezim pasar + base rate historis. |
| `/api/market/regime/history` | GET | Riwayat rezim per sesi. |
| `/api/market/trade-summary` | GET | Ringkasan transaksi sesi (regular vs non-regular). |
| `/api/market/session-movers` | GET | Pergerakan terbesar sesi berjalan. |
| `/api/market/leaders` | GET | Papan pemimpin (gainers/losers/value). |
| `/api/market/top-brokers` | GET | Broker teratas se-pasar. |
| `/api/broker-flow` | GET | Komposisi aliran broker. |
| `/api/broker-activity` | GET | Skor aktivitas broker. |
| `/api/search` | GET | Cari emiten berdasarkan kode/nama. |
| `/api/watchlist` | GET | Daftar watchlist + quote terakhir. |
| `/api/watchlist` | PUT | Ganti seluruh isi watchlist. |
| `/api/watchlist` | POST | Tambah kode ke watchlist. |
| `/api/watchlist/{code}` | DELETE | Hapus kode dari watchlist. |
| `/api/hold-check` | GET | Verdict tahan/jual untuk satu emiten. |
| `/api/signals` | GET | Sinyal BUY/SELL terbaru. |
| `/api/signals/track` | GET | Track record sinyal per horizon. |
| `/api/stocks/{code}/history` | GET | Riwayat OHLCV point-in-time. |
| `/api/stocks/{code}/technical` | GET | Seri indikator teknikal untuk chart. |
| `/api/stocks/{code}/brokers` | GET | Ringkasan broker per emiten. |
| `/api/stocks/{code}/broker-activity` | GET | Aktivitas broker per emiten. |
| `/api/stocks/{code}/recommendation` | GET | Verdict kandidat beli + level eksekusi. |
| `/api/stocks/{code}/decision` | GET | Ruang Keputusan (verdict + gauge). |
| `/api/stocks/{code}/dossier` | GET | Dossier lengkap satu emiten. |
| `/api/stocks/{code}/dossier.pdf` | GET | Dossier dalam bentuk PDF. |
| `/api/stocks/{code}/ownership` | GET | Top-10 pemegang saham. |
| `/api/stocks/{code}/foreign-flow` | GET | Arus asing per emiten. |
| `/api/stocks/{code}/smart-money` | GET | Jejak smart money per emiten. |
| `/api/stocks/{code}/events` | GET | Event study per emiten. |
| `/api/recommendations` | GET | Rekomendasi Beli se-pasar (skor, grade, level). |
| `/api/recommendations/track` | GET | Track record kandidat per grade/horizon. |
| `/api/foreign-flow` | GET | Arus asing se-pasar. |
| `/api/smart-money/radar` | GET | Radar aliran asing terkuat. |
| `/api/smart-money/patterns` | GET | Papan pola jejak smart money. |
| `/api/smart-money/verdicts` | GET | Watchlist verdict smart money. |
| `/api/smart-money/track-record` | GET | Track record jejak smart money. |
| `/api/sectors` | GET | Analisis sektor. |
| `/api/sectors/rotation` | GET | Rotasi sektor. |
| `/api/news/latest` | GET | Judul berita terbaru (`limit`, default 7). |
| `/api/sentiment` | GET | Gauge sentimen pasar + skor emiten. |
| `/api/factors/overview` | GET | Ringkasan faktor + bobot IC. |
| `/api/valuation` | GET | z-score harga terhadap band-nya sendiri. |
| `/api/screener` | GET | Screener dengan filter. |
| `/api/alerts` | GET | Status alert + aturan tersimpan. |
| `/api/alerts` | POST | Tambah aturan alert. |
| `/api/alerts/{rule_id}` | DELETE | Hapus aturan alert. |
| `/api/alerts/test` | POST | Kirim pesan uji Telegram. |
| `/api/cache/clear` | POST | Bersihkan cache API. |
| `/api/journal/summary` | GET | Ringkasan buku klaim. |
| `/api/journal/claims` | GET | Daftar klaim. |
| `/api/journal/state` | GET | Status gerbang klaim. |
| `/api/journal/halt` | POST | Hentikan penulisan klaim baru. |
| `/api/journal/resume` | POST | Buka lagi penulisan klaim. |
| `/api/backtest/config` | GET | Konfigurasi default backtest. |
| `/api/backtest/run` | POST | Jalankan simulasi backtest (tanpa persistensi). |

Catatan: `/api/market/narration` sudah dihapus (404).

---

## Scheduler & Jobs
The scheduler (APScheduler, timezone WIB) starts with `python -m idx_scraper.cli serve`.

| Job ID | Schedule (WIB) | Action |
|--------|----------|--------|
| `idx` | every 60 s (market hours) | Fetch index quote & rotate browser‑probe. |
| `wl` | every 60 s (market hours) | Fetch watch‑list quotes. |
| *live capture* | rolling sweep (market hours) | `IDX_LIVE_TOP` liquid + 20 movers, `IDX_LIVE_DELAY_MS` per request. |
| `news_refresh` | every `NEWS_INTERVAL` s (default 900), no market-hours gate | Tarik judul dari `snips.stockbit.com` ke `news_items`, lalu hapus berita dengan `published_at` > 7 hari (`prune`). |
| `refresh_preopen` | Mon-Fri 08:50 | Recompute 8 menu + clear API cache (no EOD). |
| `refresh_s1` | Mon‑Thu 12:00 | EOD Sesi I + recompute. |
| `refresh_s1_fri` | Fri 11:30 | EOD Sesi I + recompute. |
| `refresh_s2` | Mon‑Fri **16:10** | EOD Sesi II + recompute (`_job_index_eod_close` 16:10, then `research.market_segment_daily`). Same pipeline as `idx refresh`. |
| `eod_fallback` | Mon‑Fri **16:30, lalu tiap jam s/d 20:30** | Jaring kedua EOD (`_eod_catchup`): kalau EOD hari ini sudah lengkap, keluar dalam ~1 detik (satu query DB, tanpa request IDX); kalau belum, jalankan pipeline penuh. Diulang tiap jam karena dua hal yang tidak ditutup retry: penolakan IDX yang bertahan >4 menit, dan **laptop yang tidur** (proses `serve` ikut beku, bukan mati) — dengan `misfire_grace_time` 2 jam, cek terakhir masih menyala kalau mesin bangun sebelum ~22:30. Menggantikan task Windows `IDX-EOD` yang dihapus 15 Okt 2026. |
| `health_eod` | Mon‑Fri **17:45** | Empat penjaga data, tiap jalur punya `try/except` sendiri: (1) EOD hari ini belum masuk (tenggat `IDX_EOD_DEADLINE_HOUR`/`IDX_EOD_DEADLINE_MINUTE`, default 17:30), (2) cakupan kolom `foreign_*` timpang, (3) sesi yang barisnya di bawah 50% median ("sesi separuh"), (4) hari bursa tanpa baris di tengah rentang. Log + Telegram kalau ada yang gagal. Sengaja SETELAH `eod_fallback` (16:30, slot 17:30 adalah yang pertama di belakang jendela terbit IDX) supaya alarm menilai hasil jaring kedua, bukan percobaan pertama — dan supaya "telat" berarti data memang belum masuk, bukan baru mau terbit. Penjaga 3 melewati sesi hari ini (`in_progress`): sesi yang sedang ditulis bukan kerusakan — kalau ternyata tetap kurang, cek sesi berikutnya yang mengadukan. `misfire_grace_time` 2 jam seperti `eod_fallback`: laptop yang tidur melewati 17:45 tetap mendapat alarmnya begitu bangun. |
| `daily_ic` | Mon‑Fri **16:50** | IC analysis → `research.factor_ic_history` (bobot composite). Murni DB, tidak menyentuh IDX. |
| `ownership_weekly` | Monday **17:00** (weekly) | Snapshot pemegang saham watchlist → `research.ownership`. Dijarakkan lebar dari `refresh_s2` (50 menit) supaya tidak berebut profil Chrome bila transport jatuh ke browser. |
| `journal_claims` | Mon‑Fri **17:55, lalu tiap jam s/d 21:55** | Buku klaim (P6): gerbang dulu (`manual`/`data`/`regime`/`verdict`), baru kandidat. Yang ditolak tetap bercatatan di `research.claim_refusal`; idempoten pada (kode, tanggal, aturan). Diulang tiap jam karena EOD sesi hari ini baru masuk lewat jaring kedua pukul 17:30 — jadwal 17:05 yang lama selalu menulis sesi *kemarin*. |
| `journal_outcomes` | Mon‑Fri **18:10** | Isi hasil klaim yang horizonnya sudah berjalan (target/stop/kedaluwarsa). Perpindahan status tidak pernah dibatalkan, jadi aman diulang. |
| `journal_reminder` | Mon‑Fri **19:20** | Pengingat sore: klaim yang levelnya tersentuh, atau alarm kalau sesi ini tidak melahirkan klaim DAN tidak mencatat penolakan apa pun. Sengaja setelah dua kesempatan tulis (17:55 dan 18:55), supaya sesi yang datanya baru masuk sore tidak divonis "bisu" terlalu cepat. |
| `refresh_weekend_am` | Sat‑Sun 08:45 | Recompute + clear cache (market closed). |
| `refresh_weekend_pm` | Sat‑Sun 16:00 | Recompute + clear cache (market closed). |
| `alerts` | every `IDX_ALERT_INTERVAL` s (default 300) | Telegram alerts (signals + watch rules); only registered when the bot token is set. |

Jadwal ini dikunci oleh `tests/test_serve_schedule.py` — mengubah jam tanpa
memperbarui tes akan gagal. Tes itu menjaga tujuh invarian: `health_eod` tidak
pernah sebelum tenggat EOD (17:30, presisi menit), tenggat itu sendiri selalu di
belakang jam terbit IDX yang terukur (17:13–17:16), jaring kedua (`eod_fallback`)
selalu >12 menit setelah rantai 16:10, alarm selalu setelah jaring kedua, rantai
buku klaim selalu setelah tenggat dan pengingatnya baru setelah ≥2 kesempatan
tulis, jaring kedua & alarm sama-sama punya `misfire_grace_time` 2 jam (menyusul
setelah mesin bangun), dan tidak ada dua job cron yang jatuh di menit yang sama
pada hari yang sama. Jaring kedua itu sendiri diulang tiap jam sampai 20:30 dengan
`misfire_grace_time` 2 jam: slot yang terlewat karena mesin tidur tetap
dikerjakan begitu mesin bangun.

Urutan di dalam `refresh_s2`: EOD saham → close indeks → regime → jejak sinyal →
rekomendasi → broker summary → clear cache. Dua langkah yang menyentuh endpoint
berat IDX (**EOD saham**, **broker summary**) diulang sampai 3 percobaan dengan
jeda 60 s lalu 180 s — IDX pernah menolaknya `403` hanya sesaat (15 Okt 2026:
16:10 dan 16:15 gagal, 17:40 permintaan identik berhasil 963 baris dalam 0,3 s).
Percobaan ulang itu **dimatikan sebelum tenggat EOD** (`_retry_percobaan`, 17:30):
di `refresh_s1` 12:00 dan di rantai 16:10 ringkasan pasar memang belum terbit —
terukur, EOD penuh di `_serve.log` hanya pernah berhasil 17:13:38 (16 Okt) dan
17:16:16 (19 Okt) — jadi retry di sana hanya membuang menit dan menambah beban
IDX. Penolakan yang bertahan lebih lama daripada dua retry ditutup job
`eod_fallback`, yang mengulang ceknya tiap jam 16:30–20:30 sampai EOD hari itu
benar-benar terisi (slot 17:30 adalah yang pertama di belakang jendela terbit).

Dua pipeline tidak pernah berjalan bersamaan: kunci `_PIPELINE_BUSY` melewati
job kedua (pekerjaannya sudah tercakup yang sedang jalan). Perilaku ini dikunci
`tests/test_eod_retry.py` dan `tests/test_eod_fallback.py`.

### Lubang hari bursa lampau
`eod_fallback` dan startup `serve` sama‑sama memanggil `_eod_catchup`, dan itu
sekarang **dua fase**: hari ini dulu (`_eod_catchup_hari_ini`), lalu hari bursa
lampau yang bolong (`_backfill_hari_lampau`, default 21 hari bursa — atur lewat
`IDX_BACKFILL_DAYS`, `0` mematikan). Fase kedua ada karena cek "hari ini" tidak
bisa menyembuhkan sehari yang hilang: kalau mesin tidur/off melewati seluruh
jendela sore (16:10–22:30), tanggal itu tidak pernah kembali. Dulu `health_eod`
juga tidak menangkapnya — ia membandingkan tanggal **terbaru**, bukan lubang di
tengah; sejak penjaga `check_session_gaps` disambungkan, lubang seperti itu ikut
diadukan sore berikutnya. Karena `serve` yang tidak pernah di‑restart pun tetap
menjalankan tick sore, lubang itu tertutup tanpa menunggu start ulang proses.

Biaya saat tidak ada lubang: satu query DB per tanggal (≈ nol, nol request IDX).
Kalau ada yang ditarik: `_simpan_eod_hari` menulis tiga tujuan yang sama seperti
pipeline 16:10 (tabel polos, `raw_eod` + `prices_pit`, agregat pasar), lalu menu
turunan disegarkan sekali (`do_eod=False`; jendela 1 bulan di dalamnya sudah
mencakup tanggal yang baru masuk). Cakapan yang jujur: **saham** bisa ditarik
ulang (endpoint IDX yang sama melayani tanggal lampau), **close resmi indeks
tidak** — satu‑satunya sumbernya `GetIndexList` live, jadi `serve` hanya
melaporkan tanggal yang belum punya close indeks saat startup. Kalau itu perlu,
jalur yang tersedia hanya backfill Yahoo (`python -m scripts.backfill_index_eod
--days 90`, provenance `Yahoo`). Dikunci `tests/test_backfill_hari.py`.

Job `_job_index_eod_close` sengaja **tidak menulis apa pun sebelum 16:00 WIB**
(`_boleh_tulis_close_index`). Dulu pra‑buka 08:45 & close Sesi I 12:00 ikut
memanggilnya, dan `backfill_index_eod_idx` menstempel barisnya
`captured_at = hari ini 16:00` — satu‑satunya jam yang dihitung sebagai close
oleh `_eod_fresh_for` & `check_eod_health`, sekaligus baris terbaru yang dibaca
dashboard (`order by captured_at desc`). Nilai pra‑buka/tengah hari pun tampil
sebagai "IHSG hari ini" dan mengalahkan snapshot polling sepanjang sisa sesi.
Sebelum close, biarkan snapshot polling yang isinya benar‑benar segar.

**Runbook — setelah `idx serve` mati lebih lama dari `IDX_BACKFILL_DAYS`**
(default 21 hari bursa): (1) `python -m scripts.check_eod_gaps --dry-run`
untuk melihat lubang, (2) `python -m scripts.check_eod_gaps` untuk menariknya
(jeda 4 s, backoff sabar), (3) `python -m scripts.repair_foreign_flow
--start-date … --end-date …` kalau cakupan kolom ikut timpang, (4) verifikasi
lewat `idx health`.

### API Berita
| Endpoint | Fungsi |
|---|---|
| `GET /api/news/latest?limit=7` | Judul terbaru Stockbit Snips, urut `published_at` desc |
| `GET /api/market/narration` | **Dihapus** (404) |

---

## CLI Commands
```powershell
python -m idx_scraper.cli snapshot                # one‑off fetch
python -m idx_scraper.cli serve                   # start scheduler
python -m idx_scraper.cli eod [YYYYMMDD]           # fetch specific EOD
python -m idx_scraper.cli seed [--days N]        # seed historic OHLCV from Yahoo
python -m idx_scraper.cli signals                 # list BUY/SELL signals
python -m idx_scraper.cli signal-log              # view signal history
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
* `scripts/backfill_index_history_yahoo.py` — isi riwayat harian IHSG (COMPOSITE) dari Yahoo untuk rentang tanggal yang bolong ke `index_summary_daily`; **tidak menyentuh** `index_quotes` dan tidak menimpa baris yang sudah ada (nilai resmi IDX tetap menang).
* `scripts/backfill_corp_actions.py`
* `scripts/backfill_raw_eod.py` — backfill EOD IDX resmi; berjeda 4 s (`--pace`) + deteksi blokir 403/429 (berhenti dengan tanggal resume, bukan salah hitung libur). Gunakan `--ulang` untuk menarik ulang tanggal yang sudah ada (upsert semua kolom).
* `scripts/check_eod_gaps.py` — cari hari kerja yang **sama sekali tanpa sesi** di `research.raw_eod` lalu tarik ulang per hari (satu transaksi per sesi; `--dry-run` untuk melihat lubang tanpa menyentuh IDX). Ini pencari **hari kosong, bukan pencari kolom kosong** — untuk kolom yang bolong pakai `repair_foreign_flow.py`. Hari libur bursa dan tanggal di luar jendela retensi IDX (±720 hari) bukan kegagalan (exit tetap 0).
* `scripts/repair_foreign_flow.py` — tambal kolom foreign NULL pada sesi yang **sudah ada**: tarik ulang per sesi (`--start-date/--end-date`, jeda `--pace` 4 s, `--limit` untuk uji cepat), tulis **hanya** `foreign_buy/sell/net` lewat penjaga `foreign_net is null` + `ingested_at=now()` (idempoten, OHLCV tak tersentuh, terbitkan versi `prices_pit` baru), sesi tanpa kolom NULL dilewati tanpa request IDX, dan blokir 403/429 berhenti dengan tanggal resume + exit 1.
* `scripts/backfill_eod_yahoo.py`
* `scripts/intraday_capture.py`
* `scripts/refresh_sector_map.py`
* `scripts/backfill_market_segment.py`

---

## Health‑Check & Monitoring
* `cli health` memeriksa **empat** hal dan berhenti dengan exit 1 kalau salah satu
  bermasalah (`idx health || kirim-alert` aman dipakai cron). Sesi hari ini yang
  belum terbit bukan "bermasalah": labelnya `SESI BERJALAN — belum jatuh tenggat
  17:30 WIB` dan exit code-nya 0, jadi cron tidak berbunyi tiap siang. Labelnya
  dipilih dari sebabnya: `TELAT` (lewat tenggat), `TERTINGGAL` (sesi lampau yang
  belum masuk, termasuk sesi Jumat yang hilang saat akhir pekan), `CEK KONFIGURASI`
  (`DATABASE_URL` tidak diset).
  1. **kesegaran** — hari bursa yang diharapkan vs tanggal EOD terbaru
  2. **cakupan kolom** — `foreign_*` bolong di `sessions` sesi terakhir
  3. **sesi separuh** — baris per sesi di bawah 50% median jendela (ciri insert tersela)
  4. **lubang sesi** — hari bursa tanpa baris di antara sesi pertama & terakhir

  Pemeriksa 3 melewati sesi hari ini: sesi yang sedang ditulis (jaring kedua baru
  menarik EOD sesi ini pukul 17:30) bukan kerusakan. Kalau sesi itu tetap kurang,
  cek sesi berikutnya yang mengadukan — kerusakan ketahuan dalam ≤1 sesi.
  (Pemeriksa 4 tidak butuh perlakuan itu: lubang hanya dihitung di dalam rentang
  sesi yang sudah ada, jadi sesi hari ini tak pernah bisa muncul di sana.)
* **"Tidak ada yang diperiksa" ≠ hijau.** Kalau `research.raw_eod` kosong atau
  `DATABASE_URL` tidak diset, pemeriksa 2–4 lolos tanpa suara; `idx health`
  menghitungnya bermasalah (exit 1, `CEK KONFIGURASI`/`TIMPANG`) dan job Telegram
  mengirim satu pesan "Penjaga data tidak memeriksa apa pun" (satu, bukan tiga —
  akar masalahnya sama).
* **Perbaikan "sesi separuh"**: `python -m scripts.backfill_raw_eod --start-date
  <tgl> --end-date <tgl> --ulang`. `--ulang` wajib — tanpa itu backfill melewati
  tanggal yang sudah ada, dan justru itulah definisi sesi separuh.
* Kartu "Kesehatan Data" di dashboard (`GET /api/data-health`) membaca pemeriksa
  yang sama, jadi cerita di layar dan alarm Telegram tidak pernah berbeda.
  Bedanya cuma penyajian: kartu menampilkan empat tingkat keparahan, CLI memakai
  exit code.
* Tiap pemeriksa di jalur alarm punya penangkap sendiri: satu yang melempar
  (DB putus) hanya kehilangan laporannya sendiri, alarm jalur lain tetap jalan.
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
* UI (`idx-web/web/src/lib/components/TickerLogo.svelte`) memakai logo lokal bila `ok`, selain itu monogram.
* **Hak cipta: [BELUM TERVERIFIKASI].** Logo milik masing-masing emiten dan TradingView. Gunakan hanya untuk tampilan internal; cek lisensi sebelum dipublikasikan.

---

## Glossary
* **EOD** – End‑of‑Day snapshot.
* **PIT** – Point‑In‑Time, bitemporal view without look‑ahead bias.
* **Regular market** – Trades during official IDX session (09:00‑16:00 WIB).
* **Non‑regular market** – After‑hours / pre‑market trades (captured via Yahoo fallback).
* **Chrome Transport** – Playwright‑Chrome process yang me‑reuse kuki Cloudflare. Cadangan opsional; jalur utama sekarang HTTP murni (`HttpTransport`).
* **Back‑fill** – Re‑compute historic aggregates after a schema or source change.

---

*Last updated: 2026‑10‑25*