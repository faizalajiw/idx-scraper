# P4 — Gerbang Rezim, Pemeringkat Kandidat, dan Kesegaran Tampil: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Memasang gerbang yang bisa gagal (kesegaran D1 + rezim D-Q5), satu kolam kandidat yang terukur (D4), skala/tie-break yang jujur (D3), dan menampilkan status kesegaran `segar/basi/tak diketahui` di dashboard.

**Architecture:** Logika keputusan ditulis sebagai fungsi **murni** di modul kecil (mudah diuji tanpa DB), lalu dijahit ke permukaan yang sudah ada: `api/services.py` + endpoint FastAPI untuk backend, dan `lib/hooks.ts` + satu komponen badge untuk web. Tidak ada model baru (spec D7) dan tidak ada tabel baru — semua angka berasal dari `research.latest_pit`, `research.regime_daily`, dan `research.quarantine_eod` yang sudah ada.

**Tech Stack:** Python 3 + pytest (pure-first, tanpa DB/jaringan di tes), FastAPI + Pydantic; Next.js 16.3.5 (App Router) + SWR + Tailwind v4.

**Spec:** [`docs/superpowers/specs/2026-10-18-spec-mesin-keputusan-design.md`](../specs/2026-10-18-spec-mesin-keputusan-design.md) — §5 (D1–D4), §6.1, §6.4, §6.5, §6.6, §7. Plan ini berargumen dari spec itu; pelaksana membaca keduanya.

## Global Constraints

- Bahasa Indonesia untuk komentar, docstring, nama tes, pesan pengguna, dan teks UI. Kode/pengenal tetap Inggris.
- `ruff check src tests scripts` **wajib bersih**; `line-length = 100` (`pyproject.toml:59`). `ruff format` bukan gerbang di repo ini — jangan format ulang berkas yang tidak kau sentuh.
- Tes **tidak boleh** menyentuh DB atau jaringan (`pytest.ini`: `addopts = -q`, `pythonpath = src .`). Seluruh suite harus tetap lulus dalam < 60 detik.
- Semua angka analitik dari `research.latest_pit`; **harga mentah** untuk level yang ditransaksikan manusia (entry/stop/target), **harga terkoreksi** untuk yang diukur sebagai hasil (spec §6.4).
- Nilai ambang yang dipakai bersama **harus konstanta bernama, di satu tempat** (spec D4): `THICK_VALUE_PERCENTILE = 60`, `THICK_WINDOW_SESSIONS = 5`, `THICK_MIN_SESSIONS = 3` — hidup di `research/pool.py` dan tidak ditulis ulang di berkas lain.
- Nilai milik spec yang tidak boleh ditulis ulang di tempat lain: `ADX_TREND = 25.0` (`analysis.py:30`), `MIN_DAY_VALUE = 5.0e8` (`recommend.py:57`), `BAND_SHARES = (0.75, 0.90, 0.98)` (`recommend.py:69`), `EDGE_GATE_T = 1.5` & `MIN_EDGE = 0.0015` (`score_eval.py`), `EOD_DEADLINE_HOUR = 16` (`health.py:54`).
- Tidak menambah dependensi, tidak menambah menu baru (maksimal satu halaman digarap — spec D2), tidak menyentuh `score_eval.py` (itu hasil P3, sudah ada tesnya).
- Setiap perubahan perilaku yang dilihat pengguna disebut eksplisit di commit message dan di `RENCANA-P0-P7.md`.

## Review Focus

Lima kelas masukan/keadaan yang spec implikasikan tapi tidak otomatis terjaga tes manapun — masing-masing dipasangi tes di tugas pemiliknya:

1. **Pagi hari bursa sebelum jam batas EOD.** `health.expected_trading_date` mengembalikan *hari ini* kalau hari ini hari bursa. Tanpa koreksi jam batas, setiap Senin pagi seluruh papan akan terbaca "basi" dan verdict-nya ditahan — padahal datanya memang belum waktunya ada. Perilaku yang benar: sebelum `EOD_DEADLINE_HOUR` (16:00 WIB), sesi yang diharapkan adalah sesi bursa **sebelumnya**. → tes di Task 1.
2. **Rezim dari data yang basi.** Gerbang rezim yang memblokir halaman berdasar label lama akan membisukan layar karena masa lalu. Perilaku yang benar: rezim basi **tidak memblokir**, hanya ditandai (spec D1: bagian berindeks membawa penandanya sendiri). → tes di Task 4.
3. **Kejadian bursa tipis** (`value` berhingga kurang dari 20 nama di satu sesi): persentil ke-60 tak bisa dipercaya. Perilaku yang benar: sesi itu jatuh ke lantai absolut, papan tidak dikosongkan tanpa penjelasan. → tes di Task 5.
4. **Nama yang sudah ada di `latest_pit` tapi masuk `quarantine_eod`.** Perilaku yang benar: keluar dari kolam, dan karena grade dihitung dari kolam tunggal, ia tidak boleh muncul bergrade. → tes di Task 5.
5. **Perubahan status kesegaran di dalam jendela cache 30 menit.** Verdict harus ditahan/dilepas **sesudah** cache dibaca, supaya status baru tidak terkunci setengah jam dan papan tidak menyajikan grade lama tanpa penjelasan. → tes di Task 3 (`test_penahanan_diterapkan_sesudah_cache_dibaca`).

---

## Struktur berkas

| Berkas | Tanggung jawab | Status |
|---|---|---|
| `src/idx_scraper/freshness.py` | Tiga status kesegaran, sesi yang diharapkan, penahanan verdict, pesan | **baru** |
| `src/idx_scraper/research/pool.py` | Definisi kolam tunggal + gerbang ketebalan nilai (D4) | **baru** |
| `src/idx_scraper/research/regime.py` | + gerbang rezim yang bisa menolak | ubah |
| `src/idx_scraper/research/recommend.py` | + peringkat persentil, tie-break terbuka, label keyakinan (D3); − lantai likuiditas dari `score_candidate` | ubah |
| `src/idx_scraper/research/recommendation_log.py` | `candidates_for_date` memakai kolam tunggal; pembaca `quarantine_eod` | ubah |
| `src/idx_scraper/api/services.py` | + `get_freshness()` | ubah |
| `src/idx_scraper/api/analytics.py` | Papan & emiten membawa kesegaran + penahanan; gerbang di respons rezim | ubah |
| `src/idx_scraper/api/app.py`, `api/schemas.py` | Endpoint `GET /api/freshness` + skema respons | ubah |
| `idx-web/lib/types.ts`, `lib/hooks.ts` | Tipe `Freshness` + `useFreshness` | ubah |
| `idx-web/components/FreshnessBadge.tsx` | Badge tiga status | **baru** |
| `idx-web/app/page.tsx`, `app/rekomendasi/page.tsx`, `components/RegimeBanner.tsx`, `components/MarketRegimeCard.tsx` | Pemasangan badge + penanda berindeks | ubah |

Tes baru: `tests/test_freshness.py`, `tests/test_pool.py`. Tes yang bertambah: `tests/test_regime.py`, `tests/test_recommend.py`, `tests/test_recommendation_log.py`.

---

## Task 1: Kesegaran — tiga status, satu fungsi murni

**Files:**
- Create: `src/idx_scraper/freshness.py`
- Test: `tests/test_freshness.py`

**Interfaces:**
- Produces:
  - `SEGAR: str = "segar"`, `BASI: str = "basi"`, `TAK_DIKETAHUI: str = "tak diketahui"`
  - `expected_session(now: datetime, holidays: set[date] | None = None) -> date` — sesi bursa yang seharusnya sudah punya data pada `now`
  - `verdict(panel_latest: str | None, expected: str | None, *, index_latest: str | None = None) -> dict[str, Any]` → kunci: `status`, `withhold`, `reason`, `panel_latest`, `expected`, `index_latest`, `index_status`

- [ ] **Step 1: Tulis tes yang gagal**

```python
# tests/test_freshness.py
from datetime import datetime

from idx_scraper.freshness import (
    BASI, SEGAR, TAK_DIKETAHUI, expected_session, verdict,
)


def test_kesegaran_segar_saat_panel_sama_dengan_sesi_diharapkan():
    v = verdict("2026-10-16", "2026-10-16")
    assert v["status"] == SEGAR and v["withhold"] is False


def test_kesegaran_basi_menyebut_tanggal_dan_sebabnya():
    v = verdict("2026-10-14", "2026-10-16")
    assert v["status"] == BASI and v["withhold"] is True
    assert "2026-10-14" in v["reason"] and "2026-10-16" in v["reason"]


def test_kesegaran_tak_diketahui_saat_sesi_diharapkan_tak_bisa_ditentukan():
    v = verdict("2026-10-16", None)
    assert v["status"] == TAK_DIKETAHUI and v["withhold"] is True


def test_kesegaran_panel_kosong_bukan_segar():
    v = verdict(None, "2026-10-16")
    assert v["status"] == TAK_DIKETAHUI and v["withhold"] is True


def test_penanda_indeks_terpisah_tidak_menularkan_status_ke_halaman():
    v = verdict("2026-10-16", "2026-10-16", index_latest="2026-10-09")
    assert v["status"] == SEGAR and v["index_status"] == BASI


def test_sesi_hari_ini_belum_wajib_sebelum_jam_batas():
    # Jumat 10:00 WIB: sesi yang diharapkan masih Kamis, bukan Jumat.
    now = datetime(2026, 10, 16, 10, 0)
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-15"


def test_sesi_hari_ini_wajib_setelah_jam_batas():
    now = datetime(2026, 10, 16, 17, 0)
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-16"


def test_akhir_pekan_mundur_ke_jumat():
    now = datetime(2026, 10, 18, 9, 0)  # Minggu
    assert expected_session(now, holidays=set()).isoformat() == "2026-10-16"
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'idx_scraper.freshness'`

- [ ] **Step 3: Implementasi `src/idx_scraper/freshness.py`**

Docstring modul: satu paragraf yang menyebut spec D1 dan menyatakan aturan kerasnya ("verdict yang lahir dari data basi ditahan"). Impor `EOD_DEADLINE_HOUR` dan `expected_trading_date` dari `.health` (jangan salin angkanya).

Tabel keputusan `verdict` yang harus dipenuhi (persis, tidak boleh ada cabang lain):

| `panel_latest` | `expected` | `status` | `withhold` |
|---|---|---|---|
| apa pun | `None` | `TAK_DIKETAHUI` | `True` |
| `None` | ada | `TAK_DIKETAHUI` | `True` |
| `>= expected` | ada | `SEGAR` | `False` |
| `< expected` | ada | `BASI` | `True` |

`index_status` dihitung dengan tabel yang sama dari `index_latest` vs `expected`, dan **tidak pernah** mengubah `status` halaman.

`expected_session(now, holidays=None)`: kalau `now.date()` sama dengan hasil `expected_trading_date(now, holidays)` **dan** `now.hour < EOD_DEADLINE_HOUR`, mundur satu hari lalu hitung lagi; selain itu kembalikan `expected_trading_date(now, holidays)`.

Isi `reason` dengan kalimat Indonesia yang menyebut tanggalnya: `segar` → `f"data dari sesi terakhir yang diharapkan ({expected})"`; `basi` → `f"data dari {panel_latest}, sesi yang lebih baru sudah lewat ({expected})"`; `tak diketahui` karena `expected` kosong → `"sesi pasar terakhir tidak bisa ditentukan"`; karena panel kosong → `"tidak ada data sesi sama sekali"`.

- [ ] **Step 4: Jalankan dan pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v`
Expected: PASS (8 tes)

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/freshness.py tests/test_freshness.py
git commit -m "feat(freshness): tiga status kesegaran + sesi yang diharapkan sadar jam batas EOD"
```

---

## Task 2: Kesegaran terbaca dari DB dan terpapar sebagai endpoint

**Files:**
- Modify: `src/idx_scraper/freshness.py`, `src/idx_scraper/api/services.py:1135` (fungsi baru sesudah `get_data_health`), `src/idx_scraper/api/app.py:192` (endpoint baru sesudah `/api/data-health`), `src/idx_scraper/api/schemas.py` (model baru sesudah `DataHealth`)
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `verdict`, `expected_session` (Task 1) — dan pola query yang sudah ada di `api/services.py:1156-1163`
- Produces:
  - `session_bases(cur: Any) -> tuple[str | None, str | None]` — `(panel_latest, index_latest)` sebagai `YYYY-MM-DD`
  - `current(cur: Any | None = None, *, now: datetime | None = None) -> dict[str, Any]` — hasil `verdict` + `checked_at` (ISO)
  - `format_message(f: Mapping[str, Any]) -> str`
  - `services.get_freshness() -> dict[str, Any]` (tipis: buka cursor, panggil `freshness.current`)
  - `GET /api/freshness` → `response_model=Freshness`

- [ ] **Step 1: Tulis tes yang gagal**

Tambahkan ke `tests/test_freshness.py` — pakai cursor palsu, **jangan** sentuh DB:

```python
class _Cur:
    """Cursor palsu: fetchone mengembalikan nilai berikutnya, berurutan."""

    def __init__(self, values):
        self._values = list(values)
        self.queries: list[str] = []

    def execute(self, sql, params=None):
        self.queries.append(sql)

    def fetchone(self):
        return {"d": self._values.pop(0)}


def test_current_memakai_basis_panel_untuk_verdict():
    cur = _Cur(["2026-10-16", "2026-10-09"])  # panel segar, indeks tertinggal
    out = current(cur, now=datetime(2026, 10, 18, 9, 0))
    assert out["status"] == SEGAR and out["index_status"] == BASI
    assert out["expected"] == "2026-10-16"


def test_current_menanyakan_dua_basis_yang_berbeda():
    cur = _Cur(["2026-10-16", "2026-10-16"])
    current(cur, now=datetime(2026, 10, 18, 9, 0))
    assert any("latest_pit" in q for q in cur.queries)
    assert any("index_quotes" in q for q in cur.queries)


def test_pesan_kesegaran_menyebut_tanggal_dan_alasan():
    v = verdict("2026-10-14", "2026-10-16")
    assert "2026-10-14" in format_message(v) and "2026-10-16" in format_message(v)
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v`
Expected: FAIL — `ImportError: cannot import name 'current'`

- [ ] **Step 3: Implementasi**

`session_bases`: dua query, **sama persis** dengan yang dipakai `api/services.py:1156-1163` supaya tidak ada dua definisi kesegaran — panel dari `select max(trade_date) from research.latest_pit`; indeks dari `max((captured_at at time zone 'Asia/Jakarta')::date) from index_quotes where code = 'COMPOSITE' and extract(hour ...) >= 16`. Kembalikan `None` kalau kosong, selain itu ISO `YYYY-MM-DD`.

`current`: panggil `session_bases`, hitung `expected = expected_session(now or datetime.now(WIB), parse_holidays())`, kembalikan hasil `verdict(...)` ditambah `checked_at` dan `basis` (`"panel"`).

`format_message`: satu kalimat Indonesia, mis. `"Kesegaran: basi — data dari 2026-10-14, sesi yang lebih baru sudah lewat (2026-10-16)."`

Sisanya: `services.get_freshness()` + model `Freshness` di `schemas.py` (field: `status`, `withhold`, `reason`, `panel_latest`, `index_latest`, `index_status`, `expected`, `checked_at`) + endpoint `GET /api/freshness` di `app.py` dengan docstring satu paragraf yang menyebut spec D1.

- [ ] **Step 4: Jalankan dan pastikan lulus, lalu bersihkan lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v`
Expected: PASS (11 tes)

Run: `.venv\Scripts\python.exe -m ruff check src tests scripts`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/freshness.py src/idx_scraper/api/services.py src/idx_scraper/api/app.py src/idx_scraper/api/schemas.py tests/test_freshness.py
git commit -m "feat(api): endpoint /api/freshness + pembaca dua basis kesegaran"
```

---

## Task 3: Verdict ditahan saat basi

**Files:**
- Modify: `src/idx_scraper/freshness.py`, `src/idx_scraper/api/analytics.py:2690` (`get_recommendations`), `src/idx_scraper/api/analytics.py:2759` (`get_stock_recommendation`)
- Test: `tests/test_freshness.py`

**Interfaces:**
- Consumes: `verdict` (Task 1), `services.get_freshness()` (Task 2)
- Produces: `withhold(rows: list[dict[str, Any]], f: Mapping[str, Any]) -> list[dict[str, Any]]`

- [ ] **Step 1: Tulis tes yang gagal**

```python
def _row(**extra):
    return {"code": "BBRI", "score": 52.0, "grade": "A", "position_pct": 12.5,
            "entry_ref": 4200.0, "stop": 3900.0, "target": 4900.0, **extra}


def test_baris_kehilangan_grade_dan_ukuran_saat_basi():
    out = withhold([_row()], verdict("2026-10-14", "2026-10-16"))
    assert out[0]["grade"] is None and out[0]["position_pct"] is None


def test_angka_dan_level_tetap_terlihat_saat_basi():
    out = withhold([_row()], verdict("2026-10-14", "2026-10-16"))
    assert out[0]["score"] == 52.0 and out[0]["stop"] == 3900.0


def test_baris_utuh_saat_segar():
    out = withhold([_row()], verdict("2026-10-16", "2026-10-16"))
    assert out[0]["grade"] == "A" and out[0]["position_pct"] == 12.5


def test_penahanan_tidak_menghapus_baris_hanya_labelnya():
    assert len(withhold([_row(), _row(code="TLKM")], verdict(None, "2026-10-16"))) == 2


def test_penahanan_tidak_mengubah_dict_masukan():
    rows = [_row()]
    withhold(rows, verdict("2026-10-14", "2026-10-16"))
    assert rows[0]["grade"] == "A"


def test_penahanan_diterapkan_sesudah_cache_dibaca(monkeypatch):
    """Cache 30 menit tidak boleh mengunci status segar/basi (Review Focus 5)."""
    import pandas as pd

    from idx_scraper.api import analytics

    # Frame minimal: 3 kode pada satu tanggal, gaya fixture di
    # tests/test_recommendation_log.py:24-60 (kolom yang dibaca candidates_for_date).
    metrics = pd.DataFrame([...])
    ctx = {
        "metrics": metrics, "histories": {}, "factor_adj": {}, "factor_validated": False,
        "factor_note": "-", "broker_adj": {}, "broker_validated": False, "broker_note": "-",
    }
    monkeypatch.setattr(analytics, "_recommendation_context", lambda: ctx)
    monkeypatch.setattr(analytics.services, "get_freshness", lambda: verdict("2026-10-14", "2026-10-16"))
    cache: dict = {}
    monkeypatch.setattr(analytics, "_research_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(
        analytics, "_research_cache_put", lambda k, v, ttl=None: cache.__setitem__(k, v)
    )

    out = analytics.get_recommendations()
    assert out["withheld"] is True
    assert all(r["grade"] is None for r in out["rows"])
    # Yang tersimpan di cache tetap utuh: status berikutnya tidak mewarisi penahanan.
    assert cache["recommendations:20:C"]["rows"][0]["grade"] is not None
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v`
Expected: FAIL — `ImportError: cannot import name 'withhold'`

- [ ] **Step 3: Implementasi `withhold`**

Kembalikan **salinan** tiap baris (`{**row}`), supaya dict di cache tidak termutasi. Bila `f["withhold"]` False: kembalikan salinan apa adanya. Bila True: set `grade = None` dan `position_pct = None`, dan tambahkan `withheld = True` pada tiap baris. Docstring menyebut aturan spec D1 apa adanya: "yang tampil hanya angka, tanggalnya, dan sebabnya".

- [ ] **Step 4: Jahit ke papan dan ke emiten**

Di `get_recommendations` (`analytics.py:2690`) **sesudah** baris cache dibaca (`:2706-2708`) dan **sesudah** `out` disusun:

1. `f = get_freshness()` (impor dari `.services`);
2. kembalikan `{**out, "freshness": f, "withheld": bool(f["withhold"]), "rows": withhold(out["rows"], f)}` — jangan menulis balik ke dict yang di-cache, karena objek yang sama hidup 30 menit.
3. Lakukan hal yang sama di `get_stock_recommendation` (`:2759`): `freshness`, `withheld`, dan tahan `grade` pada kandidat emiten itu.

- [ ] **Step 5: Jalankan tes + lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_freshness.py -v` → PASS (17 tes)
Run: `.venv\Scripts\python.exe -m pytest` → seluruh suite lulus
Run: `.venv\Scripts\python.exe -m ruff check src tests scripts` → bersih

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/freshness.py src/idx_scraper/api/analytics.py tests/test_freshness.py
git commit -m "feat(api): tahan verdict papan saat data basi (spec D1)"

# Perubahan perilaku: respons /api/recommendations & /api/stocks/{code}/recommendation
# kini memuat freshness/withheld, dan grade hilang saat data basi.
```

---

## Task 4: Gerbang rezim yang bisa menolak

**Files:**
- Modify: `src/idx_scraper/research/regime.py` (sesudah `summary`), `src/idx_scraper/api/analytics.py:62` (`get_market_regime`), `src/idx_scraper/api/schemas.py:772` (`MarketRegime`)
- Test: `tests/test_regime.py`

**Interfaces:**
- Consumes: `ADX_TREND` dari `..analysis` (`analysis.py:30`); `freshness.verdict`
- Produces:
  - `REJECTING_REGIMES: tuple[str, ...] = ("TRENDING_DOWN",)`
  - `gate(regime: str | None, adx: float | None, vol_state: str | None = None, *, regime_fresh: bool = True) -> dict[str, Any]` → `{blocked, reason, regime, adx, vol_state, volatile, regime_fresh}`
  - `gate_current(dsn: str) -> dict[str, Any]` — baca baris `research.regime_daily` terbaru

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_gerbang_menolak_saat_tren_turun_dengan_adx_kuat():
    g = gate("TRENDING_DOWN", 31.5)
    assert g["blocked"] is True and "tren turun" in g["reason"]


def test_gerbang_tidak_menolak_saat_adx_di_bawah_ambang_tren():
    assert gate("TRENDING_DOWN", 20.0)["blocked"] is False


def test_gerbang_tidak_menolak_saat_rezim_transisi_atau_none():
    assert gate("TRANSITION", 30.0)["blocked"] is False
    assert gate(None, None)["blocked"] is False


def test_gerbang_tidak_menolak_saat_ranging():
    assert gate("RANGING", 18.0)["blocked"] is False


def test_gerbang_volatile_ditandai_tapi_tidak_memblokir():
    g = gate("TRENDING_UP", 30.0, "VOLATILE")
    assert g["blocked"] is False and g["volatile"] is True


def test_gerbang_basi_tidak_memblokir_tapi_ditandai():
    g = gate("TRENDING_DOWN", 31.5, "NORMAL", regime_fresh=False)
    assert g["blocked"] is False and g["regime_fresh"] is False
    assert "basi" in g["reason"]


def test_alasan_gerbang_selalu_terisi():
    assert all(gate(r, 30.0)["reason"] for r in ("TRENDING_DOWN", "TRENDING_UP", "RANGING", "TRANSITION", None))
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_regime.py -v`
Expected: FAIL — `ImportError: cannot import name 'gate'`

- [ ] **Step 3: Implementasi `gate` dan `gate_current`**

Aturan (docstring menyebut spec §6.5 + keputusan pemilik Q5: "hanya diam saat rezim eksplisit menolak, dan alasannya disebut di layar"):

- `blocked` True **hanya** bila `regime in REJECTING_REGIMES` **dan** `adx is not None and adx >= ADX_TREND` **dan** `regime_fresh`.
- Rezim `TRANSITION`, `RANGING`, `TRENDING_UP`, atau `None` **tidak** memblokir — alasannya dituliskan apa adanya (mis. `"rezim transisi — layar tidak dibisukan"`).
- `vol_state == "VOLATILE"` mengisi `volatile = True` tanpa memblokir; plafon ukuran posisinya urusan P5.
- `regime_fresh` False → `blocked` False dan alasan menyebut rezim basi.

`gate_current(dsn)`: baca `trade_date, regime, adx, vol_state` terbaru dari `research.regime_daily`, hitung `regime_fresh` dengan `freshness.verdict(str(trade_date), expected)["status"] == SEGAR`, kembalikan `gate(...)`.

- [ ] **Step 4: Terpapar di API**

Tambah `gate: dict | None = None` pada model `MarketRegime` (`schemas.py:772`), lalu di `get_market_regime` (`analytics.py:62`) isi `"gate"` dari `regime.gate(...)` memakai `regime`, `adx`, `vol_state`, dan tanggal bar indeks terakhir yang **dipakai perhitungan itu** untuk menetapkan `regime_fresh` (baca fungsi itu dulu — jangan mengarang bentuk hasilnya). Bila `regime_fresh` False, halaman **tidak** dibisukan (spec D1).

- [ ] **Step 5: Jalankan tes + lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_regime.py -v` → PASS (10 tes lama + 7 baru)
Run: `.venv\Scripts\python.exe -m ruff check src tests scripts` → bersih

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/research/regime.py src/idx_scraper/api/analytics.py src/idx_scraper/api/schemas.py tests/test_regime.py
git commit -m "feat(regime): gerbang yang hanya menolak saat tren turun terkonfirmasi (spec 6.5)"
```

---

## Task 5: Satu kolam, ambang ketebalan terukur (D4)

**Files:**
- Create: `src/idx_scraper/research/pool.py`
- Modify: `src/idx_scraper/research/recommend.py:640-642` (hapus lantai dari `score_candidate`), `src/idx_scraper/research/recommendation_log.py:298-362` (`candidates_for_date`), `src/idx_scraper/api/analytics.py:2739` (panggilan papan)
- Test: `tests/test_pool.py`, `tests/test_recommend.py`, `tests/test_recommendation_log.py`

**Interfaces:**
- Consumes: `MIN_DAY_VALUE` (`recommend.py:57`), `MIN_MARKET_HISTORY` (`recommend.py:48`)
- Produces:
  - `THICK_VALUE_PERCENTILE = 60`, `THICK_WINDOW_SESSIONS = 5`, `THICK_MIN_SESSIONS = 3`, `POOL_MIN_NAMES = 20`
  - `value_thresholds(metrics: pd.DataFrame) -> dict[pd.Timestamp, float]` — ambang nilai per sesi (persentil lintas saham; sesi dengan < `POOL_MIN_NAMES` nilai berhingga → `MIN_DAY_VALUE`)
  - `thick_flags(metrics: pd.DataFrame) -> pd.Series` — True per baris bila `value` baris itu ≥ ambang sesinya
  - `pool_members(metrics: pd.DataFrame, date_value: Any, *, quarantined: set[str] | None = None) -> set[str]`
  - `load_quarantined_codes(dsn: str, sessions: int = 30) -> set[str]`
  - `candidates_for_date(..., pool: set[str] | None = None)` — `pool=None` berarti hitung sendiri lewat `pool_members`

- [ ] **Step 1: Tulis tes yang gagal**

```python
# tests/test_pool.py
def _frame(spec):  # (code, date, value) -> DataFrame
    ...


def test_ambang_tebal_adalah_persentil_enam_puluh_lintas_saham():
    # 100 nama, nilai 1..100 juta → ambang sesi ≈ 60,4 juta (interpolasi linear)
    m = _frame([(f"C{i:03d}", "2026-10-01", float(i)) for i in range(1, 101)])
    th = value_thresholds(m)[pd.Timestamp("2026-10-01")]
    assert 60.0 <= th < 61.0


def test_satu_lonjakan_tidak_cukup_tiga_dari_lima():
    # lima sesi, nilai tipis di 4 sesi dan satu lonjakan → di luar kolam
    ...


def test_konsisten_tiga_dari_lima_masuk_kolam():
    ...


def test_kolam_jatuh_ke_lantai_absolut_kalau_lintas_saham_terlalu_tipis():
    # hanya 5 nama di sesi itu → ambang = MIN_DAY_VALUE, papan tidak dikosongkan
    ...


def test_kode_karantina_tidak_masuk_kolam():
    ...


def test_kolam_memakai_satu_daftar_untuk_semua_grade():
    # candidates_for_date(pool=...) → grade dihitung dari kolam yang sama
    ...
```

```python
# tests/test_recommend.py — tes lama pindah, bukan dihapus
def test_score_candidate_tidak_lagi_menyaring_likuiditas():
    """Lantai likuiditas pindah ke research.pool (satu kolam, spec 6.6)."""
    c = score_candidate(base_metrics(value=1.0e8))
    assert c is not None


def test_score_candidate_masih_menolak_sinyal_sell_dan_histori_pendek():
    assert score_candidate(base_metrics(signal="SELL")) is None
    assert score_candidate(base_metrics(hist_days=30)) is None
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_pool.py tests/test_recommend.py -v`
Expected: FAIL — modul `pool` belum ada; `test_score_candidate_tidak_lagi_menyaring_likuiditas` gagal karena `score_candidate` masih menolak `value=1.0e8`.

- [ ] **Step 3: Implementasi `research/pool.py`**

- `value_thresholds`: kelompokkan per `date`, ambil `value` berhingga, buang `NaN`. Kalau jumlahnya < `POOL_MIN_NAMES` → `MIN_DAY_VALUE`; selain itu kuadratil `THICK_VALUE_PERCENTILE / 100` dengan interpolasi linear (`numpy.percentile`, `method="linear"`).
- `thick_flags`: bandingkan `value` baris dengan ambang tanggalnya; `NaN` → False.
- `pool_members`: untuk tiap kode, ambil `THICK_WINDOW_SESSIONS` sesi terakhir **sampai dengan** `date_value`; kode masuk kolam bila (a) jumlah sesi tebal ≥ `THICK_MIN_SESSIONS`, (b) `hist_days` sesi itu ≥ `MIN_MARKET_HISTORY`, (c) `signal != "SELL"`, (d) kode tidak ada di `quarantined`.
- `load_quarantined_codes`: `select distinct code from research.quarantine_eod where trade_date >= current_date - %s` (read-only, `sessions * 2` hari kalender), kode di-`upper()`.
- Docstring modul menyebut spec D4 dan menegaskan: **satu** kolam; grade A/B/C dihitung dari kolam ini, bukan dari daftar terpisah.

- [ ] **Step 4: Jahit**

1. Hapus blok `MIN_DAY_VALUE` di `score_candidate` (`recommend.py:640-642`); perbarui docstring-nya: penyaringan likuiditas pindah ke `research/pool.py`. `MIN_DAY_VALUE` tetap ada sebagai konstanta lantai absolut yang dipakai `pool.py`.
2. `candidates_for_date`: tambah argumen `pool: set[str] | None = None`; bila `None`, hitung `pool_members(metrics, date_value, quarantined=...)`; lewati kode di luar kolam **sebelum** `score_candidate`.
3. `analytics.get_recommendations` + `get_stock_recommendation`: kirim `pool=pool_members(...)` (sekali, dari cache context yang sama seperti metrik) supaya kedua permukaan memakai kolam identik; `quarantined` dari `load_quarantined_codes` dengan penanganan `try/except` yang mencetak `[warn]` (pola yang sama dengan lapisan faktor di `analytics.py:2629`).
4. Laporkan **berapa banyak nama yang keluar** (spec §6.6 menuntut definisi kolam yang eksplisit): tambahkan `"pool_size"` pada respons `get_recommendations` (jumlah anggota kolam sebelum `limit`/`min_grade` dipotong), berdampingan dengan `"scanned"` dan `"total_candidates"` yang sudah ada (`analytics.py:2726-2731`).

- [ ] **Step 5: Jalankan tes + lint + suite penuh**

Run: `.venv\Scripts\python.exe -m pytest tests/test_pool.py tests/test_recommend.py tests/test_recommendation_log.py -v` → PASS
Run: `.venv\Scripts\python.exe -m pytest` → seluruh suite lulus
Run: `.venv\Scripts\python.exe -m ruff check src tests scripts` → bersih

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/research/pool.py src/idx_scraper/research/recommend.py src/idx_scraper/research/recommendation_log.py src/idx_scraper/api/analytics.py tests/test_pool.py tests/test_recommend.py tests/test_recommendation_log.py
git commit -m "feat(pool): satu kolam kandidat berambang persentil-60 & 3-dari-5 sesi (spec D4)"

# Perubahan perilaku: score_candidate tidak lagi menyaring likuiditas; kolam
# ditentukan research.pool, dan grade A/B/C dihitung dari kolam itu.
```

---

## Task 6: Skala yang bisa dibandingkan + tie-break terbuka (D3)

**Files:**
- Modify: `src/idx_scraper/research/recommend.py:245-303` (`candidate_rank_key`, `assign_grades`), `src/idx_scraper/research/recommendation_log.py:351-362`
- Test: `tests/test_recommend.py`, `tests/test_recommendation_log.py`

**Interfaces:**
- Consumes: `NEUTRAL_SCORE` (`recommend.py:158`), `validated_horizons()`, `candidate_rank_key`
- Produces:
  - `pool_percentile(scores: Iterable[float]) -> list[float]` — peringkat persentil 0–100, seri dapat nilai sama (rata-rata)
  - `own_history_percentile(metrics: pd.DataFrame, code: str, horizon: int) -> float | None` — persentil skor terakhir emiten itu **dalam riwayatnya sendiri**; dihitung dari skor dasar (tanpa lapisan faktor/broker, karena lapisan itu tak bisa dihitung ulang per sesi), `None` bila riwayat < 20 baris
  - `RANK_BASIS: tuple[str, ...] = ("skor", "R/R", "nilai transaksi", "kode")`
  - `rank_basis(row: Mapping[str, Any]) -> dict[str, Any]` → `{"order": RANK_BASIS, "values": [...]}` untuk ditampilkan
  - `confidence_label(score: float, horizon: int) -> str` → `"meyakinkan"` hanya bila `score != NEUTRAL_SCORE` **dan** `horizon in validated_horizons()`; selain itu `"belum terbukti"`
  - tiap kandidat membawa `percentile`, `rank_basis`, `confidence`

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_persentil_kolam_memberi_100_untuk_skor_tertinggi():
    assert pool_percentile([50.0, 50.0, 52.0]) == [50.0, 50.0, 100.0]


def test_persentil_seri_dapat_nilai_sama():
    # titik-massa di NEUTRAL_SCORE: seri tidak boleh mengklaim "tertinggi"
    assert pool_percentile([50.0, 50.0]) == [50.0, 50.0]


def test_urutan_pemutus_ditulis_dan_ditampilkan():
    r = rank_basis({"score": 52.0, "rr": 2.0, "metrics": {"value": 1.2e9}, "code": "BBRI"})
    assert r["order"] == ["skor", "R/R", "nilai transaksi", "kode"]
    assert r["values"][0] == 52.0


def test_persentil_riwayat_sendiri_memakai_skor_dasar():
    """Emiten dengan riwayat panjang: skor terakhir dibandingkan riwayatnya sendiri."""
    m = _own_history_frame(n=60)  # satu kode, 60 sesi, nilai beragam
    p = own_history_percentile(m, "BBRI", 21)
    assert p is not None and 0.0 <= p <= 100.0


def test_persentil_riwayat_sendiri_none_kalau_riwayat_pendek():
    assert own_history_percentile(_own_history_frame(n=5), "BBRI", 21) is None


def test_label_meyakinkan_hanya_untuk_horizon_tervalidasi():
    assert confidence_label(52.0, 21) == "meyakinkan"       # h21 punya komponen berbobot
    assert confidence_label(50.0, 21) == "belum terbukti"   # tidak bergerak dari netral


def test_label_meyakinkan_tidak_keluar_untuk_horizon_tanpa_komponen():
    assert confidence_label(52.0, 7) == "belum terbukti"


def test_kandidat_membawa_persentil_dan_basis_urutan():
    out = candidates_for_date(metrics_row(pool_size=3), pd.Timestamp("2026-05-01"))
    assert out and all("percentile" in c and "rank_basis" in c for c in out)
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_recommend.py tests/test_recommendation_log.py -v`
Expected: FAIL — `ImportError: cannot import name 'pool_percentile'`

- [ ] **Step 3: Implementasi**

`pool_percentile`: peringkat persentil berbasis peringkat rata-rata untuk seri — `(rank - 1) / (n - 1) * 100` dengan peringkat rata-rata; `n == 1` → `[100.0]`. Tulis dua baris ini sebagai komentar tepat di atas rumusnya, karena ini yang menggantikan "skor 0–100 = keyakinan" (spec D3).

`assign_grades`: setelah tier ditetapkan, isi `percentile` per kandidat dari skor se-kolam, dan `rank_basis` dari `rank_basis(row)`; jangan mengubah urutan (`candidate_rank_key` tetap satu-satunya penentu urutan).

`confidence_label`: dua aturan di Interfaces, apa adanya — tidak ada ambang angka baru.

`own_history_percentile`: ambil semua baris emiten itu dari `metrics` (urut tanggal menaik), hitung skor **dasar** per baris lewat `score_components(candidate_features(baris), horizon)`, lalu kembalikan nilai terakhir dari `pool_percentile(scores)`; `None` bila jumlah baris < 20. Docstring menyebut batasnya apa adanya: lapisan faktor/broker tidak ikut, karena ia hanya ada untuk sesi berjalan (bukan riwayat).

- [ ] **Step 4: Jahit ke jawaban API**

`candidates_for_date` meneruskan `percentile`, `rank_basis`, `confidence` (dari `confidence_label(score, horizon sebenarnya)`) ke tiap dict kandidat, sehingga `/api/recommendations` dan `/api/stocks/{code}/recommendation` membawanya. **Jangan** hapus `score` dari payload — track record masih memakainya; yang berubah adalah apa yang ditampilkan web (Task 7).

`own_percentile` diisi di `analytics.get_recommendations` **sesudah** `rank_candidates` dan **sebelum** hasilnya di-cache — cukup untuk baris yang benar-benar dikembalikan (≤ 100 kode), jadi biayanya kecil. Emiten tanpa riwayat cukup mengirim `None`; jangan menampilkan angka karangan sebagai gantinya.

- [ ] **Step 5: Jalankan tes + lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_recommend.py tests/test_recommendation_log.py -v` → PASS
Run: `.venv\Scripts\python.exe -m ruff check src tests scripts` → bersih

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/research/recommend.py src/idx_scraper/research/recommendation_log.py tests/test_recommend.py tests/test_recommendation_log.py
git commit -m "feat(recommend): peringkat persentil, urutan pemutus terbuka, label keyakinan bergate (spec D3)"
```

---

## Task 7: Permukaan — badge kesegaran + penanda berindeks

**Files:**
- Modify: `idx-web/lib/types.ts` (`MarketRegime` di `:52`, tambah `Freshness` dekat `DataHealth:1409`), `idx-web/lib/hooks.ts` (tambah `useFreshness` dekat `useDataHealth:475`), `idx-web/app/page.tsx:223-225`, `idx-web/app/rekomendasi/page.tsx:331`, `idx-web/components/RegimeBanner.tsx:84-86`, `idx-web/components/MarketRegimeCard.tsx:103`
- Create: `idx-web/components/FreshnessBadge.tsx`

**Interfaces:**
- Consumes: `GET /api/freshness` (Task 2); `withheld`/`freshness` pada `/api/recommendations` (Task 3); `gate` pada `/api/market/regime` (Task 4); `percentile`/`confidence` (Task 6)
- Produces: `<FreshnessBadge freshness={...} basis="panel" />`

- [ ] **Step 1: Baca aturan Next.js versi repo ini (wajib, sebelum menulis kode)**

`idx-web/AGENTS.md` dan `idx-web/node_modules/next/AGENTS.md` menyatakan versi ini **bukan** Next.js yang lazim dan memerintahkan membaca `node_modules/next/dist/docs/` dulu. Baca yang relevan dengan pekerjaan ini: `01-app/01-getting-started/` (server/client components) dan `01-app/03-api-reference/` untuk `next/link`/metadata seperlunya. Jangan menghapus blok di `AGENTS.md` — berkas itu ditulis ulang `next dev`.

- [ ] **Step 2: Tulis tipe + hook**

Di `lib/types.ts`: `export interface Freshness { status: "segar" | "basi" | "tak diketahui"; withhold: boolean; reason: string; panel_latest: string | null; index_latest: string | null; index_status: string; expected: string | null; checked_at: string }`; tambahkan `freshness?: Freshness; withheld?: boolean; pool_size?: number` pada tipe respons rekomendasi, field opsional `percentile`, `own_percentile`, `confidence`, dan `rank_basis` (`{ order: string[]; values: number[] }`) pada tipe baris kandidat yang sudah dipakai halaman `/rekomendasi`, dan `gate?: { blocked: boolean; reason: string; regime_fresh: boolean }` pada `MarketRegime` (`:52`).

Di `lib/hooks.ts`, tiru `useDataHealth` (`:475-480`) apa adanya:

```ts
/** Kesegaran data: "boleh percaya angka di dashboard?" — spec D1. */
export function useFreshness() {
  return useSWR<Freshness>("/api/freshness", fetcher, {
    refreshInterval: 60_000,
    shouldRetryOnError: false,
  });
}
```

- [ ] **Step 3: Buat `components/FreshnessBadge.tsx`**

Komponen client kecil: tiga status → kelas badge yang **sudah ada** di `app/globals.css:190-204` (`badge-buy` untuk segar, `badge-warn` untuk basi, `badge-hold` untuk tak diketahui), teks Indonesia konsisten dengan yang sudah dipakai repo ("Segar", "Basi", "Tak diketahui" — pola kata "Lebih lama dari sesi terakhir" ada di `app/dossier/[code]/page.tsx:116-120`). Properti `basis: "panel" | "indeks"` mengubah label menjadi mis. `"Segar (panel saham)"` / `"Basi (indeks)"`. Ikon (kalau dipakai) selalu `aria-hidden`, teks status `role="status"` — sama seperti `DataHealthCard.tsx:163`. `title`/teks penuh memuat `freshness.reason` supaya sebabnya terbaca, bukan hanya statusnya.

- [ ] **Step 4: Pasang**

1. `app/page.tsx:223-225` — sisipkan `<FreshnessBadge />` di paragraf status header (slot "DataIDX delayed …"). Ini permukaan pertama yang dibaca mata tiap hari.
2. `app/rekomendasi/page.tsx:331` — `<FreshnessBadge />` tepat sesudah `LastUpdated`. Bila `data?.withheld`, tampilkan juga baris alasan (`data.freshness.reason`) dan **sembunyikan** badge grade A/B/C + kolom ukuran posisi pada kartu kandidat, karena backend sudah menahannya (`grade: null`, `position_pct: null`) — jangan render "null", render satu baris penjelas.
3. **Skala yang jujur (spec D3, wajib ada di P4):** pada kartu kandidat, yang ditampilkan sebagai ukuran posisi di antara kandidat adalah **peringkat persentil kolam** (`percentile`) dan persentil riwayatnya sendiri (`own_percentile`, kalau ada) — **bukan** angka skor 0–100 (spec D3 poin 1: skor mentah tidak boleh dibaca sebagai kekuatan keyakinan). Tampilkan label `confidence` apa adanya (`"meyakinkan"` / `"belum terbukti"`), dan cantumkan urutan pemutus dari `rank_basis.order` sebagai satu baris kecil/tooltip pada daftar (mis. "urutan saat seri: skor → R/R → nilai transaksi → kode") — spec D3 poin 2 menuntut urutan itu **ditulis dan ditampilkan**, bukan cuma dipakai diam-diam.
4. Tambahkan `pool_size` ke kartu ringkasan `/rekomendasi` (dekat `Stat label="Kandidat hari ini"` di `:341`): "kolam 137 nama → 20 tampil" — supaya jumlah yang keluar dari gerbang terbaca, bukan hanya yang tampil (spec §6.6).
5. `components/RegimeBanner.tsx:84-86` dan `components/MarketRegimeCard.tsx:103` — tambahkan `<FreshnessBadge basis="indeks" />` di slot `ml-auto` yang sudah ada (penanda terpisah, spec D1), dan bila `gate.blocked` tampilkan `gate.reason` sebagai alasan layar diam.

- [ ] **Step 5: Verifikasi tanpa merusak dev server**

Dev server berjalan sebagai `next dev` (task `IDX-Web`, `scripts/start-web.bat:12`) sehingga berkas baru termuat sendiri — **jangan** menjalankan `npm run build` (ia menulis `.next` yang sedang dipakai dev server). Verifikasi dengan:

Run: `npx tsc --noEmit` (di `idx-web`) → exit 0
Lalu buka `http://localhost:3000/` dan `http://localhost:3000/rekomendasi` di browser: badge muncul dengan teks Indonesia; kartu kandidat menampilkan **persentil** (bukan skor 0–100) plus satu baris urutan pemutus; dan saat `curl http://localhost:8000/api/freshness` mengembalikan `basi`, badge di `/rekomendasi` berbunyi basi sementara kartu kandidat menyembunyikan grade — bukan menampilkan `null`.

- [ ] **Step 6: Perbarui peta menu yang basi**

`idx-web/docs/README.md` sudah basi (spec R5: perbaiki saat menyentuh dokumentasi). Perbarui baris yang menyangkut `/` dan `/rekomendasi` supaya menyebut badge kesegaran. Ini pekerjaan satu baris, bukan penulisan ulang dokumen.

- [ ] **Step 7: Commit**

```bash
cd D:\Project\market-labs\idx-web
git add lib/types.ts lib/hooks.ts components/FreshnessBadge.tsx components/RegimeBanner.tsx components/MarketRegimeCard.tsx app/page.tsx app/rekomendasi/page.tsx docs/README.md
git commit -m "feat(web): badge kesegaran segar/basi/tak diketahui + penanda basis indeks"
```

---

## Catatan pelaksana (penutup)

- **Urutan ketergantungan:** Task 1 → 2 → 3 (kesegaran), Task 4 (rezim) bebas, Task 5 → 6 (kolam & skala), Task 7 terakhir. Jangan mulai Task 7 sebelum Task 2 dan 3 ada, karena badge-nya membaca `freshness`/`withheld` yang baru lahir di sana.
- **Tiga perubahan perilaku** yang wajib disebut saat merge: (a) respons `/api/recommendations` & `/api/stocks/{code}/recommendation` memuat `freshness`/`withheld` dan **grade bisa hilang**; (b) `score_candidate` tidak lagi menyaring likuiditas (pindah ke kolam `research/pool.py`); (c) `/api/market/regime` memuat `gate`. Ketiganya mengubah angka/permukaan yang sudah dilihat pemilik → API `:8000` **wajib** di-restart lewat jalur terjaga sesudah merge (buat berkas `_restart-api.flag`, lalu `Start-ScheduledTask -TaskName IDX-API` — jalur yang sama dipakai saat P3 di-merge). Web **tidak** perlu restart (dev server).
- **Bukti wajib sebelum merge:** seluruh suite lulus (767 tes P3 + tes baru) dan `ruff check src tests scripts` bersih. Yang **tidak** dibuktikan tes otomatis: rute FastAPI (repo tidak punya `TestClient`) dan komponen web (repo tidak punya tes frontend) — keduanya diverifikasi manual seperti langkah Task 7, dan hasilnya ditulis di pesan merge apa adanya.
- **Yang sengaja tidak dikerjakan di P4:** daftar pendek 3–5 nama (D2) — ia butuh ukuran posisi berplafon volume (P5) dan baris rekaman jurnal (P6), jadi membangunnya sekarang berarti membangun dua kali. Plafon ukuran posisi dari ketebalan volume (D4 → `position_size_pct`) juga P5.
- Sesudah seluruh tugas lulus: perbarui baris status P4 di `D:\Project\market-labs\RENCANA-P0-P7.md`, lalu minta review seluruh branch sebelum merge ke `main` (pola yang sama dengan P3).
