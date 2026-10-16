# Deploy Produksi di Server Linux

Panduan lengkap memindahkan stack **market-labs** (idx-scraper + idx-web + PostgreSQL)
dari Windows ke server Linux. Semua angka di dokumen ini **terukur**, bukan perkiraan
(sumbernya disebut di tiap bagian).

Isi:
1. [Arsitektur & kenapa systemd](#1-arsitektur--kenapa-systemd)
2. [Spec server](#2-spec-server)
3. [Daftar persiapan](#3-daftar-persiapan)
4. [Database](#4-database)
5. [Environment variables](#5-environment-variables)
6. [Langkah deploy](#6-langkah-deploy)
7. [Unit & timer systemd (isi file)](#7-unit--timer-systemd-isi-file)
8. [nginx + TLS](#8-nginx--tls)
9. [Health check & monitoring](#9-health-check--monitoring)
10. [Backup & restore](#10-backup--restore)
11. [Update & rollback](#11-update--rollback)
12. [Runbook kegagalan](#12-runbook-kegagalan)
13. [Keamanan](#13-keamanan)
14. [Yang TIDAK dibawa dari Windows](#14-yang-tidak-dibawa-dari-windows)
15. [Checklist cutover](#15-checklist-cutover)

---

## 1. Arsitektur & kenapa systemd

Tiga proses, satu database:

| Proses | Perintah | Peran |
| --- | --- | --- |
| **Scheduler** | `python -m idx_scraper.cli serve` | Menarik data IDX (harga, broker, foreign flow, berita), menjalankan job EOD/refresh, mengirim alert Telegram |
| **API** | `uvicorn idx_scraper.api.app:app` | Melayani `/api/*` untuk frontend |
| **Web** | `next start` | UI Next.js; browser memanggil API dari sisi klien |

Scheduler harus **selalu hidup**. Di Windows jadwal EOD 16:10 hidup di dalam
prosesnya (APScheduler), jadi kalau prosesnya mati atau mesinnya tidur, jadwalnya
hilang begitu saja. Itu akar masalah berulang yang kita kejar.

### Unit vs timer — jawaban singkat

**Unit** = satu proses yang **diawasi** sistem. **Timer** = jam alarm yang menyalakan
sesuatu pada waktu tertentu (pengganti Task Scheduler).

Bedanya dengan Windows Task Scheduler, dan inilah alasan utamanya:

| | Windows Task Scheduler | systemd |
| --- | --- | --- |
| Proses mati | Tidak ada yang menghidupkan kembali | `Restart=always` menghidupkan dalam hitungan detik |
| Jadwal terlewat (mesin mati/sleep) | Hilang, kecuali `StartWhenAvailable` | `Persistent=true` menjalankan yang terlewat begitu mesin hidup |
| Mematikan layanan | `taskkill` manual; launcher mati → **proses jadi yatim** (Chrome menahan profil, EOD 0 baris) | `KillMode=control-group` membunuh **seluruh pohon proses** |
| Status | Sulit dilihat | `systemctl status`, `journalctl -u` |
| Log | File yang Anda tebak sendiri | Journal terpusat, otomatis |

Yatim proses itu yang dulu menghabiskan berhari-hari: `wscript` keluar segera,
`cmd.exe` + `python` tertinggal tak terkelola, Chrome-nya menahan profil `p3`.
Di systemd, satu proses tidak bisa lolos dari pengawasan begitu.

### Jadi timer dipakai untuk apa, kalau `serve` sudah punya jadwal sendiri?

Ini bagian yang jujur perlu diklarifikasi: **`idx serve` sudah menjalankan sendiri
EOD 16:10 di dalam APScheduler, plus jaring kedua `eod_fallback` yang dicek ulang
setiap jam 16:30–20:30.** Timer `idx-eod.timer` bukan pengganti, melainkan
**cadangan kalau `serve` sendiri yang mati** — satu-satunya lubang yang tidak bisa
ditutup dari dalam proses:

- `serve` mati sebelum 16:30 dan tidak hidup lagi hari itu (OOM, `systemctl stop`);
- mesin mati lebih lama daripada jendela backfill: startup `serve` menambal
  **5 hari bursa terakhir** (`IDX_BACKFILL_DAYS`), yang lebih tua dari itu tidak
  ditarik otomatis — pakai `idx eod <YYYYMMDD>`.

Karena `idx refresh --skip-if-fresh` **idempoten** (kalau data hari ini sudah lengkap,
ia langsung keluar), menjalankannya dua kali tidak berbahaya. Biayanya nol. Di
Windows, padanan timer ini dulu berupa task `IDX-EOD`, tapi **dihapus 15 Okt 2026**:
di sana jaring kedua sudah hidup di dalam `serve` (`eod_fallback` 16:30), jadi satu
penjadwal saja. Di systemd `serve` diawasi `Restart=always`, sehingga lubangnya jauh
lebih kecil — timer ini opsional.

---

## 2. Spec server

Angka di bawah berasal dari pengukuran langsung di mesin pengembangan
(14–15 Okt 2026) memakai `pg_database_size`, `pg_total_relation_size`, dan `count(*)`:

| Tabel | Baris | Ukuran | Laju |
| --- | --- | --- | --- |
| `public.index_quotes` | 405.490 | 119,6 MB | 45 indeks × ~360 poll/hari ≈ **16.200 baris/hari → 4,8 MB/hari → 1,14 GB/tahun** |
| `public.stock_quotes` | 216.984 | 73,8 MB | ~17.000 baris/hari → **5,8 MB/hari → 1,38 GB/tahun** |
| `research.raw_eod` | 243.652 | 89,4 MB | 963 emiten/hari → 0,4 MB/hari |
| `research.prices_pit` | 286.945 | 94,9 MB | 963 emiten/hari → 0,3 MB/hari |
| `research.broker_daily` | 880 | 0,2 MB | 88 firma/hari |
| `research.signal_log` | 90.143 | 20,2 MB | ~963/hari |

- **Total DB sekarang: 422 MB.** Proyeksi pertumbuhan tabel utama: **≈2,7 GB per tahun bursa** (245 hari).
- 2 tabel teratas (`index_quotes`, `stock_quotes`) menyumbang **>90%** pertumbuhan. Lihat [Retensi](#retensi-opsional).

### Rekomendasi

| Komponen | Minimum | Nyaman |
| --- | --- | --- |
| CPU | 2 vCPU | 2–4 vCPU |
| RAM | 2 GB | 4 GB (API pool + Node + Postgres) |
| Disk | 20 GB SSD | **40–60 GB SSD** (DB 3 thn ≈ 9 GB + backup + build Node + headroom) |
| OS | Debian 12 / Ubuntu 22.04+ | Debian 12 |
| Python | 3.11+ | 3.12 |
| Node.js | **20.9+** (syarat minimum resmi Next 16) | 20 LTS atau 22 LTS |
| PostgreSQL | 14+ | **16 atau 18** (dev memakai 18.6) |

> **Catatan jujur:** ini beban kerja satu pengguna. VPS 2 vCPU / 4 GB sudah lapang.
> Yang benar-benar menentukan bukan CPU, tapi **disk** (pertumbuhan tabel intraday)
> dan **keandalan jaringan** (throttle IDX).

---

## 3. Daftar persiapan

Urutkan begini; jangan lompat ke unit systemd sebelum 1–7 selesai.

1. **Server + akses SSH berbasis kunci** (matikan login password).
2. **Zona waktu WIB** — semua jadwal mengasumsikan waktu Jakarta:
   ```bash
   sudo timedatectl set-timezone Asia/Jakarta
   sudo timedatectl set-ntp true   # chrony/systemd-timesyncd: 16:10 harus tepat
   ```
3. **User non-root** untuk aplikasi:
   ```bash
   sudo useradd -r -m -d /srv/market-labs -s /bin/bash idx
   sudo mkdir -p /srv/market-labs && sudo chown idx:idx /srv/market-labs
   ```
4. **Paket sistem**: `git python3-venv postgresql nginx certbot python3-certbot-nginx`.
   `python3-dev` + `build-essential` hanya perlu kalau suatu paket tidak punya wheel
   siap pakai (dengan `psycopg[binary]`, biasanya tidak perlu).
5. **Kode** di `/srv/market-labs/idx-scraper` dan `/srv/market-labs/idx-web` (user `idx`).
6. **PostgreSQL** siap + database + user aplikasi ([§4](#4-database)).
7. **`.env`** terisi, `chmod 600`, milik `idx` ([§5](#5-environment-variables)).
8. **Skema SQL** dijalankan ([§4](#4-database)).
9. **Smoke test manual** (`idx health`, `uvicorn`, `npm run build`) sebelum dijadikan unit.
10. **Backup terjadwal** ([§10](#10-backup--restore)).
11. **Firewall**: hanya SSH + HTTP/HTTPS terbuka; port 8000 & 3000 **tidak** ([§13](#13-keamanan)).

---

## 4. Database

### Pilihan: Postgres lokal vs Supabase

| | Postgres lokal di server | Supabase (terkelola) |
| --- | --- | --- |
| Biaya | Sudah termasuk VPS | Berbayar di atas kuota gratis |
| Latensi dari API | Sangat rendah (unix socket) | Terganjal jaringan internet |
| Backup | Tanggung jawab Anda | Otomatis |
| Cocok untuk | **Rekomendasi untuk beban kerja ini** | Kalau tidak mau mengurus backup |

Keduanya memakai jalur kode yang **sama**: set `IDX_STORAGE=postgres`
(`supabase` juga diterima) + `DATABASE_URL`. Tidak ada perbedaan kode.

### Siapkan Postgres lokal

```bash
sudo -u postgres psql <<'SQL'
CREATE USER idx_app WITH PASSWORD 'GANTI_PASSWORD_KUAT';
CREATE DATABASE market_labs OWNER idx_app;
SQL
```

Tuning awal untuk VPS 4 GB (`/etc/postgresql/16/main/postgresql.conf`):

```ini
shared_buffers = 1GB
effective_cache_size = 3GB
work_mem = 16MB
maintenance_work_mem = 256MB
random_page_cost = 1.1        # SSD
```

Lalu verifikasi koneksi:
```bash
psql "postgresql://idx_app:PASSWORD@127.0.0.1:5432/market_labs" -c 'select 1'
```

### Jalankan skema (urutan penting)

Delapan berkas di `idx-scraper/sql/`. Urutan di bawah **diverifikasi dari isi
berkasnya**, bukan disalin dari daftar: `pit_functions.sql` membaca
`research.prices_pit`, dan `research_eod_meta.sql` meng-`ALTER` tabel yang sama —
keduanya menuntut `research_schema.sql` sudah dijalankan lebih dulu.

```bash
cd /srv/market-labs/idx-scraper
export PGPASSWORD='...'
PSQL="psql -h 127.0.0.1 -U idx_app -d market_labs -v ON_ERROR_STOP=1"

$PSQL -f sql/schema.sql                # tabel dasar: index_quotes, stock_quotes, stock_summary_daily, stock_daily, index_summary_daily, news_items
$PSQL -f sql/research_schema.sql        # skema research: raw_eod, prices_pit, broker_daily, ownership, corporate_actions, regime_daily, quarantine_eod
$PSQL -f sql/research_eod_meta.sql      # research.* jadi superset lossless (nama emiten, foreign flow)
$PSQL -f sql/pit_functions.sql          # fungsi as-of / bebas look-ahead (dipakai backtest)
$PSQL -f sql/intraday_schema.sql        # intraday_ticks (partisi bulanan) + view intraday_bars_5m
$PSQL -f sql/signal_log.sql             # research.signal_log
$PSQL -f sql/factor_ic_history.sql      # research.factor_ic_history
$PSQL -f sql/market_segment_daily.sql   # research.market_segment_daily
```

### Cek hasil: harus ada 23 objek

Terverifikasi di DB pengembangan — **6 objek `public`, 17 objek `research`**:

```
public    tabel   index_quotes, index_summary_daily, news_items,
                  stock_daily, stock_quotes, stock_summary_daily
research  tabel   broker_daily, corporate_actions, factor_ic_history,
                  market_segment_daily, ownership, prices_pit, quarantine_eod,
                  raw_eod, recommendation_log, regime_daily, signal_log
research  partisi intraday_ticks (+ intraday_ticks_2026_09, _2026_10)
research  view    latest_pit, prices_adjusted, intraday_bars_5m
```

> **`research.latest_pit` adalah VIEW, bukan tabel dan bukan matview.** Tidak ada
> yang perlu di-`REFRESH`. Ia membaca `research.prices_pit` dengan `DISTINCT ON
> (code, trade_date)` — jadi begitu EOD masuk, ia langsung segar. Ini juga yang
> dibaca API, sehingga "kesegaran data" = `max(trade_date)` di view ini.

Validasi cepat:
```bash
$PSQL -c "select max(trade_date), count(distinct code) from research.latest_pit"
# harapan setelah EOD: tanggal hari bursa terakhir, 963
```

### Retensi (opsional)

Kalau `index_quotes`/`stock_quotes` terasa terlalu cepat tumbuh (~1,1 dan 1,4 GB/tahun),
dua opsi paling praktis:

```sql
-- 1) Hanya simpan intraday 90 hari terakhir (EOD tetap utuh selamanya)
delete from public.index_quotes  where captured_at < now() - interval '90 days';
delete from public.stock_quotes  where captured_at < now() - interval '90 days';
```
2. Perpanjang `IDX_INDEX_INTERVAL` / `IDX_WATCHLIST_INTERVAL` (60s → 120s memotong
   baris `index_quotes` separuh, dan tabel ini sendirian menyumbang ~40% pertumbuhan).

Data EOD (`raw_eod`, `prices_pit`, `latest_pit`) **jangan** dihapus — itu sumber
kebenaran riset dan riwayatnya kecil (0,7 MB/hari berdua).

---

## 5. Environment variables

Semua dibaca dari `.env` di root `idx-scraper` (`WorkingDirectory` unit harus
menunjuk ke situ; `cli.py` memanggil `load_dotenv()` sendiri).

### Wajib

| Variabel | Contoh | Keterangan |
| --- | --- | --- |
| `IDX_STORAGE` | `postgres` | `sqlite` (default) / `postgres` / `supabase` |
| `DATABASE_URL` | `postgresql://idx_app:***@127.0.0.1:5432/market_labs` | `SUPABASE_DB_URL` juga diterima |
| `IDX_WATCHLIST` | `BBCA,BBRI,BMRI,TLKM,ASII` | daftar pantau |
| `IDX_TRANSPORT` | `http` | **`http` = tanpa Chrome.** Lihat §14 |

### Transport & pengambilan data

| Variabel | Default | Keterangan |
| --- | --- | --- |
| `IDX_IMPERSONATE` | `chrome124` | target fingerprint curl_cffi; satu-satunya tombol bila Cloudflare memperketat |
| `IDX_HTTP_COOLDOWN_SEC` | `600` | lama jalur HTTP diistirahatkan setelah ditolak, sebelum jatuh ke browser (hanya relevan di mode `auto`) |
| `IDX_MARKET_OPEN` / `IDX_MARKET_CLOSE` | `09:00` / `16:00` | jam bursa WIB |
| `IDX_INDEX_INTERVAL` | `60` | detik antar-poll indeks (penggerak utama ukuran `index_quotes`) |
| `IDX_WATCHLIST_INTERVAL` | `60` | detik antar-poll watchlist |
| `IDX_PROBE_INTERVAL` | `900` | detik antar-probe ketersediaan IDX |
| `NEWS_INTERVAL` | `900` | detik antar-tarik berita |
| `IDX_ALERT_INTERVAL` | `300` | detik antar-evaluasi alert |
| `IDX_LIVE_TOP` / `IDX_LIVE_DELAY_MS` | `60` / `2000` | jumlah emiten & jeda sapuan live capture |
| `IDX_HOLIDAYS` | (kosong) | daftar hari libur bursa, mis. `2026-12-25,2027-01-01` |
| `IDX_EOD_DEADLINE_HOUR` | `16` | jam (WIB) sejak kapan EOD hari ini dianggap "telat". **Harus ≤ jam job `health_eod` (16:45)**, kalau tidak alarmnya tidak pernah berbunyi |
| `IDX_BACKFILL_DAYS` | `5` | berapa hari bursa ke belakang yang dicek ulang saat `serve` start & tiap tick jaring sore (menambal lubang mesin yang tidur/off). `0` = matikan |

### API & integrasi

| Variabel | Default | Keterangan |
| --- | --- | --- |
| `IDX_API_CORS_ORIGINS` | `http://localhost:3000,http://localhost:3100` | origin frontend yang diizinkan; isi domain publik saat produksi |
| `IDX_API_POOL_MIN` / `IDX_API_POOL_MAX` | `1` / `10` | ukuran connection pool API |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | (opsional) | alert Telegram; tanpa ini alert hanya masuk log |
| `IDX_API_BASE` | `http://127.0.0.1:8000` | alamat API yang dipanggil scheduler untuk clear cache |
| `IDX_SQLITE_PATH` | `./data/idx.db` | hanya dipakai bila `IDX_STORAGE=sqlite` |

### Berkas status (opsional, default di CWD — biarkan saja kecuali perlu)

`IDX_ALERTS_PATH`, `IDX_ALERT_STATE`, `IDX_SMART_MONEY_STATE`,
`IDX_RECOMMENDATION_STATE`, `IDX_SIGNAL_STATE`, `IDX_ENV_FILE`, `IDX_SERVE_LOCK`.

Satu catatan untuk `IDX_SERVE_LOCK`: kalau tidak diisi, lokasinya
`$LOCALAPPDATA\idx-scraper\serve.lock` di Windows, atau
`~/.local/state/idx-scraper/serve.lock` di Linux. Kunci ini menolak `serve` kedua
(hindari dua proses berebut data). Di Linux dengan systemd praktis tidak akan
pernah bertabrakan, tapi biarkan aktif — ia murah dan menjaga dari salah ketik.

### Frontend (`idx-web/.env.local`)

| Variabel | Keterangan |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | **Di-inline saat `npm run build`, bukan saat runtime.** Lihat peringatan di §8 |

---

## 6. Langkah deploy

```bash
# 1) Kode (user idx)
sudo -u idx -H bash
cd /srv/market-labs
git clone <repo-url> idx-scraper
git clone <repo-url> idx-web

# 2) Python
cd /srv/market-labs/idx-scraper
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e ".[postgres,api]"
#   Tanpa Chrome: JANGAN pasang extra [browser].
#   docs/dossier PDF butuh reportlab -> sudah termasuk di extra [api].

# 3) Konfigurasi
cp .env.example .env 2>/dev/null || nano .env
chmod 600 .env
#   minimal: IDX_STORAGE, DATABASE_URL, IDX_WATCHLIST, IDX_TRANSPORT=http

# 4) Skema SQL (lihat §4)

# 5) Smoke test SEBELUM dijadikan service
.venv/bin/python -m idx_scraper.cli health
.venv/bin/python -m idx_scraper.cli refresh --skip-if-fresh
.venv/bin/python -m idx_scraper.cli eod 20261014     # tarik satu hari sebagai uji

# 6) Web
cd /srv/market-labs/idx-web
nano .env.local        # NEXT_PUBLIC_API_BASE_URL
npm ci
npm run build          # WAJIB sebelum start; env NEXT_PUBLIC_* dibaca di sini

# 7) Unit systemd (§7), nginx (§8), firewall (§13)
```

Kalau langkah 5 sudah mengembalikan data (bukan error koneksi/database), sisanya
soal mekanik.

---

## 7. Unit & timer systemd (isi file)

Empat berkas di `/etc/systemd/system/`. Ganti `idx-app` bila username berbeda.

### `/etc/systemd/system/idx-serve.service`

```ini
[Unit]
Description=IDX scraper scheduler (EOD, broker, foreign flow, news, alerts)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=idx
Group=idx
WorkingDirectory=/srv/market-labs/idx-scraper
Environment=PYTHONUNBUFFERED=1
Environment=IDX_TRANSPORT=http
ExecStart=/srv/market-labs/idx-scraper/.venv/bin/python -m idx_scraper.cli serve
Restart=always
RestartSec=10
# systemd default: KillMode=control-group -> seluruh pohon proses ikut mati.
# Inilah yang menghapus masalah "chrome yatim" khas Windows.
KillSignal=SIGTERM
TimeoutStopSec=30
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/idx-api.service`

```ini
[Unit]
Description=IDX API (FastAPI)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=idx
Group=idx
WorkingDirectory=/srv/market-labs/idx-scraper
Environment=PYTHONUNBUFFERED=1
ExecStart=/srv/market-labs/idx-scraper/.venv/bin/uvicorn idx_scraper.api.app:app \
          --host 127.0.0.1 --port 8000 --workers 2 --timeout-keep-alive 30
Restart=always
RestartSec=5
TimeoutStopSec=30
MemoryMax=1G

[Install]
WantedBy=multi-user.target
```

> `--host 127.0.0.1` sengaja: API **tidak** boleh menghadap internet langsung.
> Akses publik lewat nginx ([§8](#8-nginx--tls)).

### `/etc/systemd/system/idx-web.service`

```ini
[Unit]
Description=IDX web (Next.js)
After=network-online.target idx-api.service
Wants=network-online.target

[Service]
Type=simple
User=idx
Group=idx
WorkingDirectory=/srv/market-labs/idx-web
Environment=NODE_ENV=production
Environment=PORT=3000
Environment=HOSTNAME=127.0.0.1
ExecStart=/usr/bin/npm run start
Restart=always
RestartSec=5
# Next.js membutuhkan drain 10-30s untuk menyelesaikan request & callback `after()`
KillSignal=SIGTERM
TimeoutStopSec=30
MemoryMax=1G

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/idx-eod.service` + `idx-eod.timer` (opsional)

Jaring kedua EOD sudah hidup di dalam `serve` (`eod_fallback`, dicek tiap jam
16:30–20:30) — jadi timer ini hanya perlu kalau Anda ingin cadangan untuk kasus
`serve` mati. Kalau pasang, jadwalkan di **16:30** (jam yang sama dengan cek
pertama di dalam `serve`).

```ini
# idx-eod.service
[Unit]
Description=IDX EOD refresh (cadangan kalau serve mati)
After=network-online.target postgresql.service

[Service]
Type=oneshot
User=idx
Group=idx
WorkingDirectory=/srv/market-labs/idx-scraper
Environment=PYTHONUNBUFFERED=1
Environment=IDX_TRANSPORT=http
ExecStart=/srv/market-labs/idx-scraper/.venv/bin/python -m idx_scraper.cli refresh --skip-if-fresh
TimeoutStartSec=30min
```

```ini
# idx-eod.timer
[Unit]
Description=Jadwal EOD IDX: Senin-Jumat 16:30 WIB (cadangan)

[Timer]
# 16:30 = cek pertama job `eod_fallback` di `idx serve`. Karena `--skip-if-fresh`
# idempoten, menjalankannya bersamaan dengan serve tidak menggandakan kerja.
OnCalendar=Mon-Fri 16:30:00
# Baris tambahan: padanan "diulang tiap jam sampai 20:30". Berguna kalau mesin
# tidur/baru hidup malam; hapus kalau tidak perlu.
OnCalendar=Mon-Fri 17:30:00
OnCalendar=Mon-Fri 18:30:00
OnCalendar=Mon-Fri 19:30:00
OnCalendar=Mon-Fri 20:30:00
# Jalankan yang terlewat begitu mesin hidup kembali (padanan StartWhenAvailable)
Persistent=true
AccuracySec=30s
Unit=idx-eod.service

[Install]
WantedBy=timers.target
```

Aktifkan:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now idx-serve idx-api idx-web
sudo systemctl enable --now idx-eod.timer

systemctl status idx-serve --no-pager
systemctl list-timers idx-eod.timer --no-pager
journalctl -u idx-serve -f          # log langsung
```

> **`Environment=` di unit menang atas `.env`.** Aplikasi memuat `.env` dengan
> `python-dotenv`, yang secara default **tidak** menimpa variabel yang sudah ada di
> lingkungan proses. Jadi `Environment=IDX_TRANSPORT=http` di unit akan mengalahkan
> nilai di `.env` — berguna saat menguji, tapi jangan sampai keduanya berbeda tanpa
> Anda sadari. Pilih satu sumber kebenaran.

> **Idempoten, jadi aman berulang.** `--skip-if-fresh` memeriksa dulu apakah EOD
> saham dan close indeks hari ini sudah ada; kalau sudah, ia keluar tanpa menulis
> apa pun. Karena itu `serve` dan timer boleh sama-sama menjadwalkan 16:30.

> **Catatan:** `OnCalendar` memakai zona waktu sistem. Pastikan
> `timedatectl set-timezone Asia/Jakarta` sudah dijalankan, atau tulis
> `OnCalendar=Mon-Fri 16:10:00 Asia/Jakarta` secara eksplisit.

---

## 8. nginx + TLS

**Peringatan penting sebelum menyalin contoh konfigurasi apa pun:**

`NEXT_PUBLIC_API_BASE_URL` di-**inline ke bundel JavaScript saat `npm run build`**.
Artinya nilai itu dibaca oleh **browser pengguna**, bukan oleh server. Jadi:

- `http://localhost:8000` hanya benar kalau Anda mengakses web dari mesin server itu sendiri.
- Kalau web dibuka dari komputer lain, `localhost` menunjuk ke komputer Anda — bukan server.

Dua pola yang benar:

**A. Satu domain, API diproksi (paling sederhana, tanpa urusan CORS).**
Set `NEXT_PUBLIC_API_BASE_URL=""` (string kosong) saat build. Kode di
`lib/api.ts` memakai `?? "http://localhost:8000"`, dan karena string kosong bukan
*nullish*, hasilnya `""` → semua panggilan jadi **relatif** ke origin yang sama,
lalu nginx meneruskannya ke API.

```nginx
server {
    listen 80;
    server_name market.example.com;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        # Next.js streaming: jangan buffer
        proxy_buffering off;
    }
}
```

**B. Dua domain, API terpisah.** Build dengan
`NEXT_PUBLIC_API_BASE_URL=https://api.example.com`, dan isi
`IDX_API_CORS_ORIGINS=https://market.example.com` agar CORS mengizinkan origin web.

TLS:

```bash
sudo certbot --nginx -d market.example.com
```

> Untuk pemakaian pribadi, API publik sebaiknya dilindungi (Basic Auth di nginx atau
> batasi IP). Data dan endpoint ini tidak dirancang untuk dibuka bebas.

---

## 9. Health check & monitoring

Tiga lapis, dari yang paling murah:

1. **API**: `curl -s http://127.0.0.1:8000/health` → harus `{"status":"ok"}`.
2. **CLI**: `.venv/bin/python -m idx_scraper.cli health` → memeriksa kesegaran EOD,
   jam deadline, dan koneksi DB. Cocok untuk timer harian.
3. **Kesegaran data**: ini yang paling bermakna.
   ```sql
   select max(trade_date) as sesi_terakhir,
          count(distinct code) as emiten
   from research.latest_pit;
   ```
   Setelah EOD hari bursa, harapan: tanggal hari ini, **963** emiten. Kalau tertinggal
   sehari, itu alarm yang sebenarnya.

Monitoring yang layak dipasang:

- **systemd**: `Restart=always` sudah menangani crash. Pantau jumlah restart:
  `systemctl show idx-serve -p NRestarts`.
- **log**: `journalctl -u idx-serve --since today -p warning` untuk memantau
  `[transport] HTTP ditolak` dan `[warn] IDX ... status 429`.
- **uptime eksternal**: panggil `/health` dari luar (mis. cron di mesin lain atau
  layanan uptime) — jangan dari server yang sama, supaya kegagalan server tetap terdeteksi.
- **Telegram**: sudah terpasang di aplikasi (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`);
  alert sinyal dan aturan pantauan otomatis masuk ke sana.

---

## 10. Backup & restore

Database adalah satu-satunya hal yang tidak bisa dipulihkan dari `git`.

### Backup harian

`/etc/cron.daily/idx-backup` (atau systemd timer terpisah):

```bash
#!/bin/bash
set -euo pipefail
DIR=/srv/backup/idx
mkdir -p "$DIR"
STAMP=$(date +%F-%H%M)
sudo -u postgres pg_dump -Fc market_labs > "$DIR/market_labs-$STAMP.dump"
# retensi: 7 harian + 4 mingguan
find "$DIR" -name 'market_labs-*.dump' -mtime +30 -delete
```

Isi `~/.pgpass` untuk kredensial (mode `600`), jangan taruh password di skrip.

### Restore — wajib diuji, bukan diasumsikan

```bash
createdb -U idx_app market_labs_restore
pg_restore -U idx_app -d market_labs_restore -Fc /srv/backup/idx/market_labs-2026-10-15-0000.dump
psql -U idx_app -d market_labs_restore -c 'select count(*) from research.raw_eod'
```

Uji restore **sekali sebelum mengandalkannya**, dan sesekali sesudahnya. Backup yang
belum pernah direstore hanyalah harapan.

Opsi tambahan bila ingin kehilangan data mendekati nol: aktifkan WAL archiving
(`archive_mode=on`) atau `pg_basebackup` mingguan. Untuk kebutuhan pribadi, dump
harian sudah memadai.

---

## 11. Update & rollback

```bash
# Update
sudo -u idx -H bash -c 'cd /srv/market-labs/idx-scraper && git pull && .venv/bin/pip install -e ".[postgres,api]"'
sudo -u idx -H bash -c 'cd /srv/market-labs/idx-web && git pull && npm ci && npm run build'
sudo systemctl restart idx-serve idx-api idx-web

# Skema berubah? jalankan berkas SQL yang relevan dulu (idempoten, pakai IF NOT EXISTS).
```

Rollback: kode ini tidak punya artefak versi seperti image Docker, jadi rollback =
kembali ke commit sebelumnya.

```bash
git log --oneline -5
git checkout <commit-sebelumnya>      # atau: git revert <commit-bermasalah>
sudo systemctl restart idx-serve idx-api idx-web
```

Aturan penting: **cetak commit yang sedang dipakai produksi** setiap kali deploy
(`git rev-parse --short HEAD` → catat), supaya rollback tidak menebak.

---

## 12. Runbook kegagalan

| Gejala | Cek | Tindakan |
| --- | --- | --- |
| EOD 0 baris | `journalctl -u idx-serve \| grep -i -E 'status (403\|429)\|ditolak'` | Throttle/penolakan IDX. Jalankan `idx refresh --skip-if-fresh`; bila 403 menetap, naikkan `IDX_IMPERSONATE` (mis. `chrome131`) |
| `idx health` bilang EOD tertinggal | `select max(trade_date) from research.latest_pit` | Hari libur bursa? isi `IDX_HOLIDAYS`. Bukan? jalankan `idx refresh --skip-if-fresh` |
| Serve mati berulang | `systemctl status idx-serve`, `NRestarts` | Lihat `journalctl -u idx-serve -n 100`. Sering OOM → naikkan RAM atau `MemoryMax` |
| `serve` menolak start: "instance lain sedang berjalan" | `ls -l ~/.local/state/idx-scraper/serve.lock` | Kunci aktif. Cari proses `idx_scraper.cli serve` lain, atau (bila yakin tak ada) hentikan unit lalu start ulang |
| Web 502 | `systemctl status idx-web`, `journalctl -u idx-web` | `npm run build` belum dijalankan, atau `NEXT_PUBLIC_API_BASE_URL` salah saat build |
| API 500 di banyak endpoint | `curl 127.0.0.1:8000/health`, cek Postgres | DB mati → `systemctl restart postgresql` |
| Timer tidak menyala | `systemctl list-timers \| grep idx` | Zona waktu salah (`timedatectl`), mesin tidur, atau `Persistent=true` menjalankan terlewat saat boot |
| Disk penuh | `df -h`, `select pg_size_pretty(pg_database_size(current_database()))` | Jalankan [retensi](#retensi-opsional) untuk tabel intraday |
| Database lambat | `vacuumdb --analyze --all` | Statistik basi (di Windows ini pernah membuat `n_live_tup` salah 100×) |

---

## 13. Keamanan

- **Firewall**: hanya `22` (SSH), `80`, `443`. Jangan pernah buka `8000`/`3000`:
  ```bash
  sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
  ```
- **SSH**: kunci saja (`PasswordAuthentication no`), `PermitRootLogin no`.
- **Layanan tidak jalan sebagai root** — unit di atas memakai `User=idx`.
- **Secrets**: `.env` dan `~/.pgpass` mode `600`, milik `idx`. Jangan pernah masuk `git`.
- **CORS**: produksi → `IDX_API_CORS_ORIGINS=https://market.example.com` (bukan `*`).
- **API publik**: batasi dengan Basic Auth/IP allowlist; endpoint ini tidak punya autentikasi sendiri.
- **Update rutin**: `sudo apt update && sudo apt upgrade`, terutama Postgres & nginx.
- **Hak cipta data**: lihat catatan di README — data & logo milik sumbernya, untuk pemakaian pribadi.

---

## 14. Yang TIDAK dibawa dari Windows

Semua ini bisa ditinggalkan di Windows, dan **sebaiknya**:

| Windows | Di Linux |
| --- | --- |
| `scripts/preflight-chrome.ps1` | Tidak perlu — tidak ada Chrome |
| `scripts/run-hidden.vbs`, semua `.bat` | Diganti unit systemd |
| `IDX_CHROME_PROFILE_DIR=C:\Users\...` | **Jangan disalin**; di Linux jadi folder sampah. Pakai `IDX_TRANSPORT=http` |
| Kuki `cf_clearance` di profil `p3` | Tidak berlaku di IP/server baru — dan tidak dibutuhkan |
| Task Scheduler (`IDX-Serve`, `IDX-API`, `IDX-Web` — Sen-Jum 08:45, `S4U`, jalan tanpa login) | `idx-serve.service`, `idx-api.service`, `idx-web.service` (+ `idx-eod.timer` opsional) |
| Task `IDX-Stack-Daily` (dihapus 16 Okt 2026) | Duplikat tiga task di atas: aksinya sama (serve/api/web), tapi `InteractiveToken` — tidak jalan tanpa sesi login dan diblokir saat laptop pakai baterai (`DisallowStartIfOnBatteries=true`). Yang hilang hanya watchdog 30 menit; di Linux itu tugas `Restart=always` |
| Task `IDX-EOD` (dihapus 15 Okt 2026) | Tidak ada padanannya — jaring kedua EOD kini hidup di dalam `serve` (`eod_fallback` 16:30), jadi tidak perlu penjadwal kedua |
| `scripts/guard-port.ps1` (penjaga port launcher) | Tidak perlu — satu unit per port + `Restart=always` sudah menjamin satu instance |
| Playwright / Google Chrome | Hanya bila memakai `IDX_TRANSPORT=browser` atau `auto` yang jatuh ke browser. Untuk jalur HTTP, **tidak dipasang sama sekali** |

Alasannya ada di [README → IDX & Cloudflare](README.md#idx--cloudflare-tanpa-browser):
curl_cffi murni sudah menembus endpoint IDX — diuji 23/23 permintaan 200 selama
45 menit, tanpa kuki, tanpa browser, tanpa satu pun proses Chrome hidup. Jadi server
Linux ini bisa benar-benar tanpa Chrome, tanpa Xvfb, tanpa profil persisten.

> **Mengaktifkan kode baru di Windows (tanpa admin).** `serve` berjalan di konteks
> task tersimpan (session 0), jadi prosesnya **tidak bisa dihentikan dari shell
> biasa** ("access denied") dan `cmd` juga tidak bisa append ke `_serve.log` selama
> handle proses lama masih hidup. Jalannya: buat `_restart-serve.flag`, lalu
> `Start-ScheduledTask -TaskName IDX-Serve` — `scripts/restart-serve.ps1` (dipanggil
> `start-serve.bat`) menghentikan serve lama, menunggu log bebas, baru menyalakan
> yang baru. Tanpa flag, `start-serve.bat` tidak menghentikan apa pun, jadi
> penjadwalan harian tetap aman. Jejaknya di `_restart-serve.log`. Di Linux tidak
> perlu: `sudo systemctl restart idx-serve`.

> **Satu batasan yang jujur:** `scripts/fetch_broker_logos.py` (aset logo, di luar
> pipeline data) masih memakai `BrowserTransport`. Kalau ingin memperbarui logo di
> server tanpa Chrome, jalankan skrip itu dari mesin yang punya Chrome, lalu salin
> hasilnya.

---

## 15. Checklist cutover

```
[ ] timedatectl: zona Asia/Jakarta, NTP aktif
[ ] user idx + /srv/market-labs, kode ter-clone
[ ] venv + pip install -e ".[postgres,api]"  (TANPA extra browser)
[ ] PostgreSQL jalan, DB + user dibuat, tuning dasar
[ ] 8 berkas SQL dijalankan; 23 objek ada (6 public, 17 research)
[ ] .env mode 600, IDX_STORAGE=postgres, DATABASE_URL, IDX_WATCHLIST, IDX_TRANSPORT=http
[ ] idx health -> tidak ada keluhan
[ ] idx refresh --skip-if-fresh -> EOD hari bursa terakhir masuk, 963 emiten
[ ] npm ci && npm run build di idx-web (NEXT_PUBLIC_API_BASE_URL sudah benar)
[ ] 4 unit terpasang & enabled (timer EOD opsional)
[ ] systemctl status idx-serve idx-api idx-web -> active (running)
[ ] journalctl -u idx-serve | grep eod_fallback -> job jaring kedua terdaftar (16:30-20:30)
[ ] journalctl -u idx-serve | grep catchup -> startup: "EOD hari ini sudah lengkap" + tanggal lampau yang dicek ulang
[ ] (kalau timer dipasang) systemctl list-timers idx-eod.timer -> Sen-Jum 16:30 (+17:30..20:30)
[ ] nginx + certbot; https://domain/ tampil; https://domain/api/health menjawab
[ ] ufw: hanya 22/80/443
[ ] backup harian jalan; RESTORE DIUJI sekali
[ ] pantau 16:10 & 16:30 hari bursa pertama: journalctl -u idx-serve -f -> rantai sukses, lalu "EOD hari ini sudah lengkap"
[ ] max(trade_date) di research.latest_pit = hari bursa terakhir
[ ] catat commit produksi: git rev-parse --short HEAD
```

---

*Dokumen ini ditulis 15 Okt 2026. Angka DB (§2) hasil pengukuran langsung pada
PostgreSQL 18.6; spesifikasi Next.js 16 (§8) mengikuti `node_modules/next/dist/docs/`.*
