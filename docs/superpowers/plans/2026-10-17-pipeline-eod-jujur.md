# Pipeline EOD Jujur & Antikehilangan Kolom — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Membuat jalur pengisian data EOD (`raw_eod`) tidak bisa lagi kehilangan data secara diam-diam — baik kehilangan sesi (libur vs blokir vs luar jendela) maupun kehilangan kolom (sesi ada, isi kolom kosong 9 bulan).

**Architecture:** Tiga perubahan kecil yang saling menutup: (1) skrip backfill resmi membaca status transport sehingga blokir 403 tidak pernah lagi dilaporkan sebagai "hari libur" dan aman dijalankan ulang; (2) `health.py` menambah penjaga cakupan kolom (bukan cuma tanggal terbaru) sehingga celah kolom ketahuan dalam ≤1 sesi; (3) jendela catch-up `_backfill_hari_lampau` dilebarkan dari 5 → 21 hari bursa. Ditambah satu skrip perbaikan bertarget (`repair_foreign_flow.py`) yang idempoten.

**Tech Stack:** Python ≥3.11, psycopg 3, Postgres/Supabase (`research.raw_eod`, `research.prices_pit`, `research.quarantine_eod`), pytest, ruff 0.16.8 (dipin), curl_cffi lewat `IDXClient` (`client.session.last_status`).

**Spec:** Tidak ada dokumen spec terpisah — plan ini murni ops/pipeline, bukan model. Sumber kebenaran = bagian **Konteks & Bukti** di bawah (audit 17 Okt 2026).

## Global Constraints

- Python ≥3.11; paket terpasang editable (`.venv`) → `import idx_scraper` jalan tanpa `PYTHONPATH`; modul di `scripts/` hanya bisa diimpor sebagai `scripts.<nama>` (jalankan `python -m scripts.<nama>` dari root repo).
- Ruff dipin `0.16.8`, `line-length = 100`, target `py311`. Gerbang: `.venv\Scripts\ruff.exe check src tests scripts` → `All checks passed!`. `scripts/*.py` sudah punya `per-file-ignores`: `BLE001, S110, S112, PLR0124`.
- Tes dijalankan: `.venv\Scripts\python.exe -m pytest tests -q`. **Tes tidak boleh menyentuh jaringan maupun DB** (gaya `tests/test_eod_retry.py`: klien & koneksi diganti stub).
- CLI tersedia sebagai entry point: `.venv\Scripts\idx.exe` (`[project.scripts] idx = "idx_scraper.cli:main"`) → `idx health`, `idx serve`, `idx refresh`.
- Tanggal bursa selalu dihitung **WIB** (`datetime.now(WIB)`), tidak pernah tz lokal mesin.
- Jeda request ke IDX **≥4 detik**; burst 1,2 s terbukti memicu 403 Cloudflare (257 request beruntun → 257× 403).
- Retensi IDX = ±2 tahun bergulir (diuji 17 Okt 2026: `20241001` → 503, `20241015` → 200). Tanggal di luar jendela = 503 permanen, bukan gangguan.
- Jangan mengubah perilaku sukses yang sudah dipakai scheduler: `_simpan_eod_hari`, `_job_eod_full`, `_job_pipeline_refresh`, `_ingest_eod_research`, jadwal `serve` — nol modifikasi di task-task ini kecuali yang tertulis eksplisit.

## Konteks & Bukti (audit 17 Okt 2026, sudah dijalankan)

| Temuan | Angka / bukti |
|---|---|
| Sesi EOD | `raw_eod` **476 sesi** 2024-10-15 → 2026-10-16, **455.482 baris**, 48 hari kerja tanpa sesi = **semua libur bursa** |
| Kolom foreign hilang lalu ditambal | 201 sesi (2025-09-24 → 2026-07-29), **192.613 kolom** `foreign_buy/sell/net` NULL; sudah ditambal 100% |
| Kolom lain tidak berubah saat tambalan | hash per sesi (open/high/low/close/prev_close/volume/value/frequency/name) untuk 201 sesi: **0 berubah** |
| Bukan jalur Yahoo | `select source, count(*) … group by 1` → **455.482 baris `source='IDX'`**; baris Yahoo selalu `value`/`frequency` NULL, ini tidak |
| Bukan bug yang masih hidup | 2026-08 → 2026-10 cakupan foreign **100%** (jalur pipeline hidup sudah menulis `foreign_*`, lihat `cli.py:706-712`) |
| Lubang tanggal yang tampak "permanen" | 10 sesi yang selalu dianggap "lubang IDX" ternyata **semuanya bisa ditarik** dengan backoff sabar (403,403 → sukses) |
| Penyebab dilaporkan sebagai libur | `scripts/backfill_raw_eod.py:127-130`: `if not rows: empty_days += 1` — tanpa pernah membaca status transport |

**Kesimpulan akar masalah kolom:** baris lama masuk ke `raw_eod` sebelum pemetaan `foreign_*` ada di jalur ingest (repo ini lahir 2026-09-22; ada jalur migrasi warisan `scripts/migrate_sqlite_to_postgres.py`). Tidak ada satu pun proses yang menarik ulang tanggal yang **sudah ada tapi kolomnya kosong** — `_backfill_hari_lampau` (`cli.py:943`) dan `check_eod_gaps.py` hanya mengejar tanggal yang hilang seluruhnya. Itulah kenapa celahnya bertahan 9 bulan tanpa satu pun alarm.

## Review Focus

Input/keadaan yang belum diuji tes mana pun dan paling mungkin menggigit pengguna sistem ini — tiap baris dipasangkan tes di task pemiliknya:

1. **503 untuk tanggal DI DALAM jendela retensi** (gangguan sesaat, ~4% request) — bukan libur, bukan blokir; kalau dihitung libur, sesi hilang diam-diam (tes: Task 1).
2. **Rentang yang sudah terisi sebagian** (crash di tengah satu hari) — `insert` polos melanggar unique index, sedangkan "lewati tanggal yang ada" melewatinya diam-diam (tes: Task 1, `--ulang`).
3. **Kolom baru keisi hanya untuk baris baru** — persis kasus `foreign_*`; penjaga harus berbunyi pada sesi pertama yang timpang, bukan setelah 9 bulan (tes: Task 2).
4. **`IDX_HOLIDAYS` kosong/salah** — health dan catch-up menganggap libur sebagai "bolong"; harus jadi laporan, bukan abort/panic (tes: Task 2 & Task 4).
5. **Mesin bukan WIB** — tanggal bursa yang dihitung dari tz lokal bisa menggeser seluruh jendela satu hari (tes: Task 1, `tanggal_perlu` menerima `date` eksplisit; Task 4 memakai WIB).

---

### Task 1: Skrip backfill resmi jujur soal blokir + aman dijalankan ulang

**Files:**
- Modify: `scripts/backfill_raw_eod.py` (33 `SLEEP_SECONDS`, 84-172 `backfill`, 194-209 `main`)
- Create: `tests/test_backfill_raw_eod.py`
- Delete: `scripts/backfill_raw_eod_paced.py` (isinya dilebur ke skrip resmi)
- Modify: `README.md` (daftar `scripts/` di sekitar baris 336-339)

**Interfaces:**
- Produces: `scripts.backfill_raw_eod.klasifikasi(rows: list | None, status: object) -> str` (salah satu `"data" | "libur" | "luar-jendela" | "diblokir" | "gagal"`), `scripts.backfill_raw_eod.tanggal_perlu(days: Iterable[date], ada: set[date], ulang: bool) -> list[date]`, `apply_foreign`? — tidak ada; hanya dua fungsi murni di atas yang diuji.
- Menghapus: `scripts/backfill_raw_eod_paced.py` → alat operator setelah task ini: `python -m scripts.backfill_raw_eod --start-date … --end-date … [--ulang]`.

- [ ] **Step 1: Pastikan unique index ada (prasyarat ON CONFLICT)**

```bash
.venv\Scripts\python.exe -c "import os,psycopg;from pathlib import Path;[os.environ.setdefault(*[x.strip() for x in l.split('=',1)]) for l in Path('.env').read_text().splitlines() if '=' in l and not l.startswith('#')];c=psycopg.connect(os.environ['DATABASE_URL']);print(c.execute(\"select indexdef from pg_indexes where tablename='raw_eod'\").fetchall())"
```

Expected (terverifikasi 17 Okt 2026): `CREATE UNIQUE INDEX idx_raw_eod_lookup ON research.raw_eod USING btree (code, trade_date)`. Kalau **tidak ada** unique index `(code, trade_date)`: berhenti, laporkan — `ON CONFLICT (code, trade_date)` di bawah tidak akan bekerja.

- [ ] **Step 2: Tulis tes yang gagal**

```python
# tests/test_backfill_raw_eod.py
from datetime import date
from scripts.backfill_raw_eod import klasifikasi, tanggal_perlu

def test_200_dengan_baris_berarti_data():
    assert klasifikasi([object()], 200) == "data"

def test_200_tanpa_baris_berarti_libur():
    assert klasifikasi([], 200) == "libur"

def test_503_berarti_luar_jendela_bukan_libur():
    assert klasifikasi([], 503) == "luar-jendela"

def test_403_berarti_diblokir():
    assert klasifikasi([], 403) == "diblokir"

def test_429_berarti_diblokir():
    assert klasifikasi([], 429) == "diblokir"

def test_status_tak_dikenal_atau_exception_berarti_gagal():
    assert klasifikasi([], None) == "gagal"
    assert klasifikasi([], "EXC:TimeoutError") == "gagal"

def test_tanggal_perlu_melewati_yang_sudah_ada():
    days = [date(2026, 10, 5), date(2026, 10, 6)]
    assert tanggal_perlu(days, {date(2026, 10, 5)}, ulang=False) == [date(2026, 10, 6)]

def test_tanggal_perlu_ulang_mengambil_semua():
    days = [date(2026, 10, 5), date(2026, 10, 6)]
    assert tanggal_perlu(days, {date(2026, 10, 5), date(2026, 10, 6)}, ulang=True) == days
```

- [ ] **Step 3: Jalankan, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backfill_raw_eod.py -q`
Expected: FAIL — `ImportError: cannot import name 'klasifikasi'`.

- [ ] **Step 4: Implementasi `klasifikasi` + `tanggal_perlu`**

Di `scripts/backfill_raw_eod.py`, ganti `SLEEP_SECONDS = 1.2` → `SLEEP_SECONDS = 4.0  # jeda ≥4 s: burst 1,2 s terbukti memicu 403 Cloudflare`. Tambah dua fungsi murni: `klasifikasi` (urutannya: baris → `"data"`; `status == 200` → `"libur"`; `status == 503` → `"luar-jendela"`; `status in (403, 429)` → `"diblokir"`; selain itu → `"gagal"`) dan `tanggal_perlu` (kembalikan `days` apa adanya kalau `ulang`, selain itu yang tidak ada di `ada`).

- [ ] **Step 5: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backfill_raw_eod.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Pakai klasifikasi di `backfill()` + opsi `--pace`/`--ulang`**

Ubah `backfill(years=None, start_date=None, end_date=None, pace: float = SLEEP_SECONDS, ulang: bool = False)`. Per tanggal, urutannya:
1. Query `ada` sekali di awal (`select distinct trade_date from research.raw_eod`), lalu `tanggal_perlu(days, ada, ulang)`.
2. Ambil data dengan loop backoff `(0, 30, 90)`; setiap percobaan baca `getattr(client.session, "last_status", None)`; simpan `rows`+`status` terakhir.
3. `klasifikasi(rows, status)` menentukan jalannya: `"data"` → insert; `"libur"`/`"luar-jendela"`/`"gagal"` → hitung lalu lanjut; `"diblokir"` → cetak `[backfill] DIHENTIKAN di <tanggal> (status 403) — lanjutkan: --start-date <tanggal>` lalu **berhenti** (resume, bukan salah hitung libur).
4. Insert: `on conflict (code, trade_date) do nothing` untuk mode normal; kalau `ulang`, `do update set open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, prev_close=excluded.prev_close, volume=excluded.volume, value=excluded.value, frequency=excluded.frequency, source=excluded.source, name=excluded.name, foreign_buy=excluded.foreign_buy, foreign_sell=excluded.foreign_sell, foreign_net=excluded.foreign_net, ingested_at=now()`.
5. Ringkasan akhir memisahkan kelima ember: `raw_eod +N, karantina +N, libur N, luar jendela N, gagal N, dilewati N`.
6. `main()` menambah `--pace` (float, default `SLEEP_SECONDS`) dan `--ulang` (store_true), meneruskannya ke `backfill()`.

- [ ] **Step 7: Tes kembali hijau + lint**

Run: `.venv\Scripts\python.exe -m pytest tests -q` lalu `.venv\Scripts\ruff.exe check src tests scripts`
Expected: semua tes lama tetap lulus; `All checks passed!`.

- [ ] **Step 8: Uji nyata pada rentang yang sudah ada (harus 0 tulis, 100% dilewati)**

Run: `.venv\Scripts\python.exe -m scripts.backfill_raw_eod --start-date 2026-10-12 --end-date 2026-10-16`
Expected: `dilewati 5` (5 hari bursa sudah ada), `raw_eod +0`, exit code 0, **tanpa traceback**.

Alasan tes ini bermakna: `idx_raw_eod_lookup` UNIQUE `(code, trade_date)` ada (Step 1) sedangkan `insert` lama tidak punya `ON CONFLICT` — jadi perintah ini sebelum perbaikan berakhir dengan galat unique violation pada baris pertama tanggal yang sudah ada (akibat langsung dari definisi index, bukan asumsi). Sesudah perbaikan, jalur ini selesai bersih.

- [ ] **Step 9: Hapus duplikat + rapikan README, lalu commit**

```bash
Remove-Item scripts/backfill_raw_eod_paced.py
git add scripts/backfill_raw_eod.py tests/test_backfill_raw_eod.py README.md
git commit -m "fix(backfill): klasifikasi status IDX (blokir vs libur vs luar jendela) + skrip aman dijalankan ulang"
```
README: ganti daftar skrip lama dengan nama baru (`backfill_raw_eod.py` — kini berjeda 4 s + deteksi blokir, pakai `--ulang` untuk menarik ulang tanggal yang sudah ada).

---

### Task 2: Penjaga cakupan kolom di `health.py` (alarm ≤1 sesi)

**Files:**
- Modify: `src/idx_scraper/health.py` (tambah dataclass + 2 fungsi di bawah `EodHealth`, sekitar baris 62-153)
- Modify: `src/idx_scraper/cli.py` (`cmd_health` baris 1252, `_job_health_eod` baris 298)
- Modify: `tests/test_health.py` (tambah blok tes baru di akhir file)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `IDXClient` tidak diperlukan (murni DB read-only).
- Produces: `health.FieldCoverage(checked: int, troublemakers: list[str], worst_pct: float)` dengan properti `.ok`; `health.evaluate_field_coverage(rows: list[tuple[date, int, int]], sessions: int = 30) -> FieldCoverage` (murni; `rows` = `(trade_date, total_baris, foreign_terisi)`); `health.check_field_coverage(dsn: str | None = None, sessions: int = 30) -> FieldCoverage`; `health.format_field_coverage_message(c: FieldCoverage) -> str`.

- [ ] **Step 1: Tulis tes yang gagal**

```python
# tests/test_health.py  (impor tambahan: FieldCoverage, evaluate_field_coverage, format_field_coverage_message)
def test_cakupan_penuh_ok():
    rows = [(date(2026, 10, 5), 958, 958), (date(2026, 10, 6), 956, 956)]
    c = evaluate_field_coverage(rows)
    assert c.ok and c.checked == 2 and c.troublemakers == [] and c.worst_pct == 100.0

def test_satu_sesi_timpang_dilaporkan_dengan_tanggalnya():
    rows = [(date(2026, 10, 5), 958, 958), (date(2026, 10, 6), 956, 41)]
    c = evaluate_field_coverage(rows)
    assert not c.ok
    assert c.troublemakers == ["2026-10-06 (41/956, 4.3%)"]
    assert c.worst_pct == 4.3

def test_tanpa_data_dianggap_ok_tapi_dilaporkan_kosong():
    c = evaluate_field_coverage([])
    assert c.ok and c.checked == 0

def test_jendela_membatasi_jumlah_sesi_yang_dicek():
    rows = [(date(2026, 10, d), 100, 100) for d in range(1, 6)]
    assert evaluate_field_coverage(rows, sessions=3).checked == 3

def test_pesan_menyebut_tanggal_dan_skrip_perbaikan():
    c = FieldCoverage(checked=1, troublemakers=["2026-07-14 (0/965, 0.0%)"], worst_pct=0.0)
    msg = format_field_coverage_message(c)
    assert "2026-07-14" in msg and "repair_foreign_flow" in msg
```

- [ ] **Step 2: Jalankan, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health.py -q`
Expected: FAIL — `ImportError: cannot import name 'FieldCoverage'`.

- [ ] **Step 3: Implementasi dataclass + dua fungsi murni**

`evaluate_field_coverage`: ambil `sessions` baris terakhir (`rows[-sessions:]`), `checked = len(...)`, cari yang `terisi < total`, format tiap pelanggar `f"{d} ({terisi}/{total}, {100*terisi/total:.1f}%)"`, `worst_pct = min(100*terisi/total)` (100.0 kalau kosong), `.ok = not troublemakers`. `format_field_coverage_message`: pesan Telegram HTML berisi judul `⚠️ Cakupan kolom EOD timpang`, daftar `troublemakers` (maks 5 pertama + `… (+N lagi)`), dan perintah perbaikan `python -m scripts.repair_foreign_flow --start-date <terlama> --end-date <terbaru>`.

- [ ] **Step 4: Jalankan tes murni, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_health.py -q`
Expected: PASS (tes lama + 5 tes baru).

- [ ] **Step 5: `check_field_coverage` (baca DB) + sambungkan ke `idx health` dan job `health_eod`**

`check_field_coverage` menjalankan satu query: `select trade_date, count(*) total, count(foreign_net) terisi from research.raw_eod group by 1 order by 1` → ambil `sessions` terakhir → `evaluate_field_coverage`. Kalau `dsn` kosong → kembalikan `FieldCoverage(checked=0, troublemakers=[], worst_pct=100.0)`.
Wiring (dua tempat saja):
- `cli.py:1252 cmd_health`: setelah `h = check_eod_health()`, panggil `c = check_field_coverage()`; cetak `format_field_coverage_message(c)` kalau `not c.ok`, dan pakai `sys.exit(1)` kalau `not h.ok or not c.ok`.
- `cli.py:298 _job_health_eod`: setelah blok `check_eod_health` yang ada, kirim `TelegramNotifier().send_message(format_field_coverage_message(c))` hanya kalau `not c.ok`.

- [ ] **Step 6: Uji nyata hari ini**

Run: `.venv\Scripts\idx.exe health`
Expected: tidak ada baris `⚠️ Cakupan kolom EOD timpang` (data sekarang 100% di 476 sesi), exit code 0.

- [ ] **Step 7: Lint + commit**

```bash
.venv\Scripts\ruff.exe check src tests scripts
git add src/idx_scraper/health.py src/idx_scraper/cli.py tests/test_health.py
git commit -m "feat(health): penjaga cakupan kolom EOD — celah kolom ketahuan dalam 1 sesi, bukan 9 bulan"
```

---

### Task 3: Alat perbaikan bertarget untuk celah kolom

**Files:**
- Create: `scripts/repair_foreign_flow.py`
- Create: `tests/test_repair_foreign_flow.py`
- Modify: `README.md` (tambah satu paragraf di bagian skrip)

**Interfaces:**
- Produces: `scripts.repair_foreign_flow.baris_perlu_tambal(rows) -> list[tuple]` (murni; terima baris EOD, kembalikan hanya yang `foreign_buy/sell/net` semuanya `None` → dilewati, dan bentuk payload `(fbuy, fsell, fnet, code, date)` untuk yang punya nilai).
- Consumes: `IDXClient.fetch_stock_summary`, `scripts.backfill_raw_eod.refresh_prices_pit`, tabel `research.raw_eod`.

- [ ] **Step 1: Tulis tes yang gagal**

```python
# tests/test_repair_foreign_flow.py
from datetime import date
from types import SimpleNamespace as NS
from scripts.repair_foreign_flow import baris_perlu_tambal

def test_baris_tanpa_foreign_dilewati():
    rows = [NS(code="AAAA", foreign_buy=None, foreign_sell=None, foreign_net=None)]
    assert baris_perlu_tambal(rows, date(2026, 7, 14)) == []

def test_baris_dengan_foreign_dibentuk_jadi_payload():
    rows = [NS(code="BBRI", foreign_buy=1.0, foreign_sell=0.5, foreign_net=0.5)]
    assert baris_perlu_tambal(rows, date(2026, 7, 14)) == [(1.0, 0.5, 0.5, "BBRI", date(2026, 7, 14))]
```

- [ ] **Step 2: Jalankan, pastikan gagal** — `.venv\Scripts\python.exe -m pytest tests/test_repair_foreign_flow.py -q` → `ImportError`.

- [ ] **Step 3: Implementasi skrip**

Argumen: `--start-date`, `--end-date`, `--pace` (default 4.0), `--limit` (batasi jumlah sesi, untuk uji cepat). Alur per sesi: hitung `sebelum` (`select count(*) … where trade_date=%s and foreign_net is null`) → fetch dengan backoff `(0, 30, 90)` + deteksi 403 (abort dengan pesan resume, sama seperti Task 1) → `executemany("update research.raw_eod set foreign_buy=%s, foreign_sell=%s, foreign_net=%s, ingested_at=now() where code=%s and trade_date=%s and foreign_net is null", payload)` → hitung `sesudah` dan cetak `terisi N/M`. **Hanya kolom foreign yang disentuh**; OHLCV tidak pernah ditulis. Akhiri dengan `refresh_prices_pit(dsn)`.
Docstring wajib mencatat bukti historis: 201 sesi / 192.613 kolom, `source='IDX'` semua, hash OHLCV 201 sesi 0 berubah, dan prosedur verifikasi ulang (hash pra/pasca per sesi).

- [ ] **Step 4: Tes lulus + lint** — `pytest tests/test_repair_foreign_flow.py -q` (2 passed) lalu `ruff check src tests scripts`.

- [ ] **Step 5: Uji nyata tanpa efek (0 kolom tersisa untuk ditambal)**

Run: `.venv\Scripts\python.exe -m scripts.repair_foreign_flow --start-date 2026-07-01 --end-date 2026-07-02 --pace 4`
Expected: `terisi 0/0` (sudah ditambal 17 Okt) dan tidak ada error; membuktikan guard `foreign_net is null` bekerja.

- [ ] **Step 6: Commit**

```bash
git add scripts/repair_foreign_flow.py tests/test_repair_foreign_flow.py README.md
git commit -m "feat(scripts): repair_foreign_flow — tambal kolom foreign NULL, idempoten, tanpa menyentuh OHLCV"
```

---

### Task 4: Jendela catch-up 5 → 21 hari bursa + runbook outage

**Files:**
- Modify: `src/idx_scraper/cli.py:919-924` (`_BACKFILL_HARI` + komentar)
- Modify: `tests/test_backfill_hari.py` (tambah tes di akhir)
- Modify: `README.md` (tambah runbook "setelah `idx serve` mati")

**Interfaces:**
- Produces: `cli._BACKFILL_HARI_DEFAULT = 21`; `cli._BACKFILL_HARI = int(os.getenv("IDX_BACKFILL_DAYS") or _BACKFILL_HARI_DEFAULT)`.
- Consumes: `cli._hari_bursa_mundur(mulai, jumlah)` (sudah ada, murni).

- [ ] **Step 1: Tulis tes yang gagal**

```python
# tests/test_backfill_hari.py
def test_default_backfill_hari_menutup_libur_panjang():
    """5 hari bursa tidak cukup melewati libur panjang; 21 hari bursa aman."""
    from idx_scraper import cli
    assert cli._BACKFILL_HARI_DEFAULT >= 20
    mulai = date(2026, 4, 20)  # sesudah rentetan libur Lebaran 2026-03-18..24
    mundur = cli._hari_bursa_mundur(mulai, cli._BACKFILL_HARI_DEFAULT)
    assert len(mundur) == cli._BACKFILL_HARI_DEFAULT
    assert (mulai - mundur[-1]).days >= 28  # menjangkau ≥4 minggu kalender

def test_env_masih_bisa_menimpa_default(monkeypatch):
    monkeypatch.setenv("IDX_BACKFILL_DAYS", "3")
    import importlib
    from idx_scraper import cli
    importlib.reload(cli)
    assert cli._BACKFILL_HARI == 3
    monkeypatch.delenv("IDX_BACKFILL_DAYS")
    importlib.reload(cli)
```

- [ ] **Step 2: Jalankan, pastikan gagal** — `.venv\Scripts\python.exe -m pytest tests/test_backfill_hari.py -q` → `AttributeError: module 'idx_scraper.cli' has no attribute '_BACKFILL_HARI_DEFAULT'`.

- [ ] **Step 3: Implementasi**

Ganti blok komentar + konstanta di `cli.py:919-924`: `_BACKFILL_HARI_DEFAULT = 21` dengan komentar kenapa (5 hari bursa = 1 minggu kalender; outage 2–3 minggu sebelumnya kehilangan sesi diam-diam karena cek berikutnya sudah melewati tanggal itu, dan karena retensi IDX hanya ±2 tahun, tugas berikutnya bisa permanen). Perbarui juga kalimat penutup docstring `_backfill_hari_lampau` (`cli.py:955-971`) supaya menyebut angka 21 dan skrip manual untuk lubang lebih tua.

- [ ] **Step 4: Tes lulus + suita penuh** — `pytest tests -q` → semua hijau (termasuk `test_backfill_hari.py` lama yang memakai `monkeypatch`/`beku`).

- [ ] **Step 5: README runbook (outage)**

Tambah di bagian operasi: *"Kalau `idx serve` mati lebih lama dari `IDX_BACKFILL_DAYS` (default 21 hari bursa): (1) `python -m scripts.check_eod_gaps --dry-run` untuk melihat lubang, (2) `python -m scripts.check_eod_gaps` untuk menariknya (jeda 4 s, backoff sabar), (3) `python -m scripts.repair_foreign_flow --start-date … --end-date …` kalau cakupan kolom ikut timpang, (4) verifikasi lewat `idx health`."*

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/cli.py tests/test_backfill_hari.py README.md
git commit -m "fix(catchup): jendela backfill hari lampau 5 -> 21 hari bursa + runbook outage"
```

---

### Task 5: Commit skrip operator yang belum masuk repo

**Files:**
- Add: `scripts/check_eod_gaps.py`, `scripts/backfill_index_history_yahoo.py` (sudah ada di working tree, belum ter-commit)

- [ ] **Step 1: Pastikan lint & format bersih**

Run: `.venv\Scripts\ruff.exe check scripts/check_eod_gaps.py scripts/backfill_index_history_yahoo.py` lalu `.venv\Scripts\ruff.exe format --check scripts/check_eod_gaps.py scripts/backfill_index_history_yahoo.py`
Expected: `All checks passed!` dan `2 files already formatted`.
Catatan: jangan menjalankan `format --check` untuk seluruh folder `scripts/` — `scripts/backfill_raw_eod.py` bawaan repo belum ter-`ruff format`, jadi cakupan luas akan gagal karena file lama, bukan karena perubahan plan ini.

- [ ] **Step 2: Uji jalan tanpa efek samping**

Run: `.venv\Scripts\python.exe -m scripts.check_eod_gaps --dry-run`
Expected: `[gap] … hari kerja tanpa data: 48` — semuanya libur bursa (0 lubang data) sesudah penambalan 17 Okt 2026.

- [ ] **Step 3: Commit**

```bash
git add scripts/check_eod_gaps.py scripts/backfill_index_history_yahoo.py
git commit -m "scripts: penambal lubang sesi (check_eod_gaps) + pengisi riwayat IHSG tanpa menyentuh index_quotes"
```

---

## Verifikasi Akhir (setelah semua task)

- [ ] `.venv\Scripts\ruff.exe check src tests scripts` → `All checks passed!`
- [ ] `.venv\Scripts\python.exe -m pytest tests -q` → semua hijau
- [ ] `.venv\Scripts\python.exe -m scripts.backfill_raw_eod --start-date 2026-10-12 --end-date 2026-10-16` → `dilewati 5`, tanpa traceback
- [ ] `.venv\Scripts\python.exe -m scripts.check_eod_gaps --dry-run` → 48 hari = libur
- [ ] `idx health` → exit 0, tanpa peringatan cakupan kolom
- [ ] `git status --short` → bersih (tidak ada file baru yang menggantung)
