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
