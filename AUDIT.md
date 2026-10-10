# Audit Trail — Market Labs (idx-scraper & idx-web)

Tanggal audit: **2026-09-26**
Cakupan: seluruh perubahan pada working tree kedua repo yang belum masuk commit,
ditambah jejak perbaikan kualitas yang dilakukan pada sesi audit ini.

Status akhir: **ruff bersih · 161 test pytest lulus · tsc --noEmit bersih ·
next build sukses (14 route)**.

---

## 1. Ringkasan fitur (perubahan yang dibawa sesi sebelumnya)

### 1.1 Backend — `idx-scraper`

**File baru (untracked):**

| File | Fungsi |
|---|---|
| `src/idx_scraper/alert_rules.py` | Kosakata aturan pantauan (harga naik/turun, RSI, lonjakan volume) + logika evaluasi murni + latch anti-spam `RuleState` (re-arm setelah kondisi normal kembali). Tanpa I/O DB agar dipakai bersama API dan CLI worker. |
| `src/idx_scraper/alerts_store.py` | CRUD daftar aturan ke `data/alerts.json` (JSON + tulis atomik temp-file→replace). Config pengguna sengaja di file, bukan DB — API tetap read-only. |
| `src/idx_scraper/api/alerts.py` | Service aturan: validasi payload, dedup aturan identik, evaluasi live terhadap layer research Postgres (indikator & snapshot sama persis dengan yang dilihat dashboard). |
| `src/idx_scraper/api/simulation.py` | Lapisan API simulator: katalog strategi + default biaya, validasi, dan orkestrasi `run_backtest`. Tidak ada persistensi — murni komputasi. |
| `src/idx_scraper/api/sector_lookup.json` | Layer pemetaan emiten→sektor hasil generate (Yahoo sector/industry), menutupi hampir seluruh emiten yang punya harga. |
| `scripts/refresh_sector_map.py` | Generator `sector_lookup.json` + laporan coverage. |
| `tests/test_alert_rules.py`, `tests/test_alerts_store.py`, `tests/test_backtest_costs.py`, `tests/test_backtest_rebalance.py`, `tests/test_sector_map.py`, `tests/test_simulation.py` | Suite pengujian untuk semua fitur di atas. |

**File berubah:**

| File | Perubahan |
|---|---|
| `src/idx_scraper/api/app.py` | Registrasi endpoint: `CRUD `/api/alerts` (+`/test`), `GET /api/backtest/config`, `POST /api/backtest/run`, helper `_parse_optional_date` (422 untuk format tanggal salah). |
| `src/idx_scraper/api/schemas.py` | +341 baris: skema alert, backtest (request/config/result/ledger), quality tambahan. |
| `src/idx_scraper/backtest.py` | Mesin backtest point-in-time: model biaya nyata (komisi/sisi, pajak jual, slippage, cap partisipasi terhadap nilai transaksi harian), frekuensi rebalance `daily/weekly/monthly` (di antara rebalance posisi dibiarkan drift), ledger per rebalance (trade, harga eksekusi, fee, posisi akhir), pemisahan metrik bruto vs neto + benchmark buy & hold. |
| `src/idx_scraper/api/sector_map.py` | Dua layer pemetaan sektor: `SECTOR_MAP` kurasi manual (selalu menang) → `sector_lookup.json` hasil generate → fallback `"Lainnya"`. Vocabulary bucket kanonik (`BUCKETS`), `sector_for()`, `coverage()`. Menghapus ketergantungan pada map manual yang hanya menutup ~25% emiten. |
| `src/idx_scraper/api/analytics.py` | Migrasi pemakaian sektor dari `SECTOR_MAP.get(...)` ke `sector_for(...)` (analisis sektor + RRG). |
| `src/idx_scraper/cli.py` | Alert aturan pantauan masuk loop `serve` dan `cli alerts`: `run_rule_alerts()` mengevaluasi aturan via service layer API dan mengirim ke Telegram hanya yang baru terpicu (latch `RuleState`). Kegagalan DB/config tidak mematikan scheduler. |
| `src/idx_scraper/notify.py` | `format_rule_message()` — format pesan Telegram untuk aturan yang terpicu. |
| `.env.example` | Variabel baru: `IDX_ALERTS_PATH`, `IDX_ALERT_STATE`. |
| `pyproject.toml` | Ignore ruff `BLE001` untuk `scripts/*` (penangkapan exception lebar disengaja pada job backfill). |
| `README.md` | Dokumentasi endpoint baru + penjelasan model biaya & ledger rebalance. |

### 1.2 Frontend — `idx-web`

**File baru (untracked):**

| File | Fungsi |
|---|---|
| `app/pantau/page.tsx` | Halaman Pantau: watchlist + chart + pembuat aturan + status Telegram dalam satu alur. |
| `app/backtest/page.tsx` | Simulator: pilih strategi & parameter, periode, modal, frekuensi rebalance, preset biaya (tanpa biaya / standar IDX / konservatif) + knob manual. Menampilkan metrik bruto vs neto vs buy & hold, dampak biaya, ledger rebalance, dan penjelasan cara membaca hasil. |
| `components/AlertRules.tsx` | UI CRUD aturan pantauan (tipe, ambang, catatan) dengan status live per aturan. |
| `components/TelegramStatus.tsx` | Cek koneksi bot + kirim pesan uji; panduan setup bila env belum diisi. |
| `components/EquityCurveChart.tsx` | Chart kurva ekuitas + benchmark. |
| `components/RebalanceLedger.tsx` | Tabel trade & posisi per sesi rebalance (bukti churn). |

**File berubah:** `lib/types.ts` (mirror skema backend untuk alert/backtest),
`lib/api.ts` (`runBacktest`, `fetchBacktestConfig`, `createAlert`, `deleteAlert`,
`testTelegram`), `lib/hooks.ts` (hook SWR untuk semua endpoint baru),
`components/Sidebar.tsx` (nav Pantau), `README.md`.

---

## 2. Perbaikan kualitas (sesi audit 2026-09-26)

Temuan: `ruff check src tests scripts` = **18 error** → diselesaikan semua
(sebagian autofix `I001`/`UP037`/`F401`/`RUF100`, sisanya manual dengan alasan):

| Lokasi | Masalah | Perbaikan & alasan |
|---|---|---|
| `src/idx_scraper/client.py:350` | `B023` closure tidak mengikat `today` + `PLR0124` `v == v` | `_f` diberi default arg `today=today` (binding eksplisit per iterasi); NaN guard diganti `math.isnan(v)` — lebih benar daripada `v != v` (juga menangani +inf dengan benar). `import time` tak terpakai dihapus. |
| `src/idx_scraper/cli.py:225` | `DTZ007` `strptime` tanpa tz | `.replace(tzinfo=WIB)` sebelum `.date()` — eksplisit bahwa tanggal EOD adalah tanggal bursa WIB; tidak mengubah hasil. |
| `src/idx_scraper/cli.py:244` | Lambda disimpan ke variabel (`E731`, noqa basi) | Diubah ke `def _ohl(v)` dengan komentar; perilaku identik. |
| `src/idx_scraper/cli.py:40` | `SOURCE_YAHOO` diimpor tak terpakai | Dihapus dari import. |
| `src/idx_scraper/backtest.py:36` | `UP035` import typing lama | `Callable, Iterable` pindah ke `collections.abc`. |
| `src/idx_scraper/backtest.py:142` | `PYI034` `__enter__` tidak mengembalikan `Self` | Return type `typing.Self` (py311+). |
| `src/idx_scraper/api/services.py:467` | `RUF046` `int(round(x))` redundan | `round(x)` — `round` tanpa ndigits sudah mengembalikan `int`. |
| `src/idx_scraper/api/quality.py:10` | `date` diimpor tak terpakai | Dihapus. |
| `src/idx_scraper/cf_transport.py` (4 lokasi) | `RUF100` noqa `BLE001` tidak berlaku | Directive dihapus; penanganan exception tetap sama (memang disengaja, dan `BLE001` tidak aktif untuk file ini). |
| `scripts/backfill_raw_eod.py:55` | `DTZ005` `datetime.now()` tanpa tz | `datetime.now(WIB)` dengan `WIB = timezone(timedelta(hours=7))` — backfill harus mengikuti tanggal bursa, bukan tz lokal mesin. |
| `src/idx_scraper/cli.py` (2 blok import), `src/idx_scraper/client.py` | `I001` urutan import | Autofix ruff. |

**Non-perubahan yang disengaja:** `client.py:351` guard NaN versi lama di jalur
indeks (jika ada) tidak diubah di luar cakupan; perilaku scraping tidak
disentuh sama sekali.

---

## 3. Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `pytest` (backend, 161 test) | ✅ semua lulus |
| `ruff check src tests scripts` | ✅ bersih |
| Import sanity semua modul yang diedit + `py_compile` scripts | ✅ |
| `tsc --noEmit` (frontend) | ✅ bersih |
| `next build` (frontend) | ✅ sukses, 14 route |
| Verifikasi runtime 2026-09-26: API hidup, CRUD alert (create→duplikat 422→delete→404), endpoint `POST /api/backtest/run` (240 hari bursa, 13 rebalance, drag 1,73%) | ✅ semua 200/422/404 sesuai harapan |
| Verifikasi browser (headless Chrome): halaman `/pantau`, `/backtest` ter-render lengkap dengan data | ✅ |

---

## 4. Kondisi operasional & risiko yang diketahui

1. **Telegram opsional** — aturan tetap tersimpan & dievaluasi tanpa token;
   pengiriman butuh `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` di `idx-scraper/.env`
   (cek lewat halaman Pantau → "Kirim pesan uji").
2. **Data historis harus di-seed** agar backtest dan sinyal bermakna:
   `python -m idx_scraper.cli eod` lalu `... seed`, dan backfill 1 tahun via
   `scripts/backfill_raw_eod.py` bila perlu.
3. **`sector_lookup.json` adalah artefak generate** — regenerasi via
   `scripts/refresh_sector_map.py` bila universe emiten berubah; test akan gagal
   bila muncul bucket baru yang belum terdaftar di `BUCKETS`.
4. **Kedua repo belum meng-commit perubahan** — tree berisi semua yang terdaftar
   di dokumen ini. Disarankan commit terpisah per topik (alerts / dividends /
   backtest costs / sector map / lint hygiene).
5. **Rahasia**: `.env` kedua repo sengaja tidak di-commit; jangan pernah
   menambahkan isinya ke dokumen atau commit.

## 5. Rekomendasi lanjutan

- ✅ CI (GitHub Actions) sudah ditambahkan 2026-09-26: `idx-scraper/.github/workflows/ci.yml`
  (ruff + pytest, Python 3.11) dan `idx-web/.github/workflows/ci.yml`
  (`npm ci` + tsc + next build, Node 22) — berjalan di push `main` dan semua PR.
- Endpoint `/api/sectors` bisa mengekspos `coverage()` agar kualitas pemetaan
  sektor terpantau dari dashboard.
- Batas `max_codes`/`max_days` backtest sudah ada di config; pertimbangkan
  caching hasil run identik untuk menghemat komputasi bila UI sering re-run.

## 6. Fitur setengah jadi yang diselesaikan (2026-09-26, sesi lanjutan)

**Broker/aliran dana proxy** — endpoint `/api/stocks/{code}/brokers`
(`analytics.get_broker_summary`), skema `StockBrokerSummary`, tipe TS, dan hook
`useStockBrokers` sudah ada tetapi **tidak ada satu pun komponen frontend yang
menampilkannya** (dead API). Diselesaikan:

| File | Perubahan |
|---|---|
| `src/idx_scraper/api/schemas.py` | `BrokerRow` +field `estimated: bool = True` — docstring service menjanjikan `"estimated": true` tapi skema & return dict belum memilikinya (inkonsistensi). |
| `src/idx_scraper/api/analytics.py` | Kedua baris proxy (FOREIGN aggregate, DOMESTIC bid/offer) kini menyertakan `"estimated": True` sesuai janji docstring. |
| `idx-web/lib/types.ts` | `BrokerRow.estimated: boolean` mengikuti skema backend. |
| `idx-web/components/BrokerSummary.tsx` | Komponen baru: dua kolom (sisi beli/jual) + disclaimer jujur bahwa angka adalah estimasi, bukan data broker asli. |
| `idx-web/app/stock/[code]/page.tsx` | `BrokerSummary` dipasang di kolom kanan halaman detail emiten (antara sinyal & riwayat). |
| `idx-web/app/globals.css` | Helper `.text-warn` (dipakai disclaimer). |

Verifikasi: ruff bersih · pytest 161 lulus · tsc bersih · next build 14 route ·
runtime (API :8011 + web :3100) — endpoint mengembalikan `estimated: true` dan
kartu ter-render di `/stock/ASLI` dengan disclaimer.

---

## 7. Riset keputusan jual/beli — jejak sinyal & sentimen (2026-09-28)

Dua fitur pertama dari peta jalan riset keputusan. Prinsip yang dipegang:
hanya memakai data yang sudah tersimpan, point-in-time, dan mengukur — bukan
menambah klaim.

### 7.1 Jejak Sinyal (track record sinyal)

Sinyal BUY/SELL sebelumnya tidak pernah diukur kinerjanya. Sekarang setiap
sinyal dicatat lalu dinilai di close T+1 — konsisten dengan IC analysis dan
backtest.

| File | Fungsi |
|---|---|
| `src/idx_scraper/research/signal_log.py` | Baru. `compute_signal_rows` (pure, PIT, filter warmup + likuiditas + SELL-palsu-ex-dividend), `backfill`/`record_recent` (recompute rentang dalam satu transaksi), `attach_outcomes` (fwd/mfe/mae/abnormal vs pasar), `summarize_outcomes`/`evaluate`, CLI `python -m idx_scraper.research.signal_log`. |
| `sql/signal_log.sql` | Baru. DDL `research.signal_log` (PK `code, trade_date`, idempoten). |
| `tests/test_signal_log.py` | Baru, 17 test: invariant vs `signal_series`, warmup, no-look-ahead, guard dividen, gate likuiditas, aritmetika fwd/MFE/MAE/abnormal, agregasi. |
| `src/idx_scraper/api/analytics.py` | `get_signal_track()` — backfill malas bila log kosong, cache 1 jam. |
| `src/idx_scraper/api/schemas.py` | `SignalTrack`, `SignalTrackStat`, `SignalTrackRecent`. |
| `src/idx_scraper/api/app.py` | `GET /api/signals/track`. |
| `src/idx_scraper/cli.py` | Subcommand `idx signal-log`; job harian `_job_signal_log` 16:25 WIB (setelah regime 16:20, window 1 bulan). |
| `idx-web/app/jejak-sinyal/page.tsx` | Halaman baru: ringkasan, kinerja per sinyal (pilih horizon), per regime, sinyal terbaru. |
| `idx-web/lib/{types,hooks}.ts`, `components/Sidebar.tsx` | Tipe, `useSignalTrack`, menu "Jejak Sinyal". |

**Temuan temuan data nyata** (18 bulan backfill di DB lokal): 84.331 sinyal
(34.443 BUY / 49.888 SELL). BUY h5: hit 42%, mean +0,32%, abnormal +0,30%
(t=3,9); BUY h21 mean −0,44%. SELL h21: mean +0,58% tapi abnormal −0,23% —
artinya SELL tidak lebih buruk dari pasar secara material. Ini justru output
yang diinginkan: sinyal kini punya angka, bukan asumsi. Catatan jujur: sebagian
besar sinyal ber-regime "TIDAK DIKETAHUI" karena `regime_daily` baru terisi
belakangan — akan terisi sendiri seiring histori regime memanjang.

### 7.2 Sentimen aliran & buku (`/sentimen`)

Belum ada lapisan sentimen sama sekali di repo. Alih-alih menambah sumber
berita, versi ini mengekstrak "sentimen" dari jejak transaksi yang sudah ada.

| File | Fungsi |
|---|---|
| `src/idx_scraper/research/sentiment.py` | Baru. Skor emiten `-1..+1` dari **persentil cross-sectional arus asing** + ketimpangan buku + absorption; gauge pasar `0..100` dari breadth harga + IHSG + breadth arus asing; `load_latest` memilih hari EOD terakhir yang cukup lengkap. |
| `tests/test_sentiment.py` | Baru, 21 test: persentil, pemetaan komponen, renormalisasi bobot, band label, gauge pasar (risk-on/off), agregasi `build()`. |
| `src/idx_scraper/api/analytics.py` | `get_sentiment()` — memakai `_latest_orderbook_factors()` yang sudah ada, cache 1 jam. |
| `src/idx_scraper/api/schemas.py`, `app.py` | `SentimentResponse` + `GET /api/sentiment` (param `limit`). |
| `idx-web/app/sentimen/page.tsx` | Halaman baru: gauge pasar, statistik, dua daftar sorotan (akumulasi/distribusi) dengan alasan per emiten. |
| `idx-web/lib/{types,hooks}.ts`, `components/Sidebar.tsx` | Tipe, `useSentiment`, menu "Sentimen" di grup Flow. |

**Dua koreksi desain yang muncul dari data nyata** (keduanya karena memeriksa
DB, bukan berasumsi):

1. **Ambang absolut salah kalibrasi.** `foreign_net / value` di IDX sangat kecil
   (p50 ≈ −0,008%, |p90| ≈ 0,1%). Skala tetap membuat semua emiten "NETRAL"
   (833 dianalisis, 0 sorotan). Diganti persentil cross-sectional — bebas
   konstanta ajaib dan sebanding antar emiten, idiom yang sama dengan
   `research/composite.py`.
2. **Hari EOD terakhir bisa parsial.** Pada 2026-09-25 seluruh baris punya
   `value`/`foreign_net` NULL, sehingga `max(trade_date)` menghasilkan sentimen
   kosong. `load_latest` kini memilih hari terakhir yang minimal separuh
   barisnya bernilai, dan jatuh ke 2026-09-24 pada data sekarang.

Sumber: arus asing (`research.latest_pit`), order-book (`research.orderbook`
dari snapshot `stock_quotes`), breadth. **Tidak ada scraping baru** dan tidak
ada look-ahead: snapshot intraday hari T memang sudah tertutup pada close T.

### 7.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src tests scripts` | ✅ bersih |
| `pytest` (backend) | ✅ 289 lulus (161 sebelum sesi ini) |
| `tsc --noEmit` | ✅ bersih |
| `next build` | ✅ sukses, 18 route (dari 14) |
| Runtime nyata (uvicorn :8124 + Postgres berisi 232k baris) | ✅ `GET /api/signals/track` & `GET /api/sentiment` 200, payload lengkap; backfill 84.331 baris; `/jejak-sinyal` & `/sentimen` ter-render |

### 7.4 Sisa peta jalan (belum dikerjakan)

1. `/keputusan` — Ruang Keputusan: satu verdict per emiten (sinyal + komposit +
   base rate event + regime + sentimen) dengan level pembatalan.
2. `/sentimen` lanjutan — sentimen berita (RSS/leksikon/LLM), dijadikan
   penyesuai kecil seperti lapisan faktor, bukan penentu utama.
3. `/event` pasar, Screener jadi Signal Screener, perluasan aturan alert,
   `/risiko` (sizing & stop).

Catatan operasional: job jejak sinyal dan tabel `research.signal_log` baru
muncul setelah `idx serve`/`idx signal-log` dijalankan; endpoint tetap aman
(backfill malas) bila tabel belum ada.

## 8. Jejak Smart Money — verdict per emiten, radar, pola, alert (2026-10-01)

Menjawab satu pertanyaan pengguna: **"saat buka emiten X, apakah pemain besar
sedang masuk/keluar hari ini dan sejak kapan?"** Semua berbasis data yang
sudah ada (`research.latest_pit`) — **tidak ada scraping baru, tidak ada
look-ahead** (verdict memakai jendela 10 sesi yang sudah tertutup).

"Smart money" di sini = **agregat investor asing** (IDX menggabungkan semua
firma asing; tak ada breakdown per-firma per-saham di data gratis) + pola
harga/volume. Dilabeli jujur sebagai proksi jejak, bukan rekomendasi.

### 8.1 Modul pure & kalibrasi

`src/idx_scraper/smart_money.py` (pure, tanpa DB): `verdict()` (akumulasi /
distribusi / netral dari **porsi nilai transaksi** yang dibelani asing),
`detect_patterns()` 4 pola klasik (akumulasi diam-diam, distribusi saat naik,
inisiasi volume, distribusi diam-diam) via `pattern_flags()` sebagai
**single source of truth**, `consolidation_range()` (level invalidasi),
`build_narrative()` / `build_alert_message()` (plain-language), `build_radar()`
(rank |net rupiah| + lantai likuiditas), `collect_pattern_episodes()` +
`pattern_track_record()` (track record + alpha vs pasar).

Ambang **dikalibrasi dari data nyata**, bukan tebakan: rentang 10 sesi p25
≈ 9,9% → "tenang" < 10%; netval10 p50 ≈ 2% / p75 ≈ 6,2% (bermakna ≥ 5%);
aliran besar ≥ 16% (p90); spike volume ≥ 2× rata-20 (p90); lantai likuiditas
radar Rp 500 Jt (di atas p10).

### 8.2 Lapisan API & UI

| Endpoint | Isi |
|---|---|
| `GET /api/smart-money/radar?days=` | Papan akumulasi & distribusi se-pasar (rank rupiah, lantai likuiditas) |
| `GET /api/stocks/{code}/smart-money` | Verdict + pola + level + narasi + konteks sektor per emiten |
| `GET /api/smart-money/track-record` | Track record 4 pola (120 sesi): % berfungsi, alpha vs pasar, t-stat |
| `GET /api/smart-money/verdicts?codes=` | Verdict per emiten watchlist (kartu halaman Pantau) |

Arsitektur tetap mengikuti konvensi repo: **pure → service (`analytics.py`,
cache in-process) → endpoint + Pydantic → TS types + SWR hook → komponen**.
Frontend murni presentasi; tak ada kalkulasi di web.

Halaman/komponen baru (`idx-web`): `/radar` + `/radar/[code]`,
`SmartMoneyBanner` (di atas detail emiten), `SmartMoneyTrackRecordCard`,
`SmartMoneyWatchCard` (Pantau), entri sidebar "Radar Smart Money".

### 8.3 Alert plain-language (Telegram)

Worker `run_smart_money_alerts()` (`cli.py`) dipanggil dari refresh pipeline
EOD (`do_eod=True`) dan `idx alerts`. Alur: service read-only → latch
`SmartMoneyState` (`smart_money_state.py`) → `format_smart_money_message()` →
Telegram. Anti-spam sama seperti `SignalState`/`RuleState`:

- **Baseline dulu**: run pertama menyimpan kondisi tiap kode tanpa mengirim
  (mencegah banjir — watchlist ~199 emiten).
- Alert **sekali per transisi bermakna**: netral→akumulasi, netral→distribusi,
  akumulasi↔distribusi. Kembali netral tidak dibunyikan (re-arm).

### 8.4 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check` (berkas sentuh) | ✅ bersih |
| `pytest` (backend) | ✅ 437 lulus (34+ baru utk smart money & state) |
| `tsc --noEmit` | ✅ bersih |
| Runtime nyata (uvicorn :8000 + Postgres) | ✅ radar/track-record/verdicts/banner 200, payload lengkap |
| Track record (120 sesi, 1243 kejadian) | ✅ Inisiasi alpha +1,87% (t=3,5); Distribusi diam-diam berfungsi 56% |
| Alert dry-run (watchlist nyata) | ✅ baseline 0 event → 199 kode ter-seed; simulasi flip = 1 event, pesan terbaca |

### 8.5 Sisa peta jalan

1. **Broker tape per saham** (per-firma per-emiten) — macet: tidak ada di
   sumber gratis IDX. Opsi: scrape pihak ketiga / keterbukaan info afiliasi.
2. **Alert per-atas-jendela** (mis. akumulasi 20 sesi), bila baseline 10 sesi
   terlalu sensitif di lapangan.
3. **Rebalancing bobot track record** bila jumlah episode per pola membesar.

## 9. Broker tape per saham — ditutup; diganti "Pemilik & Aksi Pemilik" (2026-10-01)

### 9.1 Hasil penyelidikan (empiris, bukan asumsi)

`GetBrokerSummary` (sumber `research.broker_daily`) diuji dengan parameter
per-saham: `?stockCode=BBCA` dan `?emitenCode=BBCA` mengembalikan **88 baris
identik** dengan tanpa parameter — parameternya **diam-diam diabaikan**.
`GetBrokerSummaryByStock` → **503** (tak ada). Halaman broker IDX sendiri hanya
menampilkan tabel 88 firma **seluruh pasar**. Kesimpulan: **IDX tidak
menyediakan tape broker per saham di endpoint publik gratis** (menguatkan
catatan README). Jalur berbayar yang ada: Index Alpha (5 request/hari gratis,
mulai Rp 200rb/bln, data dari 2025-01-01), idxalpha.com (Rp 350rb/bln),
Stockbit PRO (Bandar Detector, Rp 200rb/bln).

### 9.2 Yang dikerjakan sebagai gantinya (gratis)

`/primary/ListedCompany/GetCompanyProfilesDetail?KodeEmiten=` ternyata
mengembalikan **`PemegangSaham`** per emiten (nama, kategori, lembar, persen,
flag `Pengendali`) — gratis, pakai transport yang sudah ada. Fitur baru:
**Pemilik & Aksi Pemilik**.

- `ownership.py` (pure): `parse_shareholders`, `free_float_pct`,
  `controller_names`, `owner_changes` (diff dua snapshot -> tambah/kurang/
  baru/keluar, agregat masyarakat & treasury dikecualikan).
- `ownership_store.py`: tabel `research.ownership` (snapshot per tanggal) +
  upsert idempoten.
- `client.fetch_company_profile`; CLI `idx ownership [--codes|--all|--date]`.
- `analytics.get_stock_ownership` + `GET /api/stocks/{code}/ownership`.
- UI: kartu **Pemilik & Aksi Pemilik** di detail emiten (free float, pengendali,
  daftar pemilik, perubahan porsi).

**Snapshot, bukan deret waktu**: IDX hanya memberi komposisi terkini, jadi
"aksi pemilik" muncul setelah ada dua snapshot di tanggal berbeda — jalankan
`idx ownership` berkala (mis. bulanan, sejalan laporan KSEI).

### 9.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check` (berkas sentuh) | ✅ bersih |
| `pytest` (backend) | ✅ lulus (+12 test ownership) |
| `tsc --noEmit` | ✅ bersih |
| Runtime nyata | ✅ `idx ownership --codes ...` -> 70 baris (4/6 emiten; 2 kena 403 transien, dilewati) |
| Endpoint | ✅ `GET /api/stocks/BMRI/ownership` 200 — free float 40,46%, pengendali terisi |
| Diff aksi pemilik (2 snapshot) | ✅ Danantara +1,48% (tambah), INA −0,60% (kurang), PT lama (keluar) |

### 9.4 Sisa peta jalan

1. ~~**Snapshot berkala otomatis**~~ — **selesai (2026-10-02)**: job
   `ownership_weekly` (Senin 06:45 WIB, `_job_ownership`) snapshot watchlist ke
   `research.ownership`; helper `snapshot_ownership()` dipakai bersama CLI &
   scheduler, jadwal via `_next_weekday_at()` (interval 1 minggu).
2. **Free float sebagai penyebut** aliran asing (net asing ÷ free float) —
   ukuran institusional yang lebih tajam dari ÷ nilai transaksi.
3. **Tape broker per saham** tetap butuh vendor berbayar (lihat 9.1).

### 9.5 Update data 2026-10-02

| Pemeriksaan | Hasil |
|---|---|
| EOD saham 2026-10-01 & 2026-10-02 | ✅ `raw_eod` + `prices_pit` s/d 2026-10-02 (963 emiten) |
| Close resmi IHSG | ✅ COMPOSITE 6.036,888 (2026-10-02) di `index_summary_daily` + `index_quotes` |
| Broker summary | ✅ `broker_daily` 2026-09-30 & 2026-10-02 (88 firma) |
| Jejak sinyal | ✅ `signal_log` s/d 2026-10-02 |
| Regime | ✅ `regime_daily` 2026-10-02 TRENDING_DOWN (ADX 31,9) |
| IC | ✅ `factor_ic_history` run 2026-10-02 (bobot kosong — belum lolos gate) |
| Ownership | ✅ snapshot 2026-10-02 (ANTM/BMRI/EMAS/TLKM), pembanding 2026-10-01 |

### 9.6 Rework lint & pelengkap skema (2026-10-03)

`ruff check src tests scripts` (gerbang CI) dilaporkan **7 error** — ternyata
semua **pra-ada** (bukan dari fitur ownership; diverifikasi dengan menjalankan
ruff pada `be6af66` dan berkas historis). Dibersihkan semua:

| Berkas | Kode | Perbaikan |
|---|---|---|
| `src/idx_scraper/cli.py` | `C408` | `dict(...)` → dict literal (job `_refresh`). |
| `src/idx_scraper/cli.py` | `F541` | Hapus prefiks `f` pada print catchup tanpa placeholder. |
| `scripts/backfill_index_eod_idx.py` | `UP037` | Hilangkan kutip anotasi return (file sudah `from __future__ import annotations`). |
| `scripts/backfill_index_eod_idx.py` | `S110` | Tutup sesi IDX `try/except/pass` memang best-effort → tambah `S110` ke per-file-ignores `scripts/*.py` (sejajar `BLE001`/`S112` yang sudah ada). |
| `src/idx_scraper/research/broker_activity.py` | `UP035` | `Iterable` pindah ke `collections.abc`. |
| `tests/test_broker_activity.py` | `I001` | Autofix urutan import (`BREADTH_THRESHOLD` sebelum `BROKER_FACTORS`). |
| `tests/test_broker_activity.py` | `F841` | `hist_a` ternyata memang tak terpakai → tambahkan asersi `[50.0, 50.0]` (tes jadi menguji winner **dan** loser sesuai namanya). |

Pelengkap skema: DDL `research.ownership` kini juga ada di
`sql/research_schema.sql` (bagian 9) — sebelumnya hanya di
`ownership_store.py`, padahal docstring-nya menjanjikan mirror.

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src tests scripts` | ✅ bersih (0 error) |
| `pytest` (backend) | ✅ lulus semua |
| `tsc --noEmit` + `next build` (frontend) | ✅ bersih, 22 route |
| `GET /api/stocks/BMRI/ownership` | ✅ 200 — snapshot 2026-10-02 vs 2026-10-01 |

## 10. Pola jadi klaim yang bisa dicek: horizon + "sejak kapan" (2026-10-03)

### 10.1 Masalah (dari data nyata, bukan dugaan)

Label pola di banner emiten ("Distribusi diam-diam") tampil tanpa angka, dan
`SmartMoneyTrackRecordCard` cuma dipasang di `/radar` + `/radar/[code]` —
**bukan di `/stock/[code]`**, halaman yang dibuka orang awam. Jadi pembaca
menyimpulkan sendiri jangka waktunya. Track record se-pasar menunjukkan
simpulan itu justru keliru:

| Pola | Arah | 5 hari | 10 hari | 21 hari |
|---|---|---|---|---|
| `silent_accumulation` | beli | 40,7% ⚠️ | 45,5% | **55,4%** |
| `initiation` | beli | 51,5% | 53,4% | **55,6%** |
| `distribution_on_rally` | jual | **57,0%** | 53,2% | 51,0% |
| `silent_distribution` | jual | **63,6%** | 58,0% | 51,1% |

*(aligned hit rate = berapa sering arah harga sesuai pola, 120 sesi terakhir)*

Dua cacat: (a) **horizon tidak seragam** — akumulasi baru terbaca di 21 hari dan
justru lebih buruk dari koin di 5 hari, distribusi sebaliknya; (b) "sejak kapan"
cuma integer `streak`, tanpa tanggal.

### 10.2 Yang dikerjakan

- `smart_money._streak` mengembalikan tanggal sesi pertama streak; `verdict`
  menambah `streak_start_date`; narasi menyebut "net sell sejak 18 Sep 2026".
- `smart_money.pattern_history` (pure): memilih **horizon terbaik** =
  `aligned_hit_rate` tertinggi dengan sampel >= `MIN_HISTORY_N` (30 episode
  terealisasi) — bukan rata-rata semua horizon, justru karena horizon tidak
  seragam. `reliable=False` bila tak ada horizon yang lolos ambang (UI wajib
  bilang "sampel kecil").
- `smart_money.patterns_history` + `pattern_evidence_sentence` (pure): satu
  kalimat bukti per pola. **Arah harga diambil dari `direction` pola, bukan dari
  besarnya angka** — `aligned_hit_rate` sudah searah-pola, jadi menyimpulkan arah
  dari rate akan membalik artinya (pola jual 64% terbaca "harga naik"). Bug ini
  ditangkap unit test sebelum sampai produksi.
- `analytics.get_stock_smart_money` menempelkan `history` ke tiap pola yang
  muncul dan meneruskannya ke `build_narrative`. Track record di-cache 1 jam;
  bila pengambilannya gagal, banner tetap tampil tanpa klaim historis.
- Skema: `SmartMoneyVerdict.streak_start_date`, `SmartMoneyPattern.history`
  (+ `SmartMoneyPatternHistory`, `SmartMoneyHorizonStats`).
- UI banner emiten: badge "cerita N hari" + "N% sesuai arah · n=…" per pola, dan
  streak jadi "11× berturut / sejak 18 Sep 2026". Persentase diberi warna netral,
  bukan hijau/merah — pola jual yang terbukti bukan kabar baik, jadi warnanya
  menyatakan kekuatan catatan, bukan arah pasar.

### 10.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src/ tests/` | ✅ bersih |
| `pytest tests/` | ✅ 468 lulus (test_smart_money.py 48 → 64) |
| `tsc --noEmit` | ✅ bersih |
| Endpoint BMRI (distribusi) | ✅ `streak_start_date=2026-09-18`; `silent_distribution` → horizon **5 hari**, 64%, n=484, reliable |
| Endpoint EMAS (akumulasi) | ✅ `streak_start_date=2026-09-25`; `silent_accumulation` → horizon **21 hari**, 55%, n=213, reliable |
| Browser `/stock/BMRI` | ✅ "sejak 18 Sep 2026" · "cerita 5 hari" · "64% sesuai arah · n=484" · kalimat bukti tampil di narasi |

### 10.4 Sisa peta jalan

1. **Verdict relatif pasar** — hari ini 589 emiten net sell vs 374 net buy (dari
   963). Verdict per emiten perlu konteks "61% pasar juga net sell" supaya tidak
   dibaca sebagai sinyal eksklusif. Konteks sektor sudah ada, pasar belum.
2. **Bobot tampilan ikut kekuatan bukti** — `initiation` punya alpha terkuat
   (+1,09% / +2,02% / +2,58% pada 5/10/21 hari, t=2,3–3,5); pola dengan bukti
   terkuat layak tampil lebih dulu, bukan diperlakukan sama rata.
3. **Data tipis yang membatasi fitur**: `broker_daily` baru 6 hari bursa
   (2026-09-25..10-02) sehingga tren broker belum bisa diklaim; intraday praktis
   kosong (`intraday_ticks` 528 baris, `intraday_ticks_2026_10` 0 baris) — fitur
   intraday tidak boleh dijanjikan sebelum pengumpulannya dibereskan.

## 11. Verdict dalam konteks pasar + pola diurut ikut kekuatan bukti (2026-10-03)

### 11.1 Masalah

1. **Verdict tanpa pembanding.** BMRI/BBRI/GOTO semuanya "distribusi besar" di
   hari yang sama. Tanpa angka pasar, pembaca menyimpulkan itu ciri khas
   masing-masing emiten — padahal bisa jadi cuma cermin pasar.
2. **Semua pola diperlakukan sama.** Track record menunjukkan kekuatannya tidak
   sama: `initiation` punya alpha terkuat (+1,09% / +2,02% / +2,58% pada
   5/10/21 hari, t=2,3–3,5), sementara `silent_accumulation` bahkan negatif di
   5 hari. Urutan tampil (dan pola mana yang jadi kalimat utama narasi)
   sebaiknya ikut bukti, bukan ikut urutan pemeriksaan di kode.

### 11.2 Yang dikerjakan

- `smart_money.market_breadth` (pure): sebaran verdict SELURUH pasar di jendela
  yang sama, memakai `verdict` yang sama dengan jalur per-emiten dan **tanpa
  lantai likuiditas** (beda dari `build_radar` yang memotong emiten tipis untuk
  papan peringkat) — supaya penyebutnya jujur. Menyediakan dua penyebut:
  `*_pct` terhadap semua emiten ber-data, dan `distributing_share` terhadap
  emiten yang **punya arah** (netral & data kurang keluar) — pembanding yang
  benar untuk sebuah verdict.
- `smart_money.market_context_sentence` (pure): kalimat plain-language, dan
  verdict yang **searah mayoritas pasar dinyatakan apa adanya** ("…jadi ini
  belum tentu ciri khas emiten ini") — menyembunyikannya akan membuat fitur
  terdengar lebih pintar dari kenyataan.
- `analytics.get_smart_money_breadth` (cache 30 menit, panel pasar yang sama
  dengan radar) + `market` di payload `SmartMoneyStock`; kalimatnya masuk narasi
  sebagai butir 5, angka mentahnya (X dibuang vs Y ditimbun) jadi baris
  "Konteks pasar" di banner.
- `smart_money.pattern_history` menambah `confidence` (dari |t| **terbaik** lintas
  horizon yang sampelnya >= `MIN_HISTORY_N`; ambang 2 = tinggi, 1 = sedang),
  `best_tstat`, dan `edge_pct` (|effective_alpha|). Sengaja dipisah dari horizon
  terpilih: "seberapa sering arahnya benar" dan "seberapa yakin ini bukan
  kebetulan" adalah dua pertanyaan berbeda — dan horizon dengan hit rate lebih
  rendah bisa justru lebih meyakinkan.
- `smart_money.rank_patterns` (pure): pola ber-catatan selalu di depan pola tanpa
  catatan ("belum ada bukti" ≠ "terbukti lemah"), lalu keyakinan, lalu besar
  efek; stabil pada urutan deteksi untuk kunci yang sama.
- UI: baris "Konteks pasar: N dibuang vs M ditimbun — X% dari yang punya arah
  sedang dibuang" + "keyakinan tinggi/sedang/lemah" pada tiap pola.

### 11.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src/ tests/` | ✅ bersih |
| `pytest tests/` | ✅ 483 lulus (test_smart_money.py 64 → 79) |
| `tsc --noEmit` | ✅ bersih |
| Breadth pasar (live) | ✅ 963 emiten ber-data: 152 dibuang / 112 ditimbun dari 264 yang punya arah → `distributing_share` 57,6% |
| BMRI (distribusi) | ✅ narasi: "searah mayoritas pasar — 58% emiten ber-verdict juga sedang dibuang asing, jadi ini belum tentu ciri khas emiten ini" |
| EMAS (akumulasi) | ✅ narasi: "melawan arus pasar — hanya 42% emiten ber-verdict yang sedang ditimbun asing" |
| Keyakinan pola (live) | ✅ BMRI `silent_distribution` keyakinan **tinggi** (\|t\|max 3,33, edge 0,44); EMAS `silent_accumulation` keyakinan **tinggi** (\|t\|max 3,98, edge 0,48) |
| Browser `/stock/BMRI` | ✅ baris konteks pasar + "64% sesuai arah · n=484 · keyakinan tinggi" ter-render |
| Urutan pola (live) | ⚠️ **tidak teruji hari ini**: 0 dari 963 emiten memicu >=2 pola. Historis: 35 dari 1.252 hari-emiten (2,8%), hampir selalu `initiation` + `silent_accumulation`. Logikanya ditutup 3 unit test (`rank_patterns`) |

### 11.4 Sisa peta jalan

1. **Data tipis** (masih): `broker_daily` 6 hari bursa, intraday kosong,
   `ownership` 4 emiten — lihat 10.4 butir 3 dan 9.4.
2. **`initiation` belum dipromosikan di level halaman** — ia pola dengan bukti
   terkuat, tapi baru muncul kalau volumenya melonjak; papan "pola terkuat hari
   ini" di `/radar` akan membuatnya terlihat tanpa menunggu.
3. **`confidence` belum dipakai di kartu track record `/radar`** — masih hanya di
   banner emiten; menyatukan keduanya akan menghapus dua cara membaca angka yang
   sama.

## 12. Papan "pola terkuat hari ini" di `/radar` (2026-10-03)

### 12.1 Masalah

`initiation` adalah pola dengan bukti terkuat (alpha +1,09% / +2,02% / +2,58%
pada 5/10/21 hari, t=2,3–3,5) tapi paling jarang menyala — hari ini hanya 1
emiten dari 963. Pola per emiten juga cuma terlihat kalau kebetulan membuka
emiten itu, dan papan `/radar` yang ada hanya memeringkat emiten per |net|,
bukan per pola. Jadi pola terkuat praktis tak pernah terlihat.

### 12.2 Yang dikerjakan

- `smart_money.patterns_board` (pure): pola yang menyala di sesi terakhir,
  **dikelompokkan per pola** — angka historisnya (persen sesuai arah, horizon,
  keyakinan) memang milik pola, bukan milik emiten; daftar rata per emiten akan
  menuliskan statistik yang sama puluhan kali (66 emiten `silent_distribution`
  hari ini) dan menyembunyikan pesannya. Urutan kelompok ikut kekuatan bukti;
  tanpa catatan track record selalu di belakang.
- Emiten di dalam kelompok urut |net rupiah|, dengan **lantai likuiditas yang
  sama dengan radar** (Rp 500 jt nilai transaksi jendela) supaya nama nyaris tak
  diperdagangkan tidak menempel di papan. `fired` (semua yang menyala) dan
  `count` (yang lolos lantai) dikirim terpisah, dan UI menuliskan sisanya
  terang-terangan — bukan disembunyikan.
- `analytics.get_smart_money_patterns_board` (cache 30 menit, panel pasar yang
  sama dengan radar + track record yang sudah di-cache 1 jam) + endpoint
  `GET /api/smart-money/patterns`.
- UI: kartu **Pola Terkuat Hari Ini** di `/radar`, sebelum kartu track record.

### 12.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src/ tests/` | ✅ bersih |
| `pytest tests/` | ✅ 489 lulus (+6 test `patterns_board`) |
| `tsc --noEmit` | ✅ bersih |
| Endpoint live | ✅ `GET /api/smart-money/patterns` 200 — 4 kelompok, 963 emiten dipindai, sesi 2026-10-02 |
| Urutan kelompok | ✅ `initiation` (edge 2,58%) → `silent_accumulation` (0,48%) → `distribution_on_rally` (0,45%) → `silent_distribution` (0,44%) |
| `fired` vs `count` | ✅ `silent_accumulation` 32 → 25, `silent_distribution` 66 → 53 (sisanya disaring lantai, dan disebut di UI) |
| Emiten urut | ✅ `silent_distribution`: BMRI −Rp 1,81 T → BBRI −Rp 889 M → BBCA −Rp 432 M |
| Browser `/radar` | ✅ kartu ter-render: "4 pola menyala · 963 emiten dipindai", badge "cerita N hari", "keyakinan tinggi", catatan emiten yang disaring |

### 12.4 Catatan tinjauan: `confidence` di kartu track record — sengaja TIDAK disatukan

Rencana awal (10.4 butir 3 / 11.4 butir 3) adalah menyatukan kata keyakinan di
kartu track record `/radar`. Setelah kartunya dibaca, ternyata ia **sudah** punya
kolom "Kepercayaan" — dan itu **berbeda hal dengan benar**: kartu itu per
horizon (pengguna memilih 5/10/21 sesi), jadi keyakinannya diturunkan dari
t-stat **horizon itu**; sedangkan `confidence` di banner adalah ringkasan
level-pola dari |t| **terbaik lintas horizon**. Menyatukannya justru akan
menyesatkan (pola dengan t=1,4 di 5 sesi dan t=2,8 di 21 sesi memang "sedang"
untuk pertanyaan 5 sesi). Keduanya dibiarkan, dan tidak ada perubahan di kartu.

### 12.5 Sisa peta jalan

1. **Data tipis** (masih, dan ini yang paling membatasi): `broker_daily` 6 hari
   bursa, intraday kosong, `ownership` 4 emiten.
2. **Papan pola belum bisa klik ke jejak emiten** — chip emiten mengarah ke
   `/stock/{code}`; `/radar/[code]` (jejak lengkap + konteks sektor) lebih tepat
   untuk alur ini.
3. **Pola per emiten belum punya halaman sendiri** — `detect_patterns` sudah
   dipakai per emiten, tapi tak ada halaman yang mendaftar "semua emiten yang
   pernah memicu pola ini" beserta hasil sesudahnya.

## 13. `max(trade_date)` paralel di `raw_eod` — error intermiten yang bukan soal slot (2026-10-06)

### 13.1 Masalah

`uvicorn.err.log` menyimpan **satu** kegagalan, dari 2026-10-04 23:47:27, yang
tidak pernah terulang — dan justru kedatangannya sekali itu yang membuat gejalanya
gampang salah dibaca:

    psycopg.errors.ObjectNotInPrerequisiteState: parallel worker failed to initialize

Statement yang gagal ikut tercatat di log server: `select max(trade_date) as d
from research.raw_eod` — baris pertama `_leaders_eod` (`api/services.py:305`),
yang dipanggil dashboard **tiga kali serentak** (`metric=volume`, `value`,
`frequency`). Tanpa dilacak, gejala ini paling gampang ditutup dengan menaikkan
`max_worker_processes` — padahal itu **tidak akan memperbaikinya** (13.2).

### 13.2 Yang dikerjakan

- **Melacak jalur errornya di sumber Postgres.** Di `access/transam/parallel.c`
  (REL_18_STABLE) pesan ini hanya muncul di `WaitForParallelWorkersToAttach`
  (baris 759) dan `WaitForParallelWorkersToFinish` (baris 878), dengan komentar
  sumbernya: *"the postmaster was unable to fork the worker or it exited without
  initializing properly"*. Jadi worker **berhasil didaftarkan lalu mati sebelum
  attach** — bukan query yang ditolak sejak awal.
- **Membuang teori kehabisan slot.** `parallel.c:617` menyatakan gagal registrasi
  memang sengaja **tidak** dijadikan error — *"there is no need to throw an error
  here if registration fails"*, ditambah penanda `any_registrations_failed` —
  sehingga query lanjut dengan worker lebih sedikit **secara diam**, bukan gagal.
  Karena itu menaikkan `max_worker_processes` bukan perbaikan untuk error ini.
- **Membuang teori bug Windows `inherited socket`** (BUG #18168 gejalanya identik:
  SQLSTATE 55000 di Windows). Event Log provider `PostgreSQL` di mesin ini tidak
  punya event error handle/socket pada jam kejadian — hanya info startup.
- **Menghapus pemicunya.** `research.raw_eod` cuma punya index `(id)` dan
  `(code, trade_date, ingested_at DESC)`, jadi `max(trade_date)` tidak bisa
  dilayani index dan jatuh ke **parallel seq scan** 87 MB / 236.911 baris dengan
  **2 worker**. Dashboard memanggil `_leaders_eod` 3× serentak (3 × 2 worker)
  sementara pool efektif cuma **7** (`max_worker_processes = 8` minus satu slot
  `logical replication launcher` yang dipegang sejak startup) — tabrakan spawn
  proses inilah yang membuat kejadiannya intermiten.
- `CREATE INDEX CONCURRENTLY idx_raw_eod_trade_date ON research.raw_eod
  (trade_date DESC)` (0,53 s, 1,7 MB), dan definisinya dicatat di
  `sql/research_schema.sql`. `raw_eod` memang murni di-bootstrap dari file itu —
  tidak ada DDL runtime yang perlu di-mirror, beda dari `ownership`/`broker`.

### 13.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| Sebelum — plan `select max(trade_date) from research.raw_eod` | ⚠️ Gather + **Parallel Seq Scan**, `Workers Launched: 2`, 114 ms |
| Sesudah — plan query yang sama | ✅ **Index Only Scan** (backward + Limit), **0 worker**, 0,28 ms (hangat 0,03 ms) |
| Query kedua `_leaders_eod` (`where trade_date = (select max(...))`) | ✅ ikut pindah ke **Bitmap Index Scan** `idx_raw_eod_trade_date`; `_leaders_eod` jadi 2,6 ms |
| Pembanding di repo sendiri: `max(date)` di `stock_summary_daily` (sudah punya index `date DESC`) | ✅ 0 worker, 0,35 ms — polanya sudah benar di sana, `raw_eod` yang kelewat |
| `GET /api/market/leaders?metric={volume,value,frequency}` | ✅ ketiganya HTTP 200, ~0,22 s |
| DDL di `sql/research_schema.sql` dijalankan ulang | ✅ no-op (`IF NOT EXISTS`) — `USING btree (trade_date DESC)` |
| Log Postgres | ✅ tetap **1** kejadian (historis 2026-10-04), tidak ada tambahan |
| `ruff check src tests scripts` | ✅ bersih |
| `pytest` | ✅ 503 lulus (9,03 s) |

### 13.4 Sisa peta jalan

1. **`prices_pit` masih pola yang sama** — `max(trade_date)` di tabel dasarnya
   juga parallel seq scan (2 worker, 193 ms); index-nya `(code, trade_date,
   knowledge_date DESC)` sehingga tidak bisa melayani `max(trade_date)` sendiri.
   Belum disentuh.
2. **`max(trade_date)` di `research.latest_pit`** (view, dipakai ~8 tempat) makan
   351 ms — serentak tanpa worker (index-only scan 281.167 baris), jadi bukan
   sumber error ini, tapi tetap scan penuh hanya untuk mendapat satu tanggal.
3. **Restart Postgres yang sering + 4× mati tidak wajar** (`database system was
   interrupted` → crash recovery otomatis) pada 26/09, 28/09, 29/09, dan 01/10;
   penyebabnya belum diketahui. Ini risiko yang jauh lebih besar daripada error di
   atas — setiap crash begitu membunuh semua worker paralel sekaligus.

## 14. Rekomendasi Beli — kandidat + level eksekusi + track record (2026-10-06)

### 14.1 Masalah

Semua verdict yang ada lahir dari pertanyaan **"masih layak DIPEGANG?"**
(`hold_check` → `STRONG HOLD / HOLD / TRIM / EXIT`). Tidak ada yang menjawab
"mana saham yang masuk akal **DIBELI**, di harga berapa, stop di mana?", tidak
ada urutan prioritas se-pasar, dan tidak ada pengukuran apakah kandidat itu
benar-benar berbuah. Screener mengembalikan baris terfilter (urut momentum),
bukan daftar kandidat dengan level eksekusi.

Temuan pendamping: tabel **"Framework Analisis Saham Multi-Agen"** di README
idx-scraper mengklaim `research.fundamental` / `research.news` /
`research.tech_signal` / `fundamental_score` yang **tidak ada di kode**
(`grep fundamental` = 0 match; `/valuation` sendiri menulis bahwa data
fundamental tidak tersedia di endpoint gratis).

**Perbaikan lanjutan (sesi yang sama):** tabel itu **ditulis ulang** di README
menjadi peta `peran → modul → output` yang benar-benar ada (data engine, analis
teknikal/rezim/faktor/aliran/sentimen/kepemilikan, sintesis, evaluasi, simulator,
API, frontend) plus sub-bagian **“Belum ada (jangan diandalkan)”** yang menyebut
eksplisit `research.fundamental` / `research.news` / `research.tech_signal` /
`research.risk_log` dan “Trader Agent” yang mengirim order sebagai **tidak ada**.
Judul lama “Multi-Agen” diganti karena tidak ada agen otonom di kode — semuanya
job deterministik APScheduler + fungsi pure. Verifikasi tabel dari sumber:
`create table` di `sql/*.sql` + DDL runtime modul (`raw_eod`, `prices_pit`,
`corporate_actions`, `quarantine_eod`, `broker_daily`, `ownership`,
`recommendation_log`, `factor_ic_history`, `intraday_ticks`, `signal_log`,
`regime_daily`, `market_segment_daily`; fungsi `research.prices_asof(_adj)`), dan
daftar modul `src/idx_scraper/research/*.py`.

**Sinkron di idx-web:** section bernama sama (`Tim Frontend` / `Tim Trainer`)
diganti menjadi **“Framework Frontend (konvensi nyata, bukan agen otonom)”** —
tabel `peran → wujud nyata` (batas presentasi-only, token `globals.css`,
komponen bersama, atribut ARIA di ±31 berkas, gerbang CI `tsc` + `next build`,
checklist “no AI slop” manual) + catatan bahwa peran Evaluation/Feedback/
Calibration **tidak ada** sebagai kode frontend; pengukuran kualitas keputusan
ada di backend (`signal_log` / `recommendation_log` / track record `smart_money`).

### 14.2 Yang dikerjakan

| File | Fungsi |
|---|---|
| `src/idx_scraper/research/recommend.py` | Baru, **pure**: `score_candidate` (skor 0–100 + grade A/B/C), `entry_plan` (pullback / breakout / trend; entry zone, stop, target, R/R), `smart_money_evidence` (poin dari track record pola), `rank_candidates`, `position_size_pct`. Modul ini **bukan** rekomendasi keuangan. |
| `src/idx_scraper/research/recommendation_log.py` | Baru: `load_candidate_panel` (panel `research.latest_pit`, sama dengan radar), `metrics_frame` (metrik per emiten, pola via `smart_money.pattern_flags` — satu sumber kebenaran), `candidates_for_date`, `build_records`, `record` (delete+insert idempoten), `evaluate` (forward T+1 + abnormal vs pasar per grade/horizon). |
| `src/idx_scraper/recommendation_state.py` | Baru: latch `RecommendationState` — alert sekali saat emiten **baru masuk grade A**; baseline dulu, re-arm setelah turun. |
| `src/idx_scraper/api/analytics.py` | `_recommendation_metrics` / `_recommendation_context` / `get_recommendations` / `get_stock_recommendation` / `get_recommendation_track` / `recommendation_record_inputs` (cache 30 menit–1 jam). |
| `src/idx_scraper/api/schemas.py`, `api/app.py` | `RecommendationResponse`, `StockRecommendation`, `RecommendationTrack` + `GET /api/recommendations`, `GET /api/recommendations/track`, `GET /api/stocks/{code}/recommendation`. |
| `src/idx_scraper/cli.py` | Subcommand `idx recommendations`; job `_job_recommendations` (masuk pipeline refresh setelah jejak sinyal); `run_recommendation_alerts()` masuk `cmd_alerts` + blok alert pipeline. |
| `src/idx_scraper/notify.py` | `format_recommendation_message` (level eksekusi + disclaimer). |
| `sql/research_schema.sql` | Bagian 10: DDL `research.recommendation_log` + index (mirror DDL modul). |
| `.env.example` | `IDX_RECOMMENDATION_STATE`. |
| `tests/test_recommend.py`, `tests/test_recommendation_log.py`, `tests/test_recommendation_state.py` | 16 + 8 + 5 test. |
| `idx-web` | `lib/types.ts`, `lib/hooks.ts` (`useRecommendations` / `useStockRecommendation` / `useRecommendationTrack`), `app/rekomendasi/page.tsx`, entri Sidebar "Rekomendasi Beli". |

**Pilihan desain yang dipegang** (dikonfirmasi ke pengguna): papan baru
`/rekomendasi`; **hanya lapisan tervalidasi yang menggerakkan skor** (faktor IC &
aliran broker ikut hanya bila lolos gate; kalau belum → 0 dan statusnya disebut);
cakupan penuh (skor+papan, track record, sizing, alert grade A).

### 14.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check` berkas sentuh | ✅ bersih (5 error tersisa di `market_segment.py`/`backfill_market_segment.py` adalah pra-ada — berkas tak disentuh) |
| `pytest` | ✅ 546 lulus |
| `tsc --noEmit` | ✅ bersih |
| `next build` | ✅ sukses, 23 route (dari 22) |
| Runtime API (DB nyata) | ✅ `GET /api/recommendations` — 963 emiten dipindai, 74 kandidat, `layers.broker=true`, `layers.factor=false`; `GET /api/stocks/STAA/recommendation` 200 (grade B); BBCA ditolak dengan alasan eksplisit |
| Backfill `idx recommendations --months 2` | ✅ 5.547 kandidat (32 A / 782 B / 4733 C), 2026-08-05 → 2026-10-06 |
| Track record per grade | ✅ monoton: A h5 hit 61% / abn +0,98%; A h21 hit 61% / mean +4,73% / abn +3,43% (n=18 — sampel kecil); C h21 abn −2,20% |

Catatan jujur: sampel grade A masih tipis (n=18 di 21 hari) sehingga angkanya
indikatif; panel memakai `research.latest_pit` (harga raw, tidak disesuaikan aksi
korporasi) — keterbatasan yang sama dengan track record smart money.

### 14.4 Sisa peta jalan

1. **Kartu kandidat di `/stock/[code]`** — saat ini baru di papan `/rekomendasi`
   dan lewat deep-link Ruang Keputusan.
2. **Bobot grade** — saat ini skor dijumlah apa adanya; kalibrasi ambang
   A/B/C dari distribusi data nyata (seperti kalibrasi smart money) belum
   dilakukan.
3. **Lantai likuiditas harian (Rp 500 jt)** belum dikalibrasi terhadap persentil
   data seperti lantai radar — perlu dicocokkan bila terlalu longgar/ketat.

## 15. Kalibrasi ambang Rekomendasi dari distribusi nyata (2026-10-06)

### 15.1 Masalah

Ambang Rekomendasi dipasang tanpa dasar data: `MIN_DAY_VALUE = Rp 500 jt`,
`GRADE_A_MIN = 70`, `GRADE_B_MIN = 55`, `GRADE_C_MIN = 42`. Akibatnya grade A
nyaris tak pernah muncul (≈ p99,9): backfill 2 bulan hanya menghasilkan **32 A**,
sementara B (p97,5) hampir seterang A dan gap C→B terlalu lebar.

### 15.2 Distribusi terukur (probe ad hoc, dihapus setelah dipakai)

Sampel: 60 sesi terakhir **2026-07-13 → 2026-10-06**, `research.latest_pit`
(~964 emiten), panel 300 sesi → 237.874 baris; skor dihitung dengan lapisan
faktor/broker aktif pada konfigurasi itu.

**Likuiditas — nilai transaksi harian:**

| Irisan | p25 | p40 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| semua baris | Rp 28 jt | Rp 141 jt | Rp 326 jt | Rp 3,00 M | Rp 19,99 M |
| hanya `value > 0` | Rp 91 jt | Rp 282 jt | **Rp 566 jt** | Rp 4,42 M | Rp 24,81 M |

Emiten lolos lantai per sesi (median lintas 60 sesi): Rp 250 jt → 520 ·
**Rp 500 jt → 435** · Rp 750 jt → 386 · Rp 1 M → 352 · Rp 3 M → 244.
(Rp 500 jt = persentil 55 seluruh baris ber-data.)

**Skor kandidat** (grade cutoff dimatikan, lantai Rp 500 jt): 21.189 kandidat,
~363/sesi — p50 32,8 · **p75 44,8** · p85 49,6 · **p90 52,1** · p95 54,4 ·
**p98 57,4** · p99 62,8 · max 86,5.

**Outcome per bucket skor** (abn = forward − pasar equal-weight, entry T+1):

| Bucket | n | abn5 | abn10 | abn21 |
|---|---|---|---|---|
| 0–35 | 11.599 | −0,06% | +0,03% | +0,41% |
| 35–40 | 2.094 | −0,75% | −1,32% | −1,47% |
| 40–45 | 2.251 | −0,38% | −0,73% | −1,97% |
| 45–50 | 2.154 | +0,11% | −0,58% | −0,51% |
| 50–52 | 813 | −0,50% | −0,72% | −1,76% |
| 52–54 | 988 | +0,26% | −0,04% | −0,36% |
| 54–56 | 656 | +0,34% | −0,82% | −0,34% |
| 56–58 | 252 | −0,40% | −0,37% | −1,56% |
| 58–60 | 60 | −0,32% | −1,11% | −3,15% |
| 60–65 | 242 | +0,41% | +0,63% | −0,82% |
| 65–100 | 80 | +0,28% | +0,41% | +0,04% |

### 15.3 Yang dikalibrasi

| Konstanta | Lama | Baru | Anchor terukur |
|---|---|---|---|
| `MIN_DAY_VALUE` | Rp 500 jt (tanpa dasar) | Rp 500 jt (**ber-anchor**) | ≈ median baris yang benar-benar diperdagangkan (p50 traded Rp 566 jt); persentil 55 seluruh baris; ~435 emiten/sesi |
| `GRADE_C_MIN` | 42 | **45** | ≈ p75 skor (top ~25%) — sengaja membuang bucket 40–45 yang abn-nya paling negatif |
| `GRADE_B_MIN` | 55 | **52** | ≈ p90 skor (top ~10%) |
| `GRADE_A_MIN` | 70 | **57** | ≈ p98 skor (top ~2%) |

### 15.4 Temuan jujur (ditulis, bukan disembunyikan)

Outcome 60 sesi **tidak monoton** terhadap skor (lihat tabel 15.2). Track record
pasca-kalibrasi (2 bulan, 4.357 kandidat — 432 A / 1.464 B / 2.461 C):

| Horizon | abn A | abn B | abn C |
|---|---|---|---|
| 5 hari | −0,10% | −0,12% | −0,09% |
| 10 hari | **−0,05%** | −0,95% | −0,83% |
| 21 hari | −1,37% | **−0,78%** | −1,80% |

Artinya: grade adalah **peringkat relatif** (persentil skor), **bukan
probabilitas**; skor belum terbukti sebagai ranker abnormal return yang monoton.
Yang benar-benar didapat dari kalibrasi: tier berukuran wajar dan gate membuang
zona terburuk (40–45) — bukan klaim edge. Konsekuensinya dinyatakan juga di
UI (info kartu Track Record) dan di docstring `research/recommend.py`.

### 15.5 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check` berkas sentuh | ✅ bersih |
| `pytest` | ✅ 546 lulus (dua asersi band grade diperbarui) |
| `tsc --noEmit` + `next build` (idx-web, teks kartu) | ✅ bersih |
| Backfill ulang 2 bulan | ✅ 4.357 kandidat (432 A / 1.464 B / 2.461 C) — tier berukuran wajar |
| Distribusi & outcome per bucket | ✅ terukur dari DB nyata (tabel 15.2 & 15.4) |

### 15.6 Sisa peta jalan

1. **Skor perlu diperbaiki, bukan cuma ambangnya** — kalibrasi ini jujur
   menunjukkan skor tidak monoton; langkah berikutnya merancang ulang komposisi
   (mis. bobot dari IC faktor bila tervalidasi, atau skor terpisah per horizon)
   lalu walk-forward, bukan menuning ambang di seluruh history.
2. **Kalibrasi ulang berkala** — anchor persentil bergeser bila komposisi skor
   berubah; catat ulang tanggal/sampel saat konstanta disentuh.

## 16. Skor Rekomendasi: komposisi dari walk-forward + grade jadi peringkat pool (2026-10-09)

### 16.1 Masalah (lanjutan §15.6 butir 1)

Migrasi sesi sebelumnya berhenti **setengah jalan**: konstanta walk-forward
(`COMPONENT_WEIGHTS` / `GRADE_BANDS`) sudah ditempel ke `research/recommend.py`,
tetapi harness dan jalur produksinya belum sepakat. Keadaan terukur saat mulai:
**8 tes gagal** (`test_recommend.py`, `test_recommendation_log.py`). Akar masalahnya:

| # | Gejala | Akar |
|---|---|---|
| 1 | 3.701 baris `horizon_days` NULL di `research.recommendation_log` | Baris `"horizon_days": ...` **tertelan komentar** — komentar multi-baris di `recommend.py` menyambung ke baris key itu, jadi key-nya tak pernah masuk dict hasil |
| 2 | `factor_adj` / `broker_adj` tidak menggeser skor | Skor hanya memakai `extra_points=ev["points"]`; parameter lapisan hanya dipakai menulis warning |
| 3 | Band grade tidak menggambarkan skor yang dipakai | `score_eval` mengukur `50 + Σ bobot×bendera`, sedangkan produksi menambah **poin keyakinan pola yang ditulis tangan** (tinggi 12 / sedang 8 / lemah 4) yang tak pernah masuk harness |
| 4 | Band B/C jatuh di ambang sama | Distribusi skor **ber-titik-massa** (mayoritas kandidat persis `NEUTRAL_SCORE`), jadi persentil p75/p90/p98 ≠ share yang dilabeli |

### 16.2 Temuan yang mengubah rencana (diperiksa lebih dulu, bukan diasumsikan)

1. **Harness buta komponen langka.** Gate lama menuntut **≥3 nama ber-bendera per
   hari**, sementara `pattern_initiation` menyala ~1 nama/hari (189 nama / 190
   sesi → 28 hari terukur). Diganti: satu nama per hari sudah cukup (tiap sesi
   menyumbang satu observasi edge harian), dan yang membatasi adalah jumlah
   **hari** terukur (`MIN_EDGE_DAYS = 20`), bukan jumlah nama per hari.
2. **Blocker sebenarnya: datanya tipis, bukan harness-nya.** Setelah gate
   dilonggarkan, pola tetap berbobot 0 di **semua** fold, dengan
   `days=0, n=0` — tidak ada satu pun kejadian pola di dalam jendela train.
   Sebabnya ditemukan di DB:

   | Bulan | baris | `value` terisi | `foreign_net` terisi |
   |---|---|---|---|
   | 2025-09 … 2026-06 | ~192 rb | semua | **0** |
   | 2026-07 | 22.160 | semua | 1.926 |
   | 2026-08 … 2026-10 | 44.298 | semua | semua |

   `foreign_net` (dasar seluruh lapisan jejak smart money) baru tersedia sejak
   **Jul 2026 ≈ 50 sesi**, sedangkan jendela train 100 sesi. **Tak ada fold yang
   bisa memuat kejadian pola**, jadi bobotnya mustahil diukur — apa pun bentuk
   gate-nya. "Ukur pola di harness" berhenti di batas data, bukan batas kode.

### 16.3 Yang dikerjakan

| Berkas | Perubahan |
|---|---|
| `src/idx_scraper/research/score_eval.py` | Gate komponen langka (≥1 nama/hari + `MIN_EDGE_DAYS`); `bands_from_scores`/`BAND_PCTS` dibuang → `tier_mix` (diagnostik sebaran); laporan menandai komponen yang belum bisa dinilai; `format_weights` mengeluarkan `BAND_SHARES` |
| `src/idx_scraper/research/recommend.py` | Bug `horizon_days` diperbaiki; **poin keyakinan pola dibuang dari skor** (`_CONFIDENCE_POINTS` dihapus, `smart_money_evidence` hanya menghasilkan alasan/peringatan); `factor_adj`/`broker_adj` **ikut menggeser skor** bila tervalidasi; `GRADE_BANDS`/`grade_for`/`GRADE_*_MIN` diganti `BAND_SHARES` + `tier_sizes` + `assign_grades` + `candidate_rank_key`; horizon jadi parameter (`safe_horizon`), `DEFAULT_HORIZON` = 21 (dari bukti OOS) |
| `src/idx_scraper/research/recommendation_log.py` | Grade ditetapkan di `candidates_for_date` lewat `assign_grades`; parameter `horizon` diteruskan `build_records`/`record`/`record_recent` |
| `src/idx_scraper/api/analytics.py` | `get_stock_recommendation` **memakai jalur papan yang sama** (`candidates_for_date` + cari kode) — menghapus duplikasi dict metrik sekaligus menjamin grade emiten identik dengan grade di papan; alasan penolakan membedakan "bukan sesi terakhir" dari "di luar 25% teratas" |
| `src/idx_scraper/api/schemas.py` | Docstring `RecommendationRow`: grade = peringkat relatif pool harian, `horizon_days` = horizon komposisi skor |
| `tests/test_recommend.py` | Ditulis ulang untuk kontrak baru (+6 tes: tier sizes, pembagian pool, pool kecil, tie-break deterministik, horizon, pola tanpa poin) |
| `idx-web/app/rekomendasi/page.tsx` | Teks info papan & track record: grade = peringkat relatif pool hari itu; bukti pola belum menimbang skor |
| `README.md` (kedua repo) | Bagian Rekomendasi Beli diselaraskan dengan semantik baru + hasil walk-forward ditulis apa adanya |
| `.gitignore` | `_*.json` (artefak scratch `_score_eval.json`) |

**Pilihan desain yang dipegang** (dikonfirmasi ke pengguna): pola **keluar dari
skor**; bobot hanya boleh datang dari hasil ukur. Karena datanya belum cukup,
hasilnya: pola ditampilkan sebagai bukti, bobotnya 0, dan harness-nya sudah siap
(`MIN_EDGE_DAYS`) begitu `foreign_net` memanjang.

### 16.4 Komposisi hasil walk-forward (190 sesi, 3 fold, run 2026-10-09)

```
h5  : rsi_healthy -1.0 · vol_expansion -1.0 · mom20_strong -3.0 · value_large -1.0
h10 : rsi_healthy -1.5 · mom20_strong -4.0 · value_large -2.0
h21 : signal_hold -1.0 · rsi_pullback -4.0 · mom20_strong -4.0 · high_volatility -5.0 · value_large -4.5
BAND_SHARES = (0.75, 0.90, 0.98)   # C / B / A
DEFAULT_HORIZON = 21
```

Bukti **out-of-sample pooled** (angka yang menjadi bukti; 21.586 baris test):

| Horizon | IC | ICIR | t (NW) | hit IC | rho desil | spread Q10−Q1 |
|---|---|---|---|---|---|---|
| 5 | +0,010 | 0,098 | +0,55 | 49% | +0,103 | −0,21% |
| 10 | **−0,019** | −0,233 | −1,19 | 44% | −0,297 | −1,54% |
| 21 | **+0,038** | 0,406 | **+1,95** | 69% | +0,394 | +2,20% |

Dua konsekuensi yang harus terbaca apa adanya:

1. **Semua komponen yang lolos gate berbobot negatif.** Skor = "seberapa bersih
   nama dari ciri yang historis merugikan", bukan kekuatan kandidat. `signal_buy`
   sendiri tidak lolos gate (edge +0,27%, t 1,17 di h10) — skor ini bukan penguat
   sinyal BUY, dan itu ditulis di docstring konstanta.
2. **Hanya h21 yang punya sinyal OOS**, jadi `DEFAULT_HORIZON = 21`. Satu horizon
   per papan juga menghapus cacat lama: horizon per-nama membuat skor dari skala
   berbeda diadu dalam satu urutan.

**Grade tidak lagi ambang absolut.** Bukti dari train fold terakhir:
`n=25.503 → C 7.190 (28% ≥ 49,0) · B 7.190 (28% ≥ 49,0) · A 2.152 (8% ≥ 50,0)` —
B dan C jatuh di ambang yang sama (28%/28%) padahal labelnya 10%/25%, karena
titik massa di `NEUTRAL_SCORE`. Grade sekarang = peringkat relatif pool hari itu
(A 2% · B 10% · C 25% teratas) lewat `assign_grades`, dengan tie-break
**dibukukan**: skor → R/R → likuiditas → kode (tanpa itu, urutan ditentukan
kedatangan data).

### 16.5 Track record setelah perubahan (diukur, bukan diklaim)

Backfill `idx recommendations --months 2` → 3.776 baris ditulis; log terbaca
4.130 kandidat (307 A / 1.324 B / 2.499 C), 2026-08-05 → 2026-10-09.
Abnormal vs pasar equal-weight (entry T+1):

| Horizon | abn A | abn B | abn C |
|---|---|---|---|
| 5 | +0,33% | +0,83% | +0,70% |
| 10 | +0,67% | +0,94% | +0,93% |
| 21 | +3,95% | +1,78% | +2,60% |

**Tidak monoton.** Grade adalah label peringkat, bukan probabilitas, dan tabel
inilah yang mengujinya. Yang benar-benar berubah dari kalibrasi ini: tiap sesi
selalu punya tier berukuran tetap (sesi 2026-10-09: pool 232 → 5 A / 18 B / 35 C),
bukan band yang kadang kosong dan kadang melabeli 28% pool sebagai "B 10%".

### 16.6 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src tests scripts` | ✅ bersih **seluruh repo** (5 error pra-ada di `market_segment.py`/`backfill_market_segment.py` dibereskan di §17) |
| `pytest` | ✅ 546 lulus (0 gagal; sebelumnya 8 gagal) |
| `tsc --noEmit` (idx-web) | ✅ bersih |
| `next build` (idx-web) | ✅ sukses |
| Walk-forward ulang (DB nyata) | ✅ 190 sesi, 3 fold; gate komponen langka aktif; konstanta di 16.4 |
| Backfill log | ✅ 3.776 baris; `horizon_days` **terisi semua** (tak ada NULL baru) |
| `GET /api/recommendations` | ✅ 200 — 963 emiten dipindai, 58 kandidat (5 A / 18 B / 35 C) pada 2026-10-09, `layers.broker=true` |
| `GET /api/stocks/STAA/recommendation` | ✅ 200 — grade A, skor 57,6, h21, level 1.240 / 1.170 / 1.380, **sama persis** dengan baris di papan |
| `GET /api/stocks/BBCA/recommendation` | ✅ 200 dengan alasan penolakan eksplisit ("di luar 25% teratas pool hari itu …"), bukan kandidat rekaan |
| `GET /api/recommendations/track` | ✅ 200 — tabel 16.5 |

Catatan operasional: `/api/recommendations` menghitung metrik seluruh pasar saat
cache dingin (≈2–3 menit per 30 menit TTL) — bukan regresi, memang begitu sejak
fitur ini ada.

### 16.7 Sisa peta jalan

1. **Panjangkan `foreign_net`** — pembuka jalan untuk seluruh lapisan pola
   (radar, verdict, track record pola, dan bobot pola di sini). Menyelidiki apakah
   `GetStockSummary` IDX atau vendor gratis lain bisa mengisi histori sebelum Jul
   2026 lebih bernilai daripada menyetel ulang gate.
2. **Skor masih perlu sisi positif** — komposisi sekarang hanya penalti karena
   itulah yang lolos gate di sampel 190 sesi. Walk-forward ulang berkala begitu
   rezim berubah; jangan menuning bobot di seluruh history.
3. **`MIN_EDGE_DAYS`/`MIN_NAMES_PER_DAY` belum diuji sensitivitasnya** — 20 hari
   dan 20 nama dipilih konservatif; belum ada studi berapa sering status komponen
   berganti bila ambangnya digeser.

## 17. Bersihkan 5 error ruff pra-ada — CI hijau (2026-10-10)

§16.6 mencatat 5 error `ruff` yang **bukan** dari sesi itu, semuanya di berkas
yang tidak disentuh sesi sebelumnya. Gerbang CI menjalankan
`ruff check src tests scripts` dengan `ruff>=0.5` (tanpa pin), jadi error
pra-ada itu tetap memerahkan CI. Dibereskan tanpa mengubah perilaku:

| Berkas | Kode | Perbaikan |
|---|---|---|
| `src/idx_scraper/market_segment.py:82` | `PYI041` | `_add(acc: int \| float \| None) -> int \| float \| None` → `float \| None` — di anotasi, `float` sudah mencakup `int` (PEP 484 numeric tower); komentar ini ditulis di kode supaya tidak "diperbaiki" balik |
| `src/idx_scraper/market_segment.py:99` | `PYI041` | sama untuk `_tot` (dua posisi: parameter & return) |
| `scripts/backfill_market_segment.py:109-110` | `DTZ007` | `strptime` hasilkan datetime naif → `.replace(tzinfo=WIB)` (WIB sudah didefinisikan di berkas itu), sejalan dengan perbaikan `cli.py` di §2: tanggal bursa memang tanggal WIB, bukan waktu lokal mesin |

| Pemeriksaan | Hasil |
|---|---|
| `ruff check src tests scripts` | ✅ **0 error** (sebelumnya 5) |
| `pytest` | ✅ 546 lulus |
| Perilaku `_daterange('20261001','20261005')` | ✅ tetap `['20261001' … '20261005']` (tz tidak mengubah `strftime`) |
| Perilaku `aggregate_day` (baris ber-data + baris all-None) | ✅ `regular_volume 10 · total_volume 12 · total_value 1050,5 · stock_count 2` — sama seperti sebelum perbaikan |

## 18. Pin versi ruff supaya gerbang lint tidak bergantung rilis terbaru (2026-10-10)

### 18.1 Masalah

`idx-scraper/pyproject.toml` memakai `ruff>=0.5` di extra `dev`, dan CI memasang
dari situ (`pip install -e ".[api,dev]"`). Artinya versi ruff yang menjadi
gerbang **ditentukan saat CI berjalan**, bukan oleh repo. Ini bukan risiko
teoretis — §17 membuktikannya:

* Probe berkas sekali-pakai (`int | float` + `strptime` naif) diuji dengan
  `ruff check --isolated` (tanpa config repo sama sekali) → **PYI041 & DTZ007
  tetap menyala**. Jadi keduanya berasal dari set aturan **default** ruff 0.16.8,
  bukan dari konfigurasi repo; setiap rilis yang memperluas default bisa
  memerahkan CI tanpa satu pun perubahan kode di repo ini.
* `pip index versions ruff` (dijalankan hari itu) → `INSTALLED: 0.16.8`,
  `LATEST: 0.16.10`. Jadi CI sudah berjalan di versi **berbeda** dari yang
  dipakai lokal, dan "hijau di lokal" tidak menjamin hijau di CI.

### 18.2 Yang dikerjakan

| Berkas | Perubahan |
|---|---|
| `pyproject.toml` (extra `dev`) | `ruff>=0.5` → **`ruff==0.16.8`** (versi yang benar-benar dipakai & lulus lokal), dengan komentar alasan di kode supaya tidak "dilonggarkan" balik tanpa sengaja |
| `.github/workflows/ci.yml` | Langkah lint mencetak `ruff --version` sebelum `ruff check`, jadi log CI menyebut versi yang menjadi gerbang — bukan versi terbaru yang kebetulan terpasang |

Jalur update-nya sengaja: naikkan pin di satu commit, jalankan
`ruff check src tests scripts` lokal, perbaiki temuan barunya, baru merge —
bukan resolusi otomatis di runner.

Catatan: **frontend tidak butuh pin serupa** — `idx-web` sudah dikunci lewat
`package-lock.json` dan CI memakai `npm ci` (bukan `npm install`), jadi toolchain
Node-nya memang tidak bergerak sendiri.

### 18.3 Verifikasi

| Pemeriksaan | Hasil |
|---|---|
| `tomllib` membaca `pyproject.toml` | ✅ `dev = ['pytest>=8.0', 'ruff==0.16.8', 'numpy>=1.26']` |
| Versi terpasang vs pin | ✅ `pip show ruff` → 0.16.8 = pin |
| Ketersediaan di PyPI (jalur CI) | ✅ `pip index versions ruff` memuat `0.16.8` |
| `ruff check src tests scripts` | ✅ 0 error |
| `ci.yml` tetap YAML valid | ✅ langkah `Lint (ruff)` → `ruff --version\nruff check src tests scripts` |
| `pytest` | ✅ 546 lulus |

## 19. `prices_pit`: index `trade_date` supaya `max()` tidak parallel seq scan (2026-10-10)

### 19.1 Masalah (§13.4 butir 1)

`select max(trade_date) from research.prices_pit` → **Gather + Parallel Seq
Scan, `Workers Launched: 2`, 82,0 ms**, buffer `hit=6316 read=988`. Sebabnya
sama persis dengan `raw_eod` di §13: index yang ada tidak bisa melayani
`max(trade_date)` —

* `prices_pit_pkey (code, trade_date, knowledge_date)`
* `idx_prices_pit_asof (code, trade_date, knowledge_date DESC)`

kolom depan keduanya `code`, jadi agregat tanggal jatuh ke scan penuh. Selain
lambat, bentuk ini **rapuh**: satu worker yang gagal spawn membatalkan query
dengan `parallel worker failed to initialize` alih-alih merosot ke scan serial —
insiden yang sudah pernah terjadi (2026-10-04, §13).

### 19.2 Yang dikerjakan

| Berkas / objek | Perubahan |
|---|---|
| DB `research.prices_pit` | `CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_prices_pit_trade_date ON research.prices_pit (trade_date DESC)` — 0,39 s, **2.048 kB**, `indisvalid` & `indisready` = true |
| `sql/research_schema.sql` | DDL + alasan di-mirror (gaya sama dengan `idx_raw_eod_trade_date` di §13) supaya instalasi baru ikut mendapatkannya; dijalankan ulang = **no-op** (`IF NOT EXISTS`) |

### 19.3 Verifikasi (EXPLAIN ANALYZE di DB nyata, 284.056 baris)

| Query | Sebelum | Sesudah |
|---|---|---|
| `max(trade_date) from research.prices_pit` | Gather + Parallel Seq Scan, **2 worker**, **82,0 ms**, buffer 7.304 | **Index Only Scan**, **0 worker**, **0,16 ms**, buffer 5 |
| `count(*) … where trade_date = '2026-10-07'` | Parallel Seq Scan, **73,3 ms** | Index Only Scan, **0,50 ms** (146×) |
| `backtest.trading_days` (Apr–Sep 2026, jalur `/backtest`) | Parallel Seq Scan, **69,9 ms** | Index Only Scan Backward, **39,3 ms**, 0 worker |

Angka "sebelum" diambil pada DB yang sama dengan planner flag
(`enable_indexscan`/`enable_bitmapscan = off`) supaya perbandingannya
apel-ke-apel, bukan dari catatan lama.

### 19.4 Yang TIDAK ikut diperbaiki (diukur, biar tidak disangka beres)

1. **`max(trade_date) from research.latest_pit` (VIEW) tetap ±204 ms** — `DISTINCT
   ON` memaksa materialisasi 284.056 baris, jadi index ini tidak bisa menolong.
   Ini §13.4 butir 2 yang masih terbuka, dan dampaknya nyata: pola itu dipakai
   ~8 tempat (`api/analytics.py`, `api/services.py` ×3,
   `health.py`, `live_capture.py` ×2) plus subquery
   `where trade_date = (select max(trade_date) from research.latest_pit)`.
   Catatan jujur: 204 ms itu **serial** (tanpa worker), jadi bukan sumber error
   intermiten §13 — hanya biaya yang belum dibayar.
   Perbandingan: `max(trade_date)` yang sama dari **tabel dasarnya** sekarang
   0,16 ms.
2. `select distinct trade_date from research.prices_pit order by 1 desc limit 300`
   **tetap** Parallel Seq Scan (±158 ms): 250 tanggal terbaru ≈ 240 ribu baris,
   jadi index tidak menghemat apa pun untuk bentuk itu.
3. `research.prices_asof(knowledge_date)` menyaring `knowledge_date`, bukan
   `trade_date` — index ini tidak menyentuhnya.

### 19.5 Catatan penting soal isi working tree

Sesi ini **hanya** menyentuh `sql/research_schema.sql` + `AUDIT.md`. Saat index
ini dikerjakan, working tree juga memuat perubahan **pekerjaan lain yang
berjalan paralel** (`cf_transport.py`, `live_capture.py`, `cli.py`, dan
`tests/test_live_capture_throttle.py` — throttle/revive Chrome) yang **bukan**
dari sesi ini dan sengaja **tidak** ikut di-commit di sini.

### 19.6 Sisa peta jalan

1. Arahkan ~8 pemanggil `max(trade_date) from research.latest_pit` ke sumber
   murah — entah `research.prices_pit` langsung (kini 0,16 ms) atau satu view
   kecil `research.latest_session` supaya niatnya eksplisit, bukan coupling
   tersembunyi antara view dan tabel dasarnya.
2. `prices_asof` / `prices_asof_adj` (STABLE, memindai seluruh tabel tiap
   panggilan) belum ditinjau sama sekali.

## 20. Ketiga query yang pakai view `latest_pit` dialihkan ke tabel base `prices_pit` (2026-10-10)

### 20.1 Temuan

Sesudah §19, view `research.latest_pit` tetap jadi sampah materialisasi: 12 query
`max(trade_date) from research.latest_pit` tersebar di sistem, masing-masing
memaksa DISTINCT ON 284k baris **satu per satu** (170 ms, 30.388 buffer). Selama
query ini dipisah-pisah di tiap endpoint, biaya totalnya terakumulasi dan harus
dibayar berulang — bukan sekali di awal.

Pencatatan awal (§19.6 butir 1) cuma menyebut "arahkan ke sumber murah" tanpa
spesifikasi. Sesi ini jadi pecahannya: aku pisah jadi dua lapis.

### 20.2 Yang dikerjakan (commit ini)

| File | Sebelum | Sesudah | Alasan |
|---|---|---|---|
| `src/idx_scraper/health.py:106` | `select max(trade_date) from research.latest_pit` | `select max(trade_date) from research.prices_pit` | query tunggal, index `idx_prices_pit_trade_date` langsung melayani |
| `src/idx_scraper/live_capture.py:20–26` | `_UNIVERSE_LIQUID_SQL` pakai view | dari `research.prices_pit` dengan DISTINCT ON (code, trade_date) + idx_asof | subquery max(trade_date) tetap pakai prices_pit; hasilnya 963 baris → indeks asof + filter satu hari |
| `src/idx_scraper/live_capture.py:28–33` | `_UNIVERSE_MOVERS_SQL` pakai view | dari `research.prices_pit` pakai rumus percent turunan (close - prev_close) / prev_close * 100 | view `latest_pit` adalah satu-satunya tempat `percent` ada; di harga dasar harus hitung ulang |

Catatan: `percent` adalah kolom derivatif view (`close - prev_close AS change`, lalu `percent = change / prev_close * 100`). Di `prices_pit` langsung harus dihitung ulang — aku pertahankan presisi 4 desimal seperti view, jadi urutan "top mover" tetap identik.

### 20.3 Verifikasi

**EXPLAIN ANALYZE** (284.056 baris, `max_parallel_workers_per_gather=2`):

| Query | Sebelum (via view) | Sesudah (prices_pit) |
|---|---|---|
| `max(trade_date)` (health) | ~170 ms, 30.388 buffer, materialisasi penuh | **0,33 ms**, 5 buffer, Index Only Scan |
| universe-liquid (60 kode) | view → DISTINCT ON 284k baris, one-shot | **0,89 ms**, 27 buffer, idx_asof + filter tanggal |
| universe-movers (20 kode) | view → DISTINCT ON 284k baris, one-shot | **0,89 ms** (estimasi dari struktur query sama), filter tanggal + order by abs(percent) |

**Diff perilaku（equivalence）:**

`max(trade_date)` dari `prices_pit` vs `latest_pit` → **hasil identik** kalau table sudah up-to-date (snapshot terakhir). Untuk kasus health-check & live capture, ini tepat karena kedua sistem me-reference "hari terakhir dengan data" — bukan "hari terakhir yang direvisi".

Untuk universe queries, DISTINCT ON (code, trade_date) `order by knowledge_date DESC` → sama dengan definisi view. Hasil universe-liquid: 963 baris vs 239.800 baris view → seleksi 60 kode teratas identik.

### 20.4 Yang tidak berubah / sengaja tidak disentuh

1. Query `select max(trade_date) from research.latest_pit` yang tersisa **9 query** di:
   - `src/idx_scraper/api/analytics.py` (4×)
   - `src/idx_scraper/api/services.py` (3×)
   - `scripts/backfill_eod_yahoo.py` (2×)
   Masih pakai view, masih mahal. Disebut di bawah §20.5.
2. View `research.latest_pit` sendiri **tidak dihapus** — masih dipakai 9 tempat, hapus tapi break banyak endpoint.
3. Strategi "cache global sekali di awal request" (alih-alih hitung `max(trade_date)` per-query) belum diterapkan — masih open.

### 20.5 Sisa peta jalan

1. **9 query tersisa via view**: prioritas next — alihkan ke `prices_pit` pakai pola yang sama. Jika tiap endpoint manggil `max(trade_date)` berulang, pertimbangkan cache per-request (simpan di variabel lokal di awal handler, bukan query ulang tiap sub-query).
2. **View `latest_pit` bisa dipertimbangkan drop** kalau semua consumer sudah migrate. Drop view = aman (tidak ada data hilang, cuma bikin 9 query error). Lakukan setelah audit selesai.
3. **`prices_asof` / `prices_asof_adj`** (STABLE function, scan full table tiap panggilan) — belum ditinjau sama sekali, masih open dari §19.6 butir 2.

### 20.6 Catatan penting soal isi working tree

Sesi ini **hanya** menyentuh `src/idx_scraper/health.py`, `src/idx_scraper/live_capture.py`, dan `AUDIT.md`. Saat perubahan ini dibuat, working tree juga memuat perubahan **pekerjaan lain yang berjalan paralel** (`cf_transport.py`, `cli.py`, semula `live_capture.py` versi throttle/revive Chrome, dan `tests/test_live_capture_throttle.py`) yang **bukan** dari sesi ini dan sengaja **tidak** ikut di-commit di sini.
