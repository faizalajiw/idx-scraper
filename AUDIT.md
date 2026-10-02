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
| `src/idx_scraper/api/dividends.py` | Endpoint dividen: overview TTM, daftar semua emiten pembagi, detail per emiten (riwayat, total per tahun, growth), ledger aksi korporasi. |
| `src/idx_scraper/api/simulation.py` | Lapisan API simulator: katalog strategi + default biaya, validasi, dan orkestrasi `run_backtest`. Tidak ada persistensi — murni komputasi. |
| `src/idx_scraper/api/sector_lookup.json` | Layer pemetaan emiten→sektor hasil generate (Yahoo sector/industry), menutupi hampir seluruh emiten yang punya harga. |
| `scripts/refresh_sector_map.py` | Generator `sector_lookup.json` + laporan coverage. |
| `tests/test_alert_rules.py`, `tests/test_alerts_store.py`, `tests/test_dividends.py`, `tests/test_backtest_costs.py`, `tests/test_backtest_rebalance.py`, `tests/test_sector_map.py`, `tests/test_simulation.py` | Suite pengujian untuk semua fitur di atas. |

**File berubah:**

| File | Perubahan |
|---|---|
| `src/idx_scraper/api/app.py` | Registrasi endpoint: `/api/dividends/*`, `/api/stocks/{code}/dividends`, `/api/corporate-actions`, CRUD `/api/alerts` (+`/test`), `GET /api/backtest/config`, `POST /api/backtest/run`, helper `_parse_optional_date` (422 untuk format tanggal salah). |
| `src/idx_scraper/api/schemas.py` | +341 baris: skema respons dividen, alert, backtest (request/config/result/ledger), quality tambahan. |
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
| `app/dividen/page.tsx` | Halaman Dividen: statistik TTM, chart aktivitas per tahun, yield tertinggi, baru dibayar, semua emiten pembagi (filter/sort), panel detail per emiten, aksi korporasi. Disclaimer jujur soal tidak ada kalender ex-date mendatang. |
| `app/backtest/page.tsx` | Simulator: pilih strategi & parameter, periode, modal, frekuensi rebalance, preset biaya (tanpa biaya / standar IDX / konservatif) + knob manual. Menampilkan metrik bruto vs neto vs buy & hold, dampak biaya, ledger rebalance, dan penjelasan cara membaca hasil. |
| `components/AlertRules.tsx` | UI CRUD aturan pantauan (tipe, ambang, catatan) dengan status live per aturan. |
| `components/TelegramStatus.tsx` | Cek koneksi bot + kirim pesan uji; panduan setup bila env belum diisi. |
| `components/EquityCurveChart.tsx` | Chart kurva ekuitas + benchmark. |
| `components/RebalanceLedger.tsx` | Tabel trade & posisi per sesi rebalance (bukti churn). |
| `components/DividendYearChart.tsx`, `components/DividendDetailPanel.tsx` | Visualisasi dividen tahunan dan detail per emiten. |

**File berubah:** `lib/types.ts` (mirror skema backend untuk dividen/alert/backtest),
`lib/api.ts` (`runBacktest`, `fetchBacktestConfig`, `createAlert`, `deleteAlert`,
`testTelegram`), `lib/hooks.ts` (hook SWR untuk semua endpoint baru),
`components/Sidebar.tsx` (nav Pantau & Dividen), `README.md`.

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
| Verifikasi runtime 2026-09-26: API hidup, CRUD alert (create→duplikat 422→delete→404), endpoint dividen & `POST /api/backtest/run` (240 hari bursa, 13 rebalance, drag 1,73%) | ✅ semua 200/422/404 sesuai harapan |
| Verifikasi browser (headless Chrome): halaman `/pantau`, `/dividen`, `/backtest` ter-render lengkap dengan data | ✅ |

---

## 4. Kondisi operasional & risiko yang diketahui

1. **Telegram opsional** — aturan tetap tersimpan & dievaluasi tanpa token;
   pengiriman butuh `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` di `idx-scraper/.env`
   (cek lewat halaman Pantau → "Kirim pesan uji").
2. **Data historis harus di-seed** agar dividen, backtest, dan sinyal bermakna:
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
