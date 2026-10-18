# Rencana Implementasi — P5: Lapis 3 (Rencana Posisi)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tiap tawaran di papan dan di halaman emiten membawa ukuran posisi yang bisa dieksekusi (persen modal, rupiah, jumlah lot, rugi maksimum kalau stop kena), dibatasi empat plafon bernama (per nama, likuiditas, rezim, portofolio), dengan modal dan risiko sebagai input pemilik yang disimpan di browser saja.

**Architecture:** Satu modul murni baru (`research/position.py`) menghitung rencana dari (entry, stop, modal, risiko, ketebalan nilai transaksi, rezim, keadaan portofolio yang diketik). `api/analytics.py` menempelkannya SESUDAH cache papan dan SESUDAH penahanan P4, jadi cache tidak pecah per angka modal dan baris yang ditahan tidak pernah membawa ukuran posisi. Web mengirim empat angka keadaan sebagai query dan menyimpannya di localStorage, tanpa tabel atau endpoint baru.

**Tech Stack:** Python 3 (pandas, FastAPI, pydantic v2, pytest, ruff) + Next.js 16 App Router, TypeScript, SWR, Tailwind v4.

**Spec:** `docs/superpowers/specs/2026-10-18-spec-lapis-3-rencana-posisi-design.md` (revisi 3, disetujui 18 Okt 2026).

## Global Constraints

- **Angka aturan (spec bagian 4 dan 5.1):** risiko default 1% modal; plafon per nama 25% (`MAX_POSITION_PCT`, tidak diubah); plafon likuiditas 2% dari **median nilai transaksi 5 sesi terakhir**; `vol_state == "VOLATILE"` memotong separuh; batas portofolio 6 posisi terbuka, eksposur total 80% modal, risiko serentak 6% modal (risiko serentak **diturunkan**, bukan plafon terpisah).
- **1 lot = 100 lembar, dibulatkan ke bawah.** Angka yang ditampilkan adalah nilai lot yang benar-benar bisa dibeli (`lots x 100 x entry_ref`), bukan nilai teoretis.
- **Modal tidak pernah disimpan di server.** Tidak ada tabel, kolom, atau endpoint baru untuk modal. Kunci browser: `ml-modal`, `ml-risk-pct`, `ml-open-positions`, `ml-exposure-pct` (pola `ml-theme`).
- **Rencana dihitung SESUDAH cache dan SESUDAH penahanan** (spec bagian 5.3). Tidak ada perubahan pada `_research_cache_put`/`_research_cache_get`, tidak ada kunci cache baru.
- **Tidak menyentuh perilaku P1 sampai P4.** Baseline data nyata sesi 2026-10-16 harus tetap: kolam 191, kandidat 46, karantina 16, PEGE skor 56.7 / entry 113,42 / stop 106,33 / target 127,60 / `own_percentile` 82.4.
- **Aturan lama tidak diubah:** `entry_plan`, `position_size_pct`, `MAX_POSITION_PCT`, `TARGET_RR`, `STOP_VOL_MULT` tetap apa adanya. Field `position_pct` tetap ada di payload (dipakai penahanan P4) meski web berhenti menampilkannya (spec D16).
- **Bahasa:** teks yang dilihat pengguna dan komentar kode dalam bahasa Indonesia, mengikuti gaya repo. Tidak memakai tanda pisah panjang (em dash).
- **Verifikasi:** `pytest` penuh + `ruff check src tests scripts` bersih di idx-scraper; `npx tsc --noEmit` + render nyata di idx-web (repo web tidak punya test runner, jadi render dan angka API yang jadi bukti).

## Review Focus

Lima hal yang spec minta tapi tidak diuji oleh tes mana pun kecuali ditambahkan sengaja. Masing-masing sudah punya tes di task pemiliknya.

1. **Modal kosong atau tidak valid** (`None`, `0`, negatif): rencana tetap ada dalam persen, `rupiah`/`lots`/`risk_rupiah` kosong, tanpa angka nol karangan. Tes: Task 1 `test_tanpa_modal_hanya_persen`, `test_modal_tidak_valid_dianggap_tanpa_modal`.
2. **Modal kecil** sampai porsi di bawah harga 1 lot: `lots = 0`, `rupiah = 0`, dan `note` berkata modal kurang untuk 1 lot, bukan 0 tanpa sebab. Tes: Task 1 `test_modal_kurang_untuk_satu_lot`.
3. **Keadaan portofolio belum diisi** (`open_positions` atau `exposure_pct` `None`): plafon portofolio dilewati dan `note` mengatakannya, bukan diasumsikan 0 (optimistis) atau penuh (mematikan). Tes: Task 1 `test_keadaan_portofolio_kosong_dilewati`.
4. **Nama tanpa riwayat nilai transaksi** (`value` `None` atau frame kosong): plafon likuiditas dilewati tanpa crash, dan baris yang ditahan tidak pernah dapat `plan`. Tes: Task 1 `test_typical_day_value_none_bila_tidak_ada`, Task 2 `test_riwayat_kosong_tidak_memotong`, `test_baris_ditahan_tidak_dapat_plan`.
5. **Papan dan halaman emiten sepakat** (spec D15): untuk keadaan yang sama, satu nama menghasilkan `plan` yang identik dari kedua jalur, dan baris yang ditahan tetap kosong meski modal diisi (D11). Tes: Task 2 `test_papan_dan_emiten_sepakat`, `test_kandidat_emiten_ditahan_tidak_dapat_plan`; bukti HTTP di Task 5.

---

## Task 1: Modul murni rencana posisi

**Files:**
- Create: `src/idx_scraper/research/position.py`
- Test: `tests/test_position.py`

**Interfaces:**
- Consumes: `recommend.position_size_pct`, `recommend.DEFAULT_RISK_PCT`, `recommend.MAX_POSITION_PCT` (tidak diubah).
- Produces (dipakai Task 2):
  - `ONE_LOT: int = 100`
  - `@dataclass(frozen=True) class PositionLimits` dengan field `max_position_pct: float = 25.0`, `adv_pct: float = 0.02`, `max_open_positions: int = 6`, `max_exposure_pct: float = 80.0`, `max_concurrent_risk_pct: float = 6.0`, `volatile_cut: float = 0.5`, `typical_sessions: int = 5`
  - `DEFAULT_LIMITS: PositionLimits = PositionLimits()`
  - `typical_day_value(metrics: pd.DataFrame, code: str, sessions: int = 5) -> float | None`
  - `position_plan(*, entry: float | None, stop: float | None, modal: float | None = None, risk_pct: float | None = None, typical_day_value: float | None = None, vol_state: str | None = None, open_positions: int | None = None, exposure_pct: float | None = None, limits: PositionLimits = DEFAULT_LIMITS) -> dict[str, Any]` yang mengembalikan kunci `pct`, `pct_raw`, `binding_cap`, `caps` (`per_name`, `liquidity`, `regime`, `portfolio`), `concurrent_risk_pct`, `rupiah`, `lots`, `risk_rupiah`, `note`.

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_position.py` (gaya mengikuti `tests/test_recommend.py`: modul murni, tanpa DB, helper kecil di atas):

```python
"""Tests rencana posisi lapis 3 (research/position.py): persen, lot, plafon."""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.research.position import (
    DEFAULT_LIMITS,
    position_plan,
    typical_day_value,
)

PEGE_ENTRY = 113.42
PEGE_STOP = 106.33


def pege(**kw):
    base = dict(entry=PEGE_ENTRY, stop=PEGE_STOP, modal=100_000_000.0, risk_pct=1.0)
    base.update(kw)
    return position_plan(**base)


def test_pct_raw_memakai_aturan_lama():
    p = pege()
    assert p["pct_raw"] == 16.0
    assert p["pct"] == 16.0
    assert p["binding_cap"] is None
    assert p["note"] == "Tidak ada plafon yang memotong."


def test_tanpa_modal_hanya_persen():
    p = pege(modal=None)
    assert p["pct"] == 16.0
    assert p["rupiah"] is None
    assert p["lots"] is None
    assert p["risk_rupiah"] is None
    assert "tanpa modal" in p["note"].lower()


def test_modal_penuh_rupiah_lot_rugi():
    p = pege()
    assert p["lots"] == 1410
    assert p["rupiah"] == pytest.approx(15_992_220.0, rel=1e-9)
    assert p["risk_rupiah"] == pytest.approx(999_690.0, rel=1e-9)


def test_modal_tidak_valid_dianggap_tanpa_modal():
    for modal in (0, -5.0):
        p = pege(modal=modal)
        assert p["lots"] is None
        assert p["rupiah"] is None


def test_risk_pct_tidak_valid_pakai_default():
    assert pege(risk_pct=0)["pct_raw"] == 16.0
    assert pege(risk_pct=None)["pct_raw"] == 16.0


def test_stop_tidak_valid_tidak_ada_rencana():
    p = pege(stop=PEGE_ENTRY)
    assert p["pct"] is None
    assert p["pct_raw"] is None
    assert p["lots"] is None


def test_plafon_per_nama_memotong():
    p = pege(entry=100.0, stop=90.0, risk_pct=10.0)
    assert p["pct_raw"] == 100.0
    assert p["caps"]["per_name"] == 25.0
    assert p["pct"] == 25.0
    assert p["binding_cap"] == "per_name"


def test_plafon_likuiditas_memotong_dan_disebut():
    p = pege(typical_day_value=500_000_000.0)  # 2% x 500jt / 100jt = 10% modal
    assert p["caps"]["liquidity"] == 10.0
    assert p["pct"] == 10.0
    assert p["binding_cap"] == "liquidity"
    assert "likuiditas" in p["note"].lower()


def test_plafon_likuiditas_dilewati_tanpa_modal():
    p = pege(modal=None, typical_day_value=500_000_000.0)
    assert p["caps"]["liquidity"] is None
    assert p["pct"] == 16.0


def test_plafon_rezim_memotong_separuh():
    p = pege(vol_state="VOLATILE")
    assert p["caps"]["regime"] == 8.0
    assert p["pct"] == 8.0
    assert p["binding_cap"] == "regime"


def test_rezim_normal_tidak_memotong():
    assert pege(vol_state="NORMAL")["caps"]["regime"] is None


def test_buku_penuh_porsi_nol():
    p = pege(open_positions=6)
    assert p["pct"] == 0.0
    assert p["lots"] == 0
    assert p["rupiah"] == 0.0
    assert p["binding_cap"] == "portfolio"
    assert "penuh" in p["note"].lower()


def test_plafon_eksposur_memotong():
    p = pege(exposure_pct=78.0)
    assert p["caps"]["portfolio"] == 2.0
    assert p["pct"] == 2.0
    assert p["binding_cap"] == "portfolio"


def test_keadaan_portofolio_kosong_dilewati():
    p = pege()
    assert p["caps"]["portfolio"] is None
    assert p["concurrent_risk_pct"] is None
    assert "portofolio" in p["note"].lower()


def test_risiko_serentak_diturunkan():
    p = pege(open_positions=3)
    assert p["concurrent_risk_pct"] == 3.0
    assert p["caps"]["portfolio"] is None


def test_modal_kurang_untuk_satu_lot():
    p = pege(modal=50_000.0)  # 16% x 50rb = 8rb, di bawah 1 lot (Rp 11.342)
    assert p["lots"] == 0
    assert "1 lot" in p["note"]


def test_typical_day_value_median_lima_sesi():
    rows = [
        {"code": "AAAA", "date": pd.Timestamp(f"2026-10-{i:02d}"), "value": v}
        for i, v in enumerate([1e9, 2e9, 3e9, 4e9, 5e9, 6e9, 7e9], start=1)
    ]
    rows.append({"code": "BBBB", "date": pd.Timestamp("2026-10-01"), "value": 9e9})
    frame = pd.DataFrame(rows)
    assert typical_day_value(frame, "AAAA") == 5e9  # median dari 5 sesi terakhir
    assert typical_day_value(frame, "BBBB") == 9e9  # riwayat lebih pendek tetap dipakai
    assert typical_day_value(frame, "CCCC") is None


def test_typical_day_value_none_bila_tidak_ada():
    frame = pd.DataFrame(
        [{"code": "AAAA", "date": pd.Timestamp("2026-10-01"), "value": None}]
    )
    assert typical_day_value(frame, "AAAA") is None


def test_adv_pct_sama_dengan_model_biaya_backtest():
    from idx_scraper.api.simulation import DEFAULT_COSTS

    assert DEFAULT_LIMITS.adv_pct == DEFAULT_COSTS["max_adv_pct"]
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_position.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'idx_scraper.research.position'`.

- [ ] **Step 3: Implementasi `research/position.py`**

Header modul (komentar bahasa Indonesia, menyebut spec dan kenapa modul ini terpisah: dipakai papan sekarang dan jurnal P6 nanti). Impor: `from __future__ import annotations`, `math`, `dataclasses.dataclass`, `typing.Any`, `pandas as pd`, dan `from .recommend import DEFAULT_RISK_PCT, MAX_POSITION_PCT, position_size_pct`.

Konstanta: `ONE_LOT = 100`, `ADV_PCT = 0.02` (dengan komentar: angka yang sama dengan `api.simulation.DEFAULT_COSTS["max_adv_pct"]`, sengaja disamakan dengan model biaya backtest), `VOLATILE_CUT = 0.5`, `TYPICAL_SESSIONS = 5`, lalu `PositionLimits` dan `DEFAULT_LIMITS` sesuai Interfaces.

`typical_day_value`:

```python
def typical_day_value(metrics: pd.DataFrame, code: str, sessions: int = TYPICAL_SESSIONS) -> float | None:
    if metrics is None or metrics.empty:
        return None
    col_code = metrics.get("code")
    if col_code is None:
        return None
    rows = metrics.loc[col_code.astype(str).str.upper() == str(code).upper()]
    if "value" in rows:
        rows = rows.dropna(subset=["value"])
    if rows.empty:
        return None
    if "date" in rows:
        rows = rows.sort_values("date")
    tail = rows["value"].tail(int(sessions))
    if tail.empty:
        return None
    return float(tail.median())
```

Algoritma `position_plan` (urutan ini yang diuji; jangan disederhanakan tanpa menjalankan tes):

1. `risk = float(risk_pct) if risk_pct is not None and float(risk_pct) > 0 else float(DEFAULT_RISK_PCT)`.
2. `modal_ok = modal is not None and float(modal) > 0`. `m = float(modal) if modal_ok else None`.
3. `pct_raw = position_size_pct(entry=entry, stop=stop, risk_pct=risk, max_pct=float("inf"))`. `float("inf")` sengaja: aturan persennya tetap satu sumber (`position_size_pct`), tapi plafon per nama diterapkan di sini supaya `binding_cap` bisa dilaporkan. Bila `pct_raw is None` (stop tidak valid): kembalikan seluruh kunci dengan `pct`, `pct_raw`, `rupiah`, `lots`, `risk_rupiah`, `concurrent_risk_pct` bernilai `None`, `caps` berisi `per_name` dan sisanya `None`, dan `note = "Stop tidak valid: rencana ukuran tidak bisa dihitung."`.
4. `caps`, tiap nilai dibulatkan 1 desimal kecuali `per_name`:
   - `per_name = limits.max_position_pct`
   - `liquidity = None` bila `m is None` atau `typical_day_value is None`; selain itu `round(limits.adv_pct * float(typical_day_value) / m * 100, 1)`
   - `regime = round(pct_raw * limits.volatile_cut, 1)` bila `str(vol_state or "").upper() == "VOLATILE"`, selain itu `None`
   - `portfolio`: `ruang = None` bila `exposure_pct is None`, selain itu `max(0.0, limits.max_exposure_pct - float(exposure_pct))`; bila `open_positions is not None and int(open_positions) >= limits.max_open_positions` maka `ruang = 0.0`; `portfolio = round(ruang, 1)` bila `ruang is not None` selain itu `None`.
5. `pct = round(min([pct_raw] + [v for v in caps.values() if v is not None]), 1)`.
6. `binding_cap`: nama pertama menurut urutan `("per_name", "liquidity", "regime", "portfolio")` yang nilainya **lebih kecil** dari `pct_raw`; tidak ada yang lebih kecil berarti `None`.
7. Bila `m is not None`: `rupiah_teoretis = m * pct / 100`; `lots = int(rupiah_teoretis // (ONE_LOT * float(entry)))`; `rupiah = round(lots * ONE_LOT * float(entry), 2)`; `risk_rupiah = round(lots * ONE_LOT * (float(entry) - float(stop)), 2)`. Selain itu ketiganya `None`.
8. `concurrent_risk_pct = round(risk * int(open_positions), 1)` bila `open_positions is not None` dan `modal_ok`, selain itu `None`.
9. `note`, satu kalimat utama plus satu keterangan tambahan bila perlu. Kalimat utama, berurutan sesuai kondisi: `"Dipotong plafon likuiditas (2% nilai transaksi harian)."`, `"Dipotong plafon rezim: pasar VOLATILE, porsi separuh."`, `"Dipotong plafon per nama."`, `"Dipotong plafon eksposur: ruang sisa di bawah porsi rencana."`, dan bila `binding_cap is None`: `"Tidak ada plafon yang memotong."`. Tambahan: bila `m is None` → `"Tanpa modal: porsi hanya dalam persen."`; bila `m is not None and lots == 0` → `"Modal kurang untuk 1 lot."`; keadaan portofolio yang belum lengkap disebut per bidang, supaya tidak bertabrakan dengan kalimat "sudah penuh": `open_positions is None and exposure_pct is None` → `"Posisi terbuka dan eksposur belum diisi, plafon portofolio dilewati."`, hanya `open_positions` kosong → `"Posisi terbuka belum diisi, plafon portofolio dilewati."`, hanya `exposure_pct` kosong → `"Eksposur belum diisi, plafon portofolio dilewati."`; bila `open_positions` mencapai batas → `"Sudah penuh: 6 posisi terbuka dari batas 6."` (angka nyata, bukan literal). Gabungkan dengan spasi.

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_position.py -q`
Expected: PASS (18 tes).

- [ ] **Step 5: Cek gaya**

Run: `.venv\Scripts\python.exe -m ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/research/position.py tests/test_position.py
git commit -m "feat(p5): modul murni rencana posisi (persen, lot, empat plafon)"
```

---

## Task 2: Skema, penempelan sesudah cache, dan query rute

**Files:**
- Modify: `src/idx_scraper/api/schemas.py` (tambah dua model sebelum `RecommendationRow` sekitar baris 640; tambah field `plan` di `RecommendationRow` setelah `position_pct` baris 678)
- Modify: `src/idx_scraper/api/analytics.py` (helper `_with_position_plan` dan `_close_recommendation` setelah `_gate_withhold` sekitar baris 2812; ganti 7 titik balik: baris 2832, 2859, 2891 di `get_recommendations`, baris 2906, 2932, 2938, 2981 di `get_stock_recommendation`; tambah empat parameter kata kunci di kedua signature)
- Modify: `src/idx_scraper/api/app.py` (rute `recommendations` baris 381-392 dan `stock_recommendation` baris 401-404)
- Test: `tests/test_position_plan_api.py`

**Interfaces:**
- Consumes: `position.position_plan`, `position.typical_day_value`, `PositionLimits` dari Task 1.
- Produces (dipakai Task 3 dan 4 lewat payload HTTP):
  - `PositionPlanCaps` (pydantic): `per_name: float | None`, `liquidity: float | None`, `regime: float | None`, `portfolio: float | None`
  - `PositionPlan` (pydantic): `pct`, `pct_raw`, `binding_cap: str | None`, `caps: PositionPlanCaps | None`, `concurrent_risk_pct`, `rupiah`, `lots: int | None`, `risk_rupiah`, `note: str | None`
  - `RecommendationRow.plan: PositionPlan | None`
  - `analytics._with_position_plan(out: dict[str, Any], *, modal: float | None = None, risk_pct: float | None = None, open_positions: int | None = None, exposure_pct: float | None = None, metrics: pd.DataFrame | None = None) -> dict[str, Any]`
  - `analytics._close_recommendation(out: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]` dengan `state` berisi empat kunci `modal`, `risk_pct`, `open_positions`, `exposure_pct`
  - `analytics.get_recommendations(limit=20, min_grade="C", *, modal=None, risk_pct=None, open_positions=None, exposure_pct=None)`
  - `analytics.get_stock_recommendation(code, *, modal=None, risk_pct=None, open_positions=None, exposure_pct=None)`

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_position_plan_api.py`:

```python
"""Tests penempelan rencana posisi ke payload API (sesudah cache, sesudah penahanan)."""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.api.analytics import _with_position_plan


def pege_row(**kw):
    row = {
        "code": "PEGE",
        "score": 56.7,
        "entry_ref": 113.42,
        "stop": 106.33,
        "target": 127.60,
        "position_pct": 16.0,
    }
    row.update(kw)
    return row


def frame(values=(1.0e9,)):
    return pd.DataFrame(
        [
            {"code": "PEGE", "date": pd.Timestamp(f"2026-10-{i:02d}"), "value": v}
            for i, v in enumerate(values, start=1)
        ]
    )


EMPTY_FRAME = pd.DataFrame(columns=["code", "date", "value"])


def test_baris_dapat_plan():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(
        out, modal=100_000_000.0, risk_pct=1.0, open_positions=0, exposure_pct=0.0,
        metrics=frame(),
    )
    plan = res["rows"][0]["plan"]
    assert plan["pct"] == 16.0
    assert plan["lots"] == 1410
    assert plan["rupiah"] == pytest.approx(15_992_220.0, rel=1e-9)
    assert plan["note"] == "Tidak ada plafon yang memotong."


def test_dict_masuk_tidak_termutasi():
    out = {"rows": [pege_row()], "gate": None}
    _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert "plan" not in out["rows"][0]


def test_baris_ditahan_tidak_dapat_plan():
    out = {"rows": [pege_row(grade=None, withheld=True)], "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["rows"][0]["plan"] is None


def test_modal_kosong_persen_saja():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(out, metrics=frame())
    assert res["rows"][0]["plan"]["pct"] == 16.0
    assert res["rows"][0]["plan"]["lots"] is None


def test_gerbang_volatile_memotong_separuh():
    out = {"rows": [pege_row()], "gate": {"blocked": False, "vol_state": "VOLATILE"}}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["rows"][0]["plan"]["pct"] == 8.0


def test_riwayat_kosong_tidak_memotong():
    out = {"rows": [pege_row()], "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=EMPTY_FRAME)
    assert res["rows"][0]["plan"]["caps"]["liquidity"] is None
    assert res["rows"][0]["plan"]["pct"] == 16.0


def test_kandidat_emiten_dapat_plan():
    out = {"candidate": pege_row(), "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["candidate"]["plan"]["lots"] == 1410


def test_kandidat_emiten_ditahan_tidak_dapat_plan():
    out = {"candidate": pege_row(withheld=True), "gate": None}
    res = _with_position_plan(out, modal=100_000_000.0, metrics=frame())
    assert res["candidate"]["plan"] is None


def test_papan_dan_emiten_sepakat():
    state = dict(modal=100_000_000.0, risk_pct=1.0, open_positions=2, exposure_pct=40.0)
    panel = _with_position_plan({"rows": [pege_row()], "gate": None}, metrics=frame(), **state)
    stock = _with_position_plan({"candidate": pege_row(), "gate": None}, metrics=frame(), **state)
    assert panel["rows"][0]["plan"] == stock["candidate"]["plan"]
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_position_plan_api.py -q`
Expected: FAIL, `ImportError: cannot import name '_with_position_plan'`.

- [ ] **Step 3: Implementasi `_with_position_plan` di `api/analytics.py`**

Impor di dalam fungsi (pola repo, menghindari impor lingkaran): `from ..research.position import position_plan, typical_day_value`.

```python
def _with_position_plan(out, *, modal=None, risk_pct=None, open_positions=None,
                        exposure_pct=None, metrics=None) -> dict[str, Any]:
    """Tempelkan rencana posisi ke baris yang benar-benar tampil (spec P5 5.3).

    Dipanggil SESUDAH cache dan SESUDAH penahanan: baris ber-`withheld` tidak
    pernah membawa ukuran posisi, dan cache papan tidak dipecah per angka modal.
    Dict yang masuk tidak pernah dimutasi.
    """
```

Badan: `frame = metrics if metrics is not None else _recommendation_context()["metrics"]`; `vol_state = (out.get("gate") or {}).get("vol_state")`; satu fungsi lokal `plan_of(row)` yang mengembalikan `None` bila `row.get("withheld")` benar, selain itu `position_plan(entry=row.get("entry_ref"), stop=row.get("stop"), modal=modal, risk_pct=risk_pct, typical_day_value=typical_day_value(frame, row.get("code")), vol_state=vol_state, open_positions=open_positions, exposure_pct=exposure_pct)`. Kembalikan salinan: `{**out, "rows": [{**r, "plan": plan_of(r)} for r in out.get("rows") or []]}` bila `"rows" in out`, dan `"candidate"` secara terpisah bila ada (`candidate` bukan dict berarti tetap `None`).

- [ ] **Step 4: Implementasi `_close_recommendation` dan ganti 7 titik balik**

```python
def _close_recommendation(out: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Urutan tetap: penahanan D1 -> gerbang rezim -> rencana posisi (spec P5 5.3)."""
    withheld = _withhold_panel(out) if "rows" in out else _withhold_stock(out)
    return _with_position_plan(_gate_withhold(withheld, _regime_gate()), **state)
```

Di `get_recommendations`, susun `state = {"modal": modal, "risk_pct": risk_pct, "open_positions": open_positions, "exposure_pct": exposure_pct}` sekali di awal badan, lalu ganti ketiga titik balik (baris 2832, 2859, 2891) menjadi `return _close_recommendation(cached, state)` dan `return _close_recommendation(out, state)`. Sama untuk `get_stock_recommendation` (baris 2906, 2932, 2938, 2981) dengan `_close_recommendation(cached, state)` dan `_close_recommendation(out, state)`. Tambahkan empat parameter kata kunci di kedua signature dan teruskan dari rute.

- [ ] **Step 5: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_position_plan_api.py tests/test_freshness.py tests/test_regime.py -q`
Expected: PASS (tes P4 tetap lulus: penahanan tidak berubah).

- [ ] **Step 6: Tambah model skema**

Di `api/schemas.py`, sebelum `RecommendationRow`: `class PositionPlanCaps(BaseModel)` dengan empat field `float | None = None`, lalu `class PositionPlan(BaseModel)` dengan field sesuai Interfaces (`pct: float | None = None`, `pct_raw: float | None = None`, `binding_cap: str | None = None`, `caps: PositionPlanCaps | None = None`, `concurrent_risk_pct: float | None = None`, `rupiah: float | None = None`, `lots: int | None = None`, `risk_rupiah: float | None = None`, `note: str | None = None`). Di `RecommendationRow`, setelah `position_pct`, tambah `plan: PositionPlan | None = None` dengan komentar singkat: "Rencana posisi (lapis 3). None = tidak ada modal, atau baris ini ditahan."

- [ ] **Step 7: Tambah parameter query di rute**

Di `api/app.py` rute `recommendations` (baris 381-392) dan `stock_recommendation` (401-404), tambah empat `Query` dengan deskripsi bahasa Indonesia:

```python
    modal: float | None = Query(default=None, gt=0, description="Modal rupiah; kosong = mode persen saja"),
    risk_pct: float | None = Query(default=None, gt=0, le=100, description="Risiko per transaksi (% modal)"),
    open_positions: int | None = Query(default=None, ge=0, description="Jumlah posisi terbuka (angka yang kamu isi)"),
    exposure_pct: float | None = Query(default=None, ge=0, le=100, description="Eksposur sekarang (% modal)"),
```

dan teruskan ke `analytics.get_recommendations(...)` / `analytics.get_stock_recommendation(...)`.

- [ ] **Step 8: Verifikasi bentuk rute (tanpa HTTP)**

Run:
```
.venv\Scripts\python.exe -c "from idx_scraper.api.app import app; spec=app.openapi(); print(len(app.routes)); print([p['name'] for p in spec['paths']['/api/recommendations']['get']['parameters']]); print([p['name'] for p in spec['paths']['/api/stocks/{code}/recommendation']['get']['parameters']])"
```
Expected: `57`, lalu `['exposure_pct', 'limit', 'min_grade', 'modal', 'open_positions', 'risk_pct']` dan `['code', 'exposure_pct', 'modal', 'open_positions', 'risk_pct']`.

- [ ] **Step 9: Suite penuh dan gaya**

Run: `.venv\Scripts\python.exe -m pytest -q` lalu `.venv\Scripts\python.exe -m ruff check src tests scripts`
Expected: semua lulus (825 tes lama + 27 tes baru), ruff bersih.

- [ ] **Step 10: Commit**

```bash
git add src/idx_scraper/api/schemas.py src/idx_scraper/api/analytics.py src/idx_scraper/api/app.py tests/test_position_plan_api.py
git commit -m "feat(p5): payload rencana posisi + query modal/risiko (sesudah cache)"
```

---

## Task 3: Web, strip portofolio (C) dan baris porsi kartu (A)

**Files:**
- Modify: `lib/types.ts` (tambah `PositionPlanCaps` dan `PositionPlan` di dekat `RecommendationRow`; tambah `plan?: PositionPlan | null` di `RecommendationRow`)
- Create: `lib/portfolio.ts`
- Create: `components/PortfolioStrip.tsx`
- Modify: `lib/hooks.ts` (`useRecommendations` baris 442-448)
- Modify: `app/rekomendasi/page.tsx` (hook di baris 374-378, mount strip setelah `<RegimeBanner />` baris 437, baris "Saran porsi" di baris 175-180)

**Interfaces:**
- Consumes: payload `/api/recommendations` dengan `plan` dari Task 2.
- Produces (dipakai Task 4): `lib/portfolio.ts` mengekspor `PORTFOLIO_KEYS`, `MAX_OPEN_POSITIONS`, `MAX_EXPOSURE_PCT`, `PortfolioState`, `EMPTY_PORTFOLIO`, `portfolioQuery(state): string`, `usePortfolio(): { state, setField }`; `lib/hooks.ts` mengekspor `useRecommendations(limit = 20, minGrade = "C", query = "")`.

Catatan wajib: repo web tidak punya test runner (fakta, `package.json` hanya `dev`/`build`/`start`), jadi bukti task ini adalah `tsc` bersih + render nyata + angka dari API yang sama.

- [ ] **Step 1: Tambah tipe**

`lib/types.ts`: `PositionPlanCaps` (empat field `number | null`) dan `PositionPlan` (`pct`, `pct_raw`, `binding_cap: string | null`, `caps: PositionPlanCaps | null`, `concurrent_risk_pct`, `rupiah`, `lots: number | null`, `risk_rupiah`, `note: string | null`), lalu `plan?: PositionPlan | null;` di `RecommendationRow` setelah `position_pct`.

- [ ] **Step 2: Tulis `lib/portfolio.ts`**

Empat kunci (`ml-modal`, `ml-risk-pct`, `ml-open-positions`, `ml-exposure-pct`), `MAX_OPEN_POSITIONS = 6`, `MAX_EXPOSURE_PCT = 80` (komentar: sumber kebenaran angkanya di backend `research/position.py`, ini hanya untuk tampilan strip), `PortfolioState`, `EMPTY_PORTFOLIO`, `portfolioQuery`, dan `usePortfolio`.

`portfolioQuery(state)`: keluarkan hanya nilai yang valid (`modal > 0`, `riskPct > 0`, `openPositions >= 0`, `exposurePct >= 0`), diakhiri `` (kosong) bila tidak ada. Contoh: `&modal=100000000&risk_pct=1&open_positions=3&exposure_pct=34`.

`usePortfolio`: `useState(EMPTY_PORTFOLIO)` lalu baca localStorage di `useEffect` (pola `components/ThemeToggle.tsx`: tidak membaca storage saat render, karena komponen client tetap dipra-render di server), `setField(field, value)` menulis state dan storage di dalam `try/catch`.

- [ ] **Step 3: Tulis `components/PortfolioStrip.tsx`**

Client component. Empat input angka (`Modal (Rp)`, `Risiko per transaksi (%)`, `Posisi terbuka`, `Eksposur sekarang (%)`) memakai kelas global `.input`, lalu baris angka: `Modal`, `Risiko per transaksi`, `Posisi terbuka 3 dari 6`, `Eksposur 34%`, `Ruang sisa 46%` (yakni `MAX_EXPOSURE_PCT - eksposur`, bukan `100 - eksposur`), dan `Risiko serentak 3%` dengan keterangan "turunan dari posisi terbuka x risiko per transaksi, bukan catatan".

Teks wajib:
- penanda: `Angka ini kamu isi sendiri, bukan catatan posisi nyata.`
- saat modal kosong: `Isi modal untuk melihat rupiah, lot, dan rugi maksimum. Tanpa modal, papan menampilkan persen saja.`
- catatan penyimpanan: `Tersimpan di browser ini saja.`
- saat isian tidak valid (mis. `0` pada modal atau risiko): `Isi angka lebih dari 0.` (jangan diam-diam mengirim angka itu ke API, karena API menolaknya dengan 422).
- batas kejujuran risiko (spec R5): `Ini risiko rencana, bukan jaminan kerugian maksimum harian: harga bisa melewati stop.`

- [ ] **Step 4: Sambungkan query ke hook**

`lib/hooks.ts`: `useRecommendations(limit = 20, minGrade = "C", query = "")` dengan key `` `/api/recommendations?limit=${limit}&min_grade=${minGrade}${query}` ``.

- [ ] **Step 5: Pasang di `app/rekomendasi/page.tsx`**

`const { state, setField } = usePortfolio();` dan `const query = portfolioQuery(state);` di badan komponen; kedua panggilan `useRecommendations` (baris 374-378) menerima `query`; render `<PortfolioStrip state={state} setField={setField} />` tepat sesudah `<RegimeBanner />` (baris 437). Diskusikan: kartu di bawah cukup menampilkan porsi (spec D2/C).

- [ ] **Step 6: Ganti baris "Saran porsi" dengan baris rencana (permukaan A)**

Di `CandidateCard`, buang `Stat label="Saran porsi"` (baris 175-180) dan ganti dengan blok rencana dari `row.plan`, hanya bila `!withheld && row.plan`:
- dengan modal: `Porsi 16,0% · Rp 16.000.000 · 1.410 lot` dan baris kedua `stop kena = rugi Rp 999.690 (1% modal)`
- tanpa modal: `Porsi 16,0% modal`
- bila ada plafon yang memotong: sisipkan nama plafon, mis. `Porsi 6,0% (plafon likuiditas) · Rp 6.000.000 · 529 lot`
- bila `plan.lots === 0` atau `plan.binding_cap !== null`: tampilkan `plan.note` sebagai baris kecil berwarna `var(--warn)`
- angka persen satu desimal (koma sebagai desimal, locale `id-ID`), rupiah memakai helper `rp` yang sudah ada di file itu, lot dengan pemisah ribuan titik.

- [ ] **Step 7: Typecheck**

Run: `node D:\Project\market-labs\idx-web\node_modules\typescript\bin\tsc --noEmit --project D:\Project\market-labs\idx-web\tsconfig.json`
Expected: tidak ada keluaran (bersih).

- [ ] **Step 8: Bukti angka dari API lebih dulu**

Restart API dulu bila flag Task 5 belum dijalankan, lalu catat angka nyata PEGE:
```
curl.exe -s "http://localhost:8000/api/stocks/PEGE/recommendation?modal=100000000&risk_pct=1&open_positions=0&exposure_pct=0" | .venv\Scripts\python.exe -c "import json,sys; c=json.load(sys.stdin)['candidate']; print(json.dumps(c['plan'], ensure_ascii=False))"
```
Expected: JSON rencana dengan `pct`, `lots`, `rupiah`, `risk_rupiah`, `note`. Simpan angkanya sebagai pembanding langkah berikut. Bila `pct` bukan 16,0 (mis. dipotong plafon likuiditas PEGE), itu **temuan yang sah**: catat di ledger dan jangan paksakan angka Lampiran A spec.

- [ ] **Step 9: Bukti render nyata (dengan modal terisi)**

Buat `D:\Project\market-labs\idx-web\public\_seed.html` (sementara, **jangan di-commit**) berisi skrip yang menulis keempat kunci localStorage sesuai angka langkah 8 lalu `location.href = "/rekomendasi"`, kemudian render dengan Chrome headless memakai profil tetap supaya localStorage satu origin:
```
& $chrome --headless=new --dump-dom --virtual-time-budget=25000 --user-data-dir="$env:TEMP\ml-p5-profile" http://localhost:3000/_seed.html | Select-String "Porsi|lot|rugi|ruang sisa"
```
(Resep Chrome yang sama dengan verifikasi P4, tercatat di `.superpowers/sdd/2026-10-18-p4-gerbang-rezim-pemeringkat/progress.md`.) Hapus `_seed.html` sesudahnya.
Expected: DOM memuat baris porsi dan lot yang angkanya **sama dengan keluaran API langkah 8**, plus `Ruang sisa` di strip.

- [ ] **Step 10: Bukti render mode persen (tanpa modal)**

Run: `& $chrome --headless=new --dump-dom --virtual-time-budget=25000 --user-data-dir="$env:TEMP\ml-p5-profile2" http://localhost:3000/rekomendasi | Select-String "Porsi"`
Expected: memuat `Porsi 16,0% modal` (atau porsi yang dipotong plafon likuiditas), tanpa `Rp` dan tanpa `lot`, dan strip memuat `Isi modal untuk melihat rupiah`.

- [ ] **Step 11: Commit**

```bash
git add lib/types.ts lib/portfolio.ts lib/hooks.ts components/PortfolioStrip.tsx app/rekomendasi/page.tsx
git commit -m "feat(p5): strip portofolio + baris porsi di kartu papan"
```

---

## Task 4: Web, panel Rencana posisi di halaman emiten (B)

**Files:**
- Create: `components/PositionPlanPanel.tsx`
- Modify: `lib/hooks.ts` (`useStockRecommendation` baris 451-456)
- Modify: `app/keputusan/DecisionWorkspace.tsx` (mount antara `<ContextTiles>` baris 472 dan `<DetailTabs>` baris 473)

**Interfaces:**
- Consumes: `usePortfolio`, `portfolioQuery` dari Task 3; payload `/api/stocks/{code}/recommendation` dengan `plan` dari Task 2.
- Produces: `useStockRecommendation(code: string | null, query = "")`; komponen `<PositionPlanPanel code={code} />` (self-fetching, tidak butuh prop selain `code`).

- [ ] **Step 1: Perluas hook**

`useStockRecommendation(code, query = "")` dengan key `` `/api/stocks/${encodeURIComponent(code)}/recommendation${query ? `?${query.slice(1)}` : ""}` `` (perhatikan `query` dari `portfolioQuery` diawali `&`, jadi potong karakternya atau bangun ulang; pilih satu bentuk dan pakai konsisten).

- [ ] **Step 2: Tulis `components/PositionPlanPanel.tsx`**

Client component. Isi: kotak isian `Modal` dan `Risiko per transaksi` (memakai `usePortfolio` yang sama, jadi angka yang diketik di papan terbaca di sini dan sebaliknya), lalu tabel `.data-table` berisi `Porsi`, `Nilai beli`, `Jumlah lot`, `Rugi maksimum`, `Plafon likuiditas`, `Plafon rezim` (dua terakhir dari `plan.caps`, tulis `tidak berlaku` bila `null`), dan baris penutup: `Rencana batal kalau harga menembus <stop>. Di bawah level itu tidak ada menambah posisi.`

Keadaan yang wajib ada (R-27): loading (`Skeleton` dari `components/States.tsx`), galat (`ErrorState`), tanpa data (`EmptyState`), dan `candidate === null` (pakai `reason` dari payload, bukan panel kosong tanpa sebab). Bila `withheld`, panel menampilkan level dan sebabnya, bukan ukuran posisi.

- [ ] **Step 3: Pasang di halaman emiten**

Di `DecisionWorkspace.tsx`, render `<PositionPlanPanel code={code} />` antara `<ContextTiles d={data} />` dan `<DetailTabs d={data} />`, dan hanya saat `code` ada.

- [ ] **Step 4: Typecheck**

Run: `node D:\Project\market-labs\idx-web\node_modules\typescript\bin\tsc --noEmit --project D:\Project\market-labs\idx-web\tsconfig.json`
Expected: bersih.

- [ ] **Step 5: Bukti render nyata**

Pakai `_seed.html` (dibuat ulang sementara, dihapus sesudah) yang mengarah ke `/keputusan/PEGE`, lalu:
```
& $chrome --headless=new --dump-dom --virtual-time-budget=25000 --user-data-dir="$env:TEMP\ml-p5-profile" http://localhost:3000/_seed.html | Select-String "Rencana posisi|Jumlah lot|Plafon likuiditas|Rencana batal"
```
Expected: keempat teks muncul, dan angka `Jumlah lot` sama dengan jawaban API untuk `PEGE` dengan keadaan yang sama (Task 3 langkah 8).

- [ ] **Step 6: Commit**

```bash
git add components/PositionPlanPanel.tsx lib/hooks.ts app/keputusan/DecisionWorkspace.tsx
git commit -m "feat(p5): panel rencana posisi di halaman emiten"
```

---

## Task 5: Bukti E2E menyeluruh

**Files:**
- Create: `.superpowers/sdd/2026-10-18-p5-rencana-posisi/progress.md` (ledger: ruling, perintah, keluaran nyata)

**Interfaces:**
- Consumes: semua task sebelumnya.

- [ ] **Step 1: Suite penuh dan gaya (idx-scraper)**

Run: `.venv\Scripts\python.exe -m pytest -q` dan `.venv\Scripts\python.exe -m ruff check src tests scripts`
Expected: semua lulus, ruff `All checks passed!`.

- [ ] **Step 2: Restart API**

Buat `_restart-api.flag` di root idx-scraper, lalu `Start-ScheduledTask -TaskName IDX-API`; tunggu `_restart-api.log` menunjukkan instance baru dan `guard-port.ps1 8000` tidak menolak. Catat PID baru di ledger.

- [ ] **Step 3: Sehat setelah restart**

Run: `curl.exe -s http://localhost:8000/api/recommendations?limit=3 | .venv\Scripts\python.exe -c "import json,sys; d=json.load(sys.stdin); print(d['pool_size'], d['total_candidates'], d['gate'] and d['gate']['blocked'], [r['code'] for r in d['rows']], [r.get('plan') is not None for r in d['rows']])"`
Expected: `191 46 <True|False> [kode...] [False, False, False]` (tanpa modal tidak ada rencana), dan baseline skor/level PEGE tidak berubah.

- [ ] **Step 4: D15 dengan HTTP nyata (papan vs emiten)**

Ambil `plan` PEGE dari papan 20 baris dan dari endpoint emiten dengan keadaan yang sama (mis. `modal=100000000&risk_pct=1&open_positions=2&exposure_pct=40`), lalu bandingkan dengan Python; dua objek harus identik. Bila kandidat PEGE tidak ada di papan pada limit tertentu, naikkan `limit` sampai ada, dan catat perintahnya di ledger.

- [ ] **Step 5: Verifikasi keadaan yang menahan**

Run: `curl.exe -s "http://localhost:8000/api/stocks/PEGE/recommendation?modal=100000000&open_positions=6" | .venv\Scripts\python.exe -c "import json,sys; c=json.load(sys.stdin)['candidate']; print(c['plan']['pct'], c['plan']['binding_cap'], c['plan']['note'])"`
Expected: `0.0 portfolio Sudah penuh: 6 posisi terbuka dari batas 6. Eksposur belum diisi, plafon portofolio dilewati.`

- [ ] **Step 6: Verifikasi q parameternya ditolak**

Run: `curl.exe -s -o NUL -w "%{http_code}\n" "http://localhost:8000/api/recommendations?modal=0"`
Expected: `422`.

- [ ] **Step 7: Render ketiga permukaan di browser nyata**

Ulangi dump-dom Task 3 langkah 9 (papan, modal terisi) dan Task 4 langkah 5 (emiten), lalu catat di ledger potongan DOM yang membuktikan: baris porsi + lot di kartu, strip memuat `Ruang sisa`, panel memuat `Rencana batal`. Hapus `public/_seed.html` sebelum commit berikutnya.

- [ ] **Step 8: Cek lampiran spec terhadap kenyataan**

Bila angka nyata berbeda dari Lampiran A spec (mis. plafon likuiditas PEGE memotong), tambahkan satu kalimat koreksi di Lampiran A spec (bukan mengubah rumusnya) dan catat di ledger.

- [ ] **Step 9: Commit ledger + koreksi spec**

```bash
git add .superpowers/sdd/2026-10-18-p5-rencana-posisi/progress.md docs/superpowers/specs/2026-10-18-spec-lapis-3-rencana-posisi-design.md
git commit -m "docs(p5): ledger bukti E2E + koreksi lampiran spec bila ada"
```

---

## Task 6: Rilis

**Files:**
- Modify: `D:\Project\market-labs\RENCANA-P0-P7.md` (baris P5)

- [ ] **Step 1: Review konteks segar sebelum merge**

Dispatch reviewer dengan konteks segar atas seluruh diff branch (pola P4), lalu tutup temuan Critical/Important.

- [ ] **Step 2: Merge ke `main` dan restart API**

Merge branch fitur ke `main` (tanpa fast-forward bila pola P4 dipakai), lalu ulangi Task 5 langkah 2 sampai 3 pada hasil merge.

- [ ] **Step 3: Push kedua repo**

Run di masing-masing repo: `git push origin main`, lalu `git rev-list --count origin/main..main`
Expected: `0` di idx-scraper dan idx-web.

- [ ] **Step 4: Perbarui roadmap**

Baris P5 di `RENCANA-P0-P7.md`: status selesai, SHA merge, PID API, dan ringkasan perubahan perilaku (tiga permukaan, empat plafon, modal di browser).

```bash
git add RENCANA-P0-P7.md
git commit -m "docs: P5 selesai (lapis 3 rencana posisi)"
```

---

## Catatan pelaksana

- **Urutan:** Task 1 dan 2 tidak bisa paralel (2 memakai 1). Task 3 dan 4 bisa berurutan saja (4 memakai `lib/portfolio.ts` dari 3). Task 5 sesudah keduanya. Task 6 paling akhir.
- **Repo web tidak punya test runner.** Jangan menambah jest/vitest/playwright untuk task ini; buktinya `tsc` + render nyata + angka API. Jangan menjalankan `npm run build` selama `next dev` hidup di :3000 (dipakai untuk render bukti).
- **`public/_seed.html` adalah alat verifikasi, bukan fitur.** Dibuat sementara, dihapus sesudah dipakai, dan tidak boleh masuk commit.
- **Satu langkah manusia yang tidak bisa digantikan agen:** pemilik membuka `/rekomendasi`, mengetik `100000000` di kolom modal, dan memastikan kartu menampilkan baris porsi dengan rupiah dan lot. Catat jawabannya di ledger.
- **Baseline P4 tidak boleh bergeser:** kolam 191, kandidat 46, karantina 16, ambang p60 Rp 895 jt, PEGE (skor 56.7, entry 113,42, stop 106,33, target 127,60, `own_percentile` 82.4).
- **Jangan menyentuh** `position_size_pct`, `entry_plan`, `_research_cache_put/_get`, `withhold` di `freshness.py`, atau cara kerja penahanan D1/gerbang rezim selain urutan pemanggilan yang dijelaskan Task 2 Step 4.
