# Rencana Implementasi — P6: Lapis 4 (Pengikat & Pengingat)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Klaim mesin masuk buku bukti sekali dan tidak bisa ditimpa, klaim yang tidak lengkap ditolak sistem, hasilnya diisi belakangan memakai harness P3, empat angkanya tampil di layar, dan ada gerbang klaim empat sebab yang bisa menghentikan penulisan klaim baru tanpa menghapus catatan.

**Architecture:** Satu modul murni (`research/journal.py`) memutuskan boleh-tidaknya dan bentuk klaim; satu penyimpan berkas (`journal_halt.py`) menyimpan halt manual dengan tulis atomik; satu penyimpan DB (`research/journal_store.py`) menulis buku, menolak, mengisi hasil, dan meringkas. `cli.py` mendapat tiga job (penulis klaim, pengisi hasil, pengingat) di jadwal sore yang sudah ada, dan `api/journal.py` menjadi lapisan baca tulis tipis untuk web. Web menempel di `/rekomendasi` tanpa menu baru.

**Tech Stack:** Python 3 (pandas, FastAPI, pydantic v2, psycopg, pytest, ruff, apscheduler) + Next.js 16 App Router, TypeScript, SWR, Tailwind v4.

**Spec:** `docs/superpowers/specs/2026-10-19-spec-lapis-4-pengikat-design.md` (revisi 1, ditulis 19 Okt 2026 atas permintaan "langsung mulai saja").

**Cara eksekusi (konvensi repo, sama seperti P3 sampai P5):**

- Branch kerja: `feat/p6-pengikat-pengingat` di `idx-scraper` (sudah dibuat, memuat commit spec `f918b91`) dan branch yang sama di `idx-web`. Kerja di tempat, bukan worktree (`next dev` :3000 dan task `IDX-API`/`IDX-Serve`/`IDX-Web` berjalan dari direktori ini).
- Mode eksekutor: inline (skill `executing-plans`), reviewer akhir dari konteks segar.
- Ledger kerja: `.superpowers/sdd/2026-10-19-p6-pengikat-pengingat/` (`plan-path` berisi path plan ini, `progress.md` memuat Setup, pre-flight scan antar task, baseline, dan bukti tiap task). Direktori itu di-`.gitignore`, jadi ledger tidak ikut commit.
- Perintah tes memakai venv repo: `.venv\Scripts\python.exe -m pytest -q` dan `.venv\Scripts\python.exe -m ruff check` (`pytest`/`ruff` tidak ada di PATH).

## Global Constraints

- **Buku ditulis sekali, tidak bisa ditimpa** (spec D1, P2 D5): kunci (`code`, `trade_date`, `rule`), `on conflict ... do nothing`. Rekam ulang tidak boleh mengubah angka lama.
- **Pre-commit menolak, bukan memperingatkan** (spec D3, P2 6.2): klaim tanpa stop, tanpa target, atau tanpa ukuran posisi tidak masuk buku.
- **Tidak mencatat posisi nyata** (spec D12, P2 Q3): tidak ada kolom lot yang dibeli pemilik, tidak ada harga fill, tidak ada laba-rugi uang nyata.
- **Dua dasar harga** (P2 6.4): level (`ref_raw`, `stop_raw`, `target_raw`) dari harga mentah; hasil (`outcome_abn`) dari harga terkoreksi. Satu klaim menyimpan `ref_adj`.
- **Satuan dikunci:** `size_pct` adalah persen sebagai angka (16,0 artinya 16% modal, sama dengan `position.pct`); `outcome_abn` memakai satuan `abn_k` milik `claim_eval` (fraksi, bukan persen).
- **Empat sebab gerbang, urutan tetap** (spec D5): `manual`, `data`, `regime`, `verdict`. Hanya sebab pertama yang dilaporkan.
- **Reuse, bukan tulis ulang** (spec D9, D10, D11): pengukuran hasil memakai `claim_eval.claim_outcomes`; verdict memakai `claim_eval.claim_verdict`; kalimat rekaman memakai `claim_eval.format_record_line`; patokan pasar memakai definisi di `/api/recommendations/track`.
- **Tidak menambah menu web** (P2 R6, spec D15).
- **Tidak mengubah perilaku P1 sampai P5.** Baseline data nyata sesi 2026-10-16 wajib tetap: kolam 191, kandidat 46, karantina 16, PEGE skor 56.7 / entry 113,42 / stop 106,33 / target 127,60.
- **Bahasa:** teks yang dilihat pengguna dan komentar kode dalam bahasa Indonesia, mengikuti gaya repo. Tidak memakai tanda pisah panjang (em dash).
- **Verifikasi:** `pytest` penuh + `ruff check src tests scripts` bersih di idx-scraper; `npx tsc --noEmit` + `next build` bersih di idx-web.
- **Berkas `sql/` dipasang manual** (`psql -f`, urut) dan DDL-nya di-mirror di Python (F3, F4). Keduanya wajib ada.

## Review Focus

Lima masukan yang spec minta tapi belum tentu diuji tes mana pun. Masing-masing mendapat tes di task pemiliknya.

1. **`recommendation_log` kosong untuk sesi itu** (job rekomendasi gagal atau sesi tanpa kandidat): penulis klaim harus mencatat penolakan sebab `kolam_kosong`, bukan menulis klaim kosong atau melempar. Tes: Task 3 `test_kolam_kosong_dicatat_sebagai_penolakan`.
2. **Bar harga hilang di dalam jendela horizon**: hasil klaim tetap `terbuka` (`unresolved`), tidak dianggap kedaluwarsa dan tidak diberi hasil karangan. Tes: Task 3 `test_bar_hilang_tidak_dianggap_kedaluwarsa`.
3. **Berkas halt rusak atau tidak bisa dibaca** (JSON cacat, izin ditolak): keadaan dibaca sebagai "tidak dihentikan" dengan catatan sebabnya, tanpa crash. Tes: Task 2 `test_berkas_halt_rusak_dianggap_tidak_dihentikan`.
4. **Dua kali tulis untuk kunci yang sama dengan angka berbeda**: baris lama menang; jumlah baris masuk 0 pada percobaan kedua. Tes: Task 3 `test_rekam_ulang_tidak_menimpa`.
5. **Tanggal sesi, bukan tanggal jam lokal**: job yang jalan lewat tengah malam tetap memakai sesi bursa terakhir, bukan tanggal hari itu. Tes: Task 4 `test_sesi_terakhir_dipakai_saat_lewat_tengah_malam`.

---

## Task 1: Modul murni aturan klaim

**Files:**
- Create: `src/idx_scraper/research/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: `research.claim_eval.VERDICT_HORIZON` (satu-satunya angka horizon), `research.position.position_plan` (ukuran posisi; dipakai di Task 4, bukan di modul murni ini).
- Produces (dipakai Task 3, 4, 5):
  - `RULE_PAPAN: str = "papan-teratas"`, `SHORTLIST_SIZE: int = 5`
  - `GATE_REASONS: tuple[str, ...] = ("manual", "data", "regime", "verdict")`
  - `REJECT_MISSING_STOP = "tanpa_stop"`, `REJECT_MISSING_TARGET = "tanpa_target"`, `REJECT_MISSING_SIZE = "tanpa_ukuran"`, `REJECT_GEOMETRY = "geometri"`, `REJECT_HORIZON = "horizon"`, `REJECT_EMPTY_POOL = "kolam_kosong"`
  - `claim_gate(*, manual_halt: bool = False, halt_reason: str | None = None, freshness_status: str | None = None, gate_blocked: bool = False, gate_reason: str | None = None, verdict: str | None = None) -> dict[str, Any]` mengembalikan `{"allowed": bool, "reason": str | None, "message": str}`
  - `claim_reject_reason(*, entry: float | None, stop: float | None, target: float | None, size_pct: float | None = None, size_lots: int | None = None, horizon_days: int | None = None) -> str | None`
  - `claim_from_candidate(candidate: dict[str, Any], *, trade_date: str, rule: str = RULE_PAPAN, ref_adj: float | None = None, regime: str | None = None, vol_state: str | None = None, freshness_status: str | None = None) -> dict[str, Any] | None` dengan kunci keluaran sama seperti kolom `research.claim_journal` (Task 3)
  - `summarize_claims(rows: list[dict[str, Any]], *, verdict: str | None = None) -> dict[str, Any]` mengembalikan kunci yang diminta `claim_eval.format_record_line` (`claims`, `hit_target`, `hit_stop`, `mean_abnormal`) ditambah `sessions`, `expired`, `open`, `horizon_mismatch`, `per_session`

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_journal.py`, gaya `tests/test_position.py` (murni, tanpa DB, helper kecil di atas):

```python
def test_gerbang_manual_menang_atas_sebab_lain():
    """Halt manual disebut lebih dulu, walau data juga basi dan rezim menolak."""
    out = claim_gate(manual_halt=True, halt_reason="berhenti sejenak",
                     freshness_status="basi", gate_blocked=True, gate_reason="rezim menolak")
    assert out["allowed"] is False
    assert out["reason"] == "manual"
    assert "berhenti sejenak" in out["message"]

def test_gerbang_urutan_sebab_manual_data_rezim_verdict():
    """Urutan tetap: tiap keadaan sendirian memulangkan sebabnya sendiri."""
    assert claim_gate(manual_halt=True, halt_reason="x")["reason"] == "manual"
    assert claim_gate(freshness_status="basi")["reason"] == "data"
    assert claim_gate(gate_blocked=True, gate_reason="rezim TRENDING_DOWN menolak")["reason"] == "regime"
    assert claim_gate(verdict="tidak terbukti")["reason"] == "verdict"

def test_gerbang_mengizinkan_saat_tidak_ada_sebab():
    out = claim_gate(freshness_status="segar", verdict="belum cukup")
    assert out["allowed"] is True
    assert out["reason"] is None

def test_gerbang_mengizinkan_saat_kesegaran_tak_diketahui():
    """Kesegaran None bukan basi: penahanan P4 yang memutuskan, bukan gerbang ini."""
    assert claim_gate()["allowed"] is True

def test_klaim_tanpa_stop_ditolak():
    assert claim_reject_reason(entry=113.42, stop=None, target=127.60, size_pct=16.0) == "tanpa_stop"

def test_klaim_tanpa_target_ditolak():
    assert claim_reject_reason(entry=113.42, stop=106.33, target=None, size_pct=16.0) == "tanpa_target"

def test_klaim_tanpa_ukuran_ditolak():
    assert claim_reject_reason(entry=113.42, stop=106.33, target=127.60) == "tanpa_ukuran"

def test_geometri_tidak_masuk_akal_ditolak():
    assert claim_reject_reason(entry=113.42, stop=113.42, target=127.60, size_pct=16.0) == "geometri"
    assert claim_reject_reason(entry=113.42, stop=106.33, target=113.42, size_pct=16.0) == "geometri"
    assert claim_reject_reason(entry=0.0, stop=-1.0, target=1.0, size_pct=16.0) == "geometri"

def test_klaim_lengkap_lolos_dan_bentuknya_sesuai_kolom():
    kandidat = {"code": "PEGE", "entry_ref": 113.42, "stop": 106.33, "target": 127.60,
                "horizon_days": 21, "grade": "A", "size_pct": 8.0}
    klaim = claim_from_candidate(kandidat, trade_date="2026-10-16", ref_adj=112.98)
    assert klaim is not None
    assert klaim["code"] == "PEGE" and klaim["rule"] == RULE_PAPAN
    assert klaim["direction"] == "BUY"
    assert klaim["ref_raw"] == 113.42 and klaim["ref_adj"] == 112.98
    assert klaim["status"] == "terbuka" and klaim["outcome_abn"] is None
    assert set(klaim) == set(CLAIM_COLUMNS)   # kunci = kolom tabel, tidak lebih

def test_klaim_tidak_lengkap_tidak_dikembalikan():
    assert claim_from_candidate({"code": "X", "entry_ref": 100.0, "stop": None, "target": 120.0},
                                trade_date="2026-10-16") is None

def test_agregat_dirata_per_sesi_bukan_per_klaim():
    """Dua sesi: sesi pertama 3 klaim (+1% rata-rata), sesi kedua 1 klaim (-3%).
    Rata-rata per sesi = -1,0%, bukan rata-rata per klaim = 0,0%."""
    rows = ([_row("2026-10-15", abn=0.01, status="kena_target")] * 3
            + [_row("2026-10-16", abn=-0.03, status="kena_stop", reason_status="kena_stop")])
    out = summarize_claims(rows)
    assert out["claims"] == 4
    assert out["sessions"] == 2
    assert out["hit_target"] == 3
    assert round(out["mean_abnormal"], 4) == -0.01
    assert out["per_session"][0]["sessions"] == "2026-10-15"

def test_buku_kosong_bukan_nol():
    out = summarize_claims([])
    assert out["claims"] == 0 and out["mean_abnormal"] is None
    assert out["sessions"] == 0 and out["per_session"] == []

def test_hasil_belum_terukur_tidak_dihitung_sebagai_nol():
    """Klaim terbuka tidak punya hasil: rata-rata tetap None, bukan 0,0%."""
    out = summarize_claims([_row("2026-10-16", abn=None, status="terbuka")])
    assert out["claims"] == 1 and out["open"] == 1
    assert out["mean_abnormal"] is None
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'idx_scraper.research.journal'`

- [ ] **Step 3: Implementasi `research/journal.py`**

Aturan yang dikunci (spec 5.1, 5.2, 5.5):

- `claim_gate` memeriksa berurutan `manual` (dari `manual_halt`), `data` (`freshness_status` ada dan bukan `"segar"`), `regime` (`gate_blocked`), `verdict` (`verdict == "tidak terbukti"`). Sebab pertama yang menolak itulah `reason`, dan `message` menyebut sebabnya dalam satu kalimat bahasa Indonesia. Tidak ada sebab berarti `allowed=True, reason=None`. Kesegaran `None` **bukan** basi.
- `claim_reject_reason` memeriksa urutan: stop, target, ukuran (`size_pct` atau `size_lots` wajib salah satu `> 0`), geometri (`entry > 0`, `stop < entry`, `target > entry`), horizon (`horizon_days >= 1`). Mengembalikan kode pertama yang gagal, atau `None`.
- `claim_from_candidate` membaca kandidat apa adanya (`code`, `entry_ref`, `stop`, `target`, `horizon_days`, `grade`) plus `size_pct`/`size_lots` yang sudah dihitung pemanggil; memulangkan `None` kalau `claim_reject_reason` berbunyi. Tidak ada angka yang dikarang: kolom yang tidak ada di kandidat ditulis `None` apa adanya.
- `summarize_claims` mengelompokkan per `trade_date`, menghitung rata-rata `outcome_abn` **per sesi dulu** (hanya klaim yang `outcome_abn` tidak `None`), lalu merata-ratakan antar sesi. `hit_target`/`hit_stop` dari status. `horizon_mismatch` = jumlah klaim yang `horizon_days != VERDICT_HORIZON` (spec D9).
- `CLAIM_COLUMNS` adalah daftar kolom tabel Task 3 dikurangi kolom yang diisi DB (`recorded_at`), dipakai tes bentuk klaim dan `record_claims`.
- Modul tanpa DB, tanpa I/O.

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal.py -v`
Expected: PASS (13 tes)

- [ ] **Step 5: Ruff + commit**

```bash
.venv\Scripts\python.exe -m ruff check src tests
git add src/idx_scraper/research/journal.py tests/test_journal.py
git commit -m "feat(p6): aturan murni gerbang klaim + bentuk klaim + agregat per sesi"
```

---

## Task 2: Penyimpan halt manual

**Files:**
- Create: `src/idx_scraper/journal_halt.py`
- Test: `tests/test_journal_halt.py`

**Interfaces:**
- Consumes: pola `alerts_store` (`_path`, tulis atomik tmp + replace, path bisa ditimpa env).
- Produces (dipakai Task 4, 5):
  - `HALT_PATH_ENV = "IDX_JOURNAL_HALT"`, `DEFAULT_PATH = "data/journal_halt.json"`
  - `state(path: str | Path | None = None) -> dict[str, Any]` mengembalikan `{"halted": bool, "reason": str | None, "at": str | None, "by": str | None, "note": str | None}`
  - `set_halt(*, reason: str, by: str = "pemilik", path: str | Path | None = None) -> dict[str, Any]`
  - `clear(*, by: str = "pemilik", path: str | Path | None = None) -> dict[str, Any]`

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_journal_halt.py`, memakai `tmp_path`:

```python
def test_halt_tanpa_sebab_ditolak(tmp_path):
    with pytest.raises(ValueError):
        set_halt(reason="   ", path=tmp_path / "halt.json")

def test_halt_menyala_terbaca_lengkap(tmp_path):
    p = tmp_path / "halt.json"
    set_halt(reason="rule saya dilanggar dua kali", by="pemilik", path=p)
    st = state(path=p)
    assert st["halted"] is True
    assert st["reason"] == "rule saya dilanggar dua kali"
    assert st["by"] == "pemilik" and st["at"]

def test_halt_mati_menyisakan_jejak_di_berkas(tmp_path):
    p = tmp_path / "halt.json"
    set_halt(reason="uji", path=p)
    clear(by="pemilik", path=p)
    st = state(path=p)
    assert st["halted"] is False and st["reason"] is None

def test_berkas_belum_ada_berarti_tidak_dihentikan(tmp_path):
    st = state(path=tmp_path / "tidak-ada.json")
    assert st == {"halted": False, "reason": None, "at": None, "by": None, "note": None}

def test_berkas_halt_rusak_dianggap_tidak_dihentikan(tmp_path):
    """Fail-open, tapi sebabnya terlihat: mesin tidak boleh berhenti karena berkas cacat."""
    p = tmp_path / "halt.json"
    p.write_text("{bukan json", encoding="utf-8")
    st = state(path=p)
    assert st["halted"] is False
    assert st["note"] and "rusak" in st["note"]

def test_path_env_dipakai_kalau_argumen_kosong(tmp_path, monkeypatch):
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "dari-env.json"))
    set_halt(reason="dari env")
    assert state()["halted"] is True
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_halt.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementasi `journal_halt.py`**

Ikuti `alerts_store.py` untuk tulis atomik dan pembacaan `env`; bedanya berkas ini menyimpan **satu** keadaan, bukan daftar. `state()` tidak pernah melempar: berkas tidak ada atau isinya tidak terbaca berarti `halted=False` dengan `note` yang menyebut sebab (rusak atau tidak terbaca). `set_halt` mewajibkan `reason` tidak kosong; `clear` menulis `halted=False` dan menyimpan jejak waktu terakhir.

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_halt.py -v`
Expected: PASS (6 tes)

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/journal_halt.py tests/test_journal_halt.py
git commit -m "feat(p6): penyimpan halt manual (tulis atomik, fail-open saat berkas rusak)"
```

---

## Task 3: Skema buku klaim + penyimpan DB

**Files:**
- Create: `sql/journal.sql`, `src/idx_scraper/research/journal_store.py`
- Modify: `DEPLOY-LINUX.md` (daftar `psql -f`, dekat baris 194)
- Test: `tests/test_journal_store.py`

**Interfaces:**
- Consumes: `research.journal` (Task 1), `research.claim_eval.claim_outcomes`, `research.recommendation_log` (tabel), `research.prices_adjusted` (view, `adj_close`), `research.raw_eod`.
- Produces (dipakai Task 4, 5):
  - `_DDL: str`, `ensure_table(cur) -> None`
  - `candidates_for(dsn: str, trade_date: str, *, limit: int = SHORTLIST_SIZE) -> list[dict[str, Any]]` (baris `recommendation_log` + `size_pct` dari `position.position_plan`, urut seperti papan)
  - `record_claims(cur, claims: list[dict[str, Any]]) -> int` (jumlah baris yang benar-benar masuk)
  - `record_refusal(cur, *, trade_date: str, reason: str, detail: str | None = None) -> None`
  - `panel(conn, *, codes: list[str], start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]` (frame `raw` dan `adj` dengan kolom yang diminta `claim_eval`, yaitu `code, date, high, low, close` dan `code, date, close`)
  - `fill_outcomes(cur, *, trade_date: str | None = None) -> dict[str, int]` mengembalikan `{"diperiksa", "kena_target", "kena_stop", "kedaluwarsa", "terbuka"}`
  - `journal_summary(cur, *, rule: str | None = None, code: str | None = None) -> dict[str, Any]` (hasil `summarize_claims` + `record_line` dari `claim_eval.format_record_line`)
  - `gaps(cur, *, sessions: int = 10) -> list[dict[str, Any]]` (sesi tanpa klaim **dan** tanpa penolakan)

**DDL (salin persis; mirror di `_DDL`)**

```sql
CREATE TABLE IF NOT EXISTS research.claim_journal (
    code            TEXT          NOT NULL,
    trade_date      DATE          NOT NULL,
    rule            TEXT          NOT NULL,
    direction       TEXT          NOT NULL DEFAULT 'BUY',
    grade           TEXT,
    ref_raw         NUMERIC(18,4) NOT NULL,
    stop_raw        NUMERIC(18,4) NOT NULL,
    target_raw      NUMERIC(18,4) NOT NULL,
    ref_adj         NUMERIC(18,4),
    horizon_days    INTEGER       NOT NULL,
    size_pct        NUMERIC(9,4),
    size_lots       INTEGER,
    size_rupiah     NUMERIC(18,2),
    regime          TEXT,
    vol_state       TEXT,
    freshness_status TEXT,
    status          TEXT          NOT NULL DEFAULT 'terbuka'
                    CHECK (status IN ('terbuka','kena_target','kena_stop','kedaluwarsa')),
    outcome_abn     NUMERIC(12,8),
    touch_date      DATE,
    recorded_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date, rule)
);
CREATE INDEX IF NOT EXISTS idx_claim_journal_date ON research.claim_journal (trade_date DESC, status);

CREATE TABLE IF NOT EXISTS research.claim_refusal (
    trade_date DATE        NOT NULL,
    reason     TEXT        NOT NULL,
    detail     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, reason)
);
```

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_journal_store.py`, memakai `_FakeCursor` seperti `tests/test_corp_action_filter.py:91-114` (tanpa Postgres):

```python
def test_rekam_ulang_tidak_menimpa():
    """Percobaan kedua dengan angka berbeda harus masuk 0 dan tidak mengubah baris lama."""
    cur = _FakeCursor()
    pertama = [_claim(code="PEGE", stop=106.33, target=127.60)]
    kedua = [_claim(code="PEGE", stop=100.00, target=130.00)]
    assert record_claims(cur, pertama) == 1
    assert record_claims(cur, kedua) == 0
    rows = cur.rows("claim_journal")
    assert len(rows) == 1 and float(rows[0]["stop_raw"]) == 106.33

def test_kolam_kosong_dicatat_sebagai_penolakan(monkeypatch):
    """recommendation_log kosong: tidak ada klaim, tapi sesinya dijelaskan."""
    cur = _FakeCursor()
    monkeypatch.setattr(journal_store, "candidates_for", lambda *a, **k: [])
    out = write_session(cur, trade_date="2026-10-16")      # fungsi tipis di modul ini
    assert out["written"] == 0
    assert cur.rows("claim_refusal")[0]["reason"] == "kolam_kosong"

def test_gerbang_menolak_maka_tidak_ada_klaim_dan_sebabnya_tercatat(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(journal_store, "candidates_for", lambda *a, **k: [_claim()])
    out = write_session(cur, trade_date="2026-10-16", gate=claim_gate(freshness_status="basi"))
    assert out["written"] == 0
    assert cur.rows("claim_refusal")[0]["reason"] == "data"

def test_bar_hilang_tidak_dianggap_kedaluwarsa():
    """Bar mentah hilang di tengah jendela: klaim tetap terbuka, bukan kedaluwarsa."""
    rows = fill_outcomes_from_frames(claims=[_claim(horizon=3)],
                                     raw=pd.DataFrame([{"code": "PEGE", "date": "2026-10-15",
                                                        "high": 120.0, "low": 100.0, "close": 110.0}]),
                                     adj=pd.DataFrame([...dua sesi, satu hilang...]))
    assert rows[0]["status"] == "terbuka" and rows[0]["outcome_abn"] is None

def test_hasil_jelek_ditulis_apa_adanya():
    rows = fill_outcomes_from_frames(claims=[_claim(horizon=1)], raw=..., adj=...)
    assert rows[0]["status"] == "kena_stop"
    assert rows[0]["outcome_abn"] < 0

def test_ringkasan_memakai_kalimat_rekaman_yang_sudah_ada():
    """Kunci yang diminta format_record_line dipenuhi, dan kalimat kosongnya dipakai apa adanya."""
    s = journal_summary(_FakeCursor(), rule=RULE_PAPAN)
    assert s["record_line"] == "Aturan ini: belum punya rekaman"

def test_sesi_tanpa_klaim_dan_tanpa_penolakan_masuk_daftar_celah():
    celah = gaps(_FakeCursor(), sessions=5)
    assert celah and celah[0]["trade_date"] == "2026-10-16"
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_store.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementasi `journal_store.py` + `sql/journal.sql`**

Aturan yang dikunci:

- `ensure_table` menjalankan `_DDL` dengan `create ... if not exists`, dipanggil sebelum menulis (pola `recommendation_log.ensure_table`).
- `record_claims` memakai `insert ... on conflict (code, trade_date, rule) do nothing` **satu per baris** (bukan `executemany`) supaya jumlah baris masuk bisa dihitung; kolom yang tidak ada di klaim diisi `None`, bukan 0.
- `write_session(cur, *, trade_date, gate=None)` (fungsi tipis di modul ini, dipakai CLI Task 4): ambil `candidates_for`; kosong berarti `record_refusal(reason="kolam_kosong")`; kalau `gate` menolak, `record_refusal(reason=gate["reason"], detail=gate["message"])`; kalau lolos, bangun klaim via `claim_from_candidate` untuk setiap kandidat, tulis yang lengkap, dan catat `record_refusal(reason=...)` per kandidat yang ditolak pre-commit. Mengembalikan `{"written": n, "refused": n}`.
- `panel` mengembalikan frame dengan **nama kolom yang diminta `claim_eval`**: `raw` = `code, date, high, low, close` dari `research.raw_eod`; `adj` = `code, date, close` dari `research.prices_adjusted.adj_close` **via `research.latest_pit`** (PIT, bukan `raw_eod`).
- `fill_outcomes` membangun frame klaim dari `claim_journal` (kolom `code, trade_date AS date, direction, stop_raw AS stop, target_raw AS target, horizon_days`), memanggil `claim_eval.claim_outcomes(claims, raw=..., adj=..., horizons=(VERDICT_HORIZON,))`, lalu memetakan hasil apa adanya: `stop` → `kena_stop`, `target` → `kena_target`, `timeout` → `kedaluwarsa`, `unresolved` → tetap `terbuka`. `outcome_abn` diambil dari kolom `abn_{VERDICT_HORIZON}` pada satuan aslinya (fraksi). Perpindahan status **tidak dibatalkan**: klaim yang statusnya bukan `terbuka` tidak diperiksa ulang.
- `journal_summary` merakit baris dengan kunci yang diminta `summarize_claims`, memanggilnya, lalu menambahkan `record_line = claim_eval.format_record_line(...)` memakai `claims`, `hit_target`, `hit_stop`, `mean_abnormal` (satu formatter, tidak ada yang kedua).
- `gaps` mencari sesi di dalam `sessions` terakhir yang **tidak** punya baris di `claim_journal` **dan tidak** punya baris di `claim_refusal`.
- Berkas `sql/journal.sql` menyalin DDL di atas apa adanya, dengan komentar gaya `sql/signal_log.sql` yang menyebut mirror Python-nya.

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_store.py -v`
Expected: PASS

- [ ] **Step 5: Pasang DDL ke DB lokal dan buktikan tabelnya ada**

`ensure_table` sudah memasangnya sendiri saat perintah pertama dijalankan, tapi buktikan juga lewat berkas `sql/` (itu yang dipakai deploy Linux):

```powershell
$dsn = (Select-String -Path .env -Pattern '^DATABASE_URL=').Line -replace '^DATABASE_URL=',''
psql $dsn -f sql/journal.sql
psql $dsn -c "\d research.claim_journal"
```
Expected: dua tabel + index muncul; perintah aman diulang (`if not exists`).

- [ ] **Step 6: Commit**

```bash
git add sql/journal.sql src/idx_scraper/research/journal_store.py tests/test_journal_store.py DEPLOY-LINUX.md
git commit -m "feat(p6): skema buku klaim + penyimpan (tulis sekali, tolak, isi hasil, ringkas)"
```

---

## Task 4: CLI + tiga job sore

**Files:**
- Modify: `src/idx_scraper/cli.py` (blok parser `add_parser` + dispatch, dan blok `add_job` sekitar baris 1789-1826)
- Modify: `tests/test_serve_schedule.py:136-146` (peta cron)
- Test: `tests/test_journal_cli.py`

**Interfaces:**
- Consumes: Task 1, 2, 3.
- Produces:
  - `idx journal record [--trade-date YYYY-MM-DD]`
  - `idx journal outcomes [--trade-date YYYY-MM-DD]`
  - `idx journal summary [--rule RULE] [--code CODE]`
  - `idx journal state`
  - `idx journal halt --reason "..." [--by NAMA]`
  - `idx journal resume`
  - job `journal_claims` (mon-fri, `hour="17-21"`, `minute=0`, `coalesce=True`, `misfire_grace_time=7200`)
  - job `journal_outcomes` (mon-fri, 17:30)
  - job `journal_reminder` (mon-fri, 17:45)
  - `_last_session_date(dsn) -> str` (hari bursa terakhir yang ada di `research.raw_eod`)

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_journal_cli.py` (memakai fixture `jobs` gaya `tests/test_serve_schedule.py:65-88` untuk bagian jadwal, dan `monkeypatch` untuk bagian perintah):

```python
def test_sesi_terakhir_dipakai_saat_lewat_tengah_malam(monkeypatch):
    """Job jalan 00:10 WIB: sesi yang dipakai tetap sesi bursa terakhir, bukan tanggal itu."""
    monkeypatch.setattr(cli, "_now_wib", lambda: datetime(2026, 10, 20, 0, 10, tzinfo=WIB))
    monkeypatch.setattr(cli, "_last_session_date", lambda dsn: "2026-10-19")
    assert cli._journal_session(dsn="x") == "2026-10-19"

def test_perintah_halt_tanpa_sebab_keluar_dengan_pesan(args_halt_tanpa_sebab, capsys):
    cli.cmd_journal_halt(args_halt_tanpa_sebab)
    assert "sebab" in capsys.readouterr().err.lower()

def test_jadwal_cron_lengkap(jobs):
    assert _cron(jobs)["journal_claims"] == ("mon-fri", "17-21", 0)
    assert _cron(jobs)["journal_outcomes"] == ("mon-fri", 17, 30)
    assert _cron(jobs)["journal_reminder"] == ("mon-fri", 17, 45)
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_cli.py tests/test_serve_schedule.py -v`
Expected: FAIL (perintah dan job belum ada; peta cron lama belum memuat id baru)

- [ ] **Step 3: Implementasi di `cli.py`**

- `_journal_session(dsn)`: pakai `--trade-date` kalau diisi; kalau tidak, sesi terakhir dari `research.raw_eod` (`max(trade_date)`), bukan tanggal jam lokal.
- Perintah `journal` mengikuti gaya `cmd_*` lain: DSN dari env, keluar dengan `[err]` kalau kosong, impor modul di dalam fungsi, cetak ringkas. `record` mencetak `{"written", "refused"}`; `outcomes` mencetak hitungan per status; `summary` mencetak `record_line`; `halt` mewajibkan `--reason` dan menolak yang kosong dengan pesan yang menyebut kata "sebab"; `resume` mencetak keadaan sesudahnya.
- Tiga job baru didaftarkan dengan gaya yang sudah ada (`add_job`, `CronTrigger`, `id=`, `replace_existing=True`, `misfire_grace_time`, `coalesce=True`). `journal_claims` sengaja diulang tiap jam 17:00 sampai 21:00 dengan pola yang sama seperti `eod_fallback`: kalau data sesi belum masuk, ia mencatat penolakan dan mencoba lagi jam berikutnya; kalau klaim sudah ditulis, ia tidak menulis apa-apa.
- Pengingat (`journal_reminder`) memakai `TelegramNotifier` dan formatter murni baru di `notify.py` (`format_claim_hits(rows) -> str`), plus penjaga bisu: kalau tidak ada klaim yang ditulis dan tidak ada baris penolakan untuk sesi itu, kirim satu pesan alarm.

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_cli.py tests/test_serve_schedule.py -v`
Expected: PASS (peta cron lama + tiga id baru)

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/cli.py src/idx_scraper/notify.py tests/test_journal_cli.py tests/test_serve_schedule.py
git commit -m "feat(p6): perintah idx journal + tiga job sore (tulis, isi hasil, ingatkan)"
```

---

## Task 5: API baca tulis

**Files:**
- Create: `src/idx_scraper/api/journal.py`
- Modify: `src/idx_scraper/api/app.py` (rute tipis), `src/idx_scraper/api/schemas.py` (model)
- Test: `tests/test_journal_api.py`

**Interfaces:**
- Consumes: Task 1, 2, 3 (`journal_store`, `journal_halt`, `journal.claim_gate`).
- Produces:
  - `GET /api/journal/summary?rule=&code=` → `JournalSummary` (`claims`, `sessions`, `hit_target`, `hit_stop`, `expired`, `open`, `horizon_mismatch`, `mean_abnormal`, `per_session`, `verdict`, `record_line`)
  - `GET /api/journal/claims?trade_date=&code=&limit=` → `list[JournalClaim]`
  - `GET /api/journal/state` → `JournalGateState` (`allowed`, `reason`, `message`, `halt` (`halted`, `reason`, `at`, `by`, `note`), `gaps`)
  - `POST /api/journal/halt` (body `JournalHaltRequest {reason, by}`) → `JournalGateState`; `reason` kosong → HTTP 422
  - `POST /api/journal/resume` (body `JournalResumeRequest {by}`) → `JournalGateState`
  - `api/journal.py`: `summary(*, rule=None, code=None) -> dict`, `claims(*, trade_date=None, code=None, limit=50) -> list[dict]`, `state() -> dict`, `halt(*, reason, by) -> dict`, `resume(*, by) -> dict`

- [ ] **Step 1: Tulis tes yang gagal**

`tests/test_journal_api.py`, gaya `tests/test_position_plan_api.py` (fungsi lapisan API dengan cursor palsu yang di-`monkeypatch`):

```python
def test_state_menyebut_sebab_penolakan_yang_berlaku(monkeypatch, tmp_path):
    monkeypatch.setenv(HALT_PATH_ENV, str(tmp_path / "halt.json"))
    set_halt(reason="uji", path=tmp_path / "halt.json")
    st = journal_api.state()
    assert st["allowed"] is False and st["reason"] == "manual"
    assert st["halt"]["halted"] is True

def test_halt_tanpa_sebab_menghasilkan_422():
    with pytest.raises(HTTPException) as e:
        app.halt_endpoint(JournalHaltRequest(reason="  ", by="pemilik"))
    assert e.value.status_code == 422

def test_summary_memakai_kalimat_rekaman(monkeypatch):
    monkeypatch.setattr(journal_api, "_summary", lambda **k: {"claims": 0, "record_line": "Aturan ini: belum punya rekaman"})
    assert journal_api.summary()["record_line"] == "Aturan ini: belum punya rekaman"

def test_claims_tanpa_trade_date_memakai_sesi_terakhir(monkeypatch):
    ...
```

- [ ] **Step 2: Jalankan tes, pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_api.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implementasi lapisan + rute**

- `api/journal.py` mengikuti bentuk `api/alerts.py`: seluruh logika di sini, `app.py` hanya handler tipis yang memanggilnya dan mengubah `ValueError` menjadi HTTP 422.
- Model pydantic polos tanpa `alias`, field opsional default `None` (konvensi `schemas.py`), dan docstring model menyebut aturan "tidak tersedia bukan nol" (F12, F13 spec).
- `state()` merakit `claim_gate(...)` dari sumber yang sudah ada: `journal_halt.state()`, `services.get_freshness()` untuk kesegaran, `analytics.get_market_regime()` untuk gerbang rezim, `journal_store.journal_summary(...)["verdict"]` untuk verdict. Cursor hilang atau DB mati tidak boleh membuat rute 500: keadaan dibaca apa adanya dengan `note` (fail-open seperti penahanan P4).

- [ ] **Step 4: Jalankan tes, pastikan lulus**

Run: `.venv\Scripts\python.exe -m pytest tests/test_journal_api.py -v`
Expected: PASS

- [ ] **Step 5: Bukti HTTP nyata**

```bash
# hidupkan ulang API supaya kode baru benar-benar jalan (pola jalur terjaga P5)
ni _restart-api.flag; Start-ScheduledTask -TaskName IDX-API; Start-Sleep 8
curl.exe -s "http://127.0.0.1:8000/api/journal/summary" ; curl.exe -s "http://127.0.0.1:8000/api/journal/state"
```
Expected: JSON dengan `record_line` (buku kosong berarti "Aturan ini: belum punya rekaman") dan `state` yang menyebut `allowed` beserta sebabnya.

- [ ] **Step 6: Commit**

```bash
git add src/idx_scraper/api/journal.py src/idx_scraper/api/app.py src/idx_scraper/api/schemas.py tests/test_journal_api.py
git commit -m "feat(p6): API buku klaim (ringkasan, klaim, keadaan gerbang, halt/resume)"
```

---

## Task 6: Permukaan web + bukti sesi nyata

**Files:**
- Modify: `idx-web/lib/types.ts`, `idx-web/lib/hooks.ts`, `idx-web/app/rekomendasi/page.tsx`
- Create: `idx-web/components/ClaimRecord.tsx`
- Modify: `../RENCANA-P0-P7.md` (baris P6 + §Sudah ditutup), ledger `.superpowers/sdd/2026-10-19-p6-pengikat-pengingat/progress.md`

**Interfaces:**
- Consumes: Task 5 (`/api/journal/summary`, `/api/journal/state`, `POST /api/journal/halt`, `POST /api/journal/resume`).
- Produces: `ClaimRecord` (baris rekaman per aturan + penanda gerbang + tombol halt dengan modal konfirmasi); hook `useJournalSummary()`, `useJournalState()`, `useJournalHalt()`, `useJournalResume()`.

- [ ] **Step 1: Jalankan jalur nyata pada sesi yang sudah ada**

```bash
idx journal record --trade-date 2026-10-16
idx journal outcomes --trade-date 2026-10-16
idx journal summary
```
Expected: klaim sesi 2026-10-16 masuk buku (maksimum 5 baris, sebab `papan-teratas`), `record_line` menyebut jumlah klaim dan hasilnya apa adanya, dan **angka baseline P1 sampai P5 tidak berubah** (kolam 191, kandidat 46, PEGE seperti lampiran P5 spec).

- [ ] **Step 2: Tulis komponen + hook, lalu tempel di `/rekomendasi`**

- Baris rekaman: ambil `record_line` dari API apa adanya (satu formatter, di Python); jangan menyusun kalimat rekaman di TypeScript.
- Blok empat angka di atas papan: `claims`, `sessions`, `hit_target`, `hit_stop`, `mean_abnormal`, dan `verdict` bila ada.
- Penanda gerbang: pakai kosakata yang sudah ada (`withheld`, `gate.reason`, `FreshnessBadge`), jangan bikin istilah baru.
- Tombol halt: ikut pola modal yang sudah jadi (role dialog, Escape, fokus kembali), sebab wajib diisi, keadaan tampil sesudah aksi; pakai kelas `.btn`, `.card`, `.badge` yang sudah ada, dan `tabular-nums` untuk semua angka.
- Tidak ada menu baru di `Sidebar.GROUPS` atau `CommandPalette.NAV_ACTIONS` (P2 R6, spec D15).
- Keadaan kosong dan gagal wajib ada: buku kosong memakai kalimat API, rute gagal memakai `ErrorState`.

- [ ] **Step 3: Verifikasi web**

```bash
cd idx-web; npx tsc --noEmit; npm run build
```
Expected: keduanya bersih; lalu buka `http://localhost:3000/rekomendasi` dan pastikan baris rekaman, blok empat angka, dan tombol halt terlihat (render nyata, bukan hanya HTTP 200).

- [ ] **Step 4: Tulis bukti ke ledger + perbarui rencana induk**

- Ledger `.superpowers/sdd/2026-10-19-p6-pengikat-pengingat/progress.md`: tempel keluaran `idx journal summary`, potongan baris buku sesi 2026-10-16, hasil `curl` keadaan gerbang, dan potongan DOM/PNG render nyata `/rekomendasi`.
- `../RENCANA-P0-P7.md` (di `D:\Project\market-labs`, bukan repo git): baris P6 di tabel status menjadi selesai + merge commit, dan satu butir baru di §"Sudah ditutup" yang menyebut: buku klaim hidup, empat sebab gerbang, dan **P5 R2/R8 tidak ditutup** (spec R1, keputusan Q3).

- [ ] **Step 5: Suite penuh + commit**

```bash
cd ../idx-scraper; .venv\Scripts\python.exe -m pytest -q; .venv\Scripts\python.exe -m ruff check src tests scripts
git add idx-web/lib/types.ts idx-web/lib/hooks.ts idx-web/components/ClaimRecord.tsx idx-web/app/rekomendasi/page.tsx
git commit -m "feat(p6): permukaan buku klaim di /rekomendasi + bukti sesi 2026-10-16"
```
Expected: semua lulus; commit terakhir di branch `feat/p6-pengikat-pengingat`. (Dua repo punya commit masing-masing; jangan pakai `git add -A`, ada berkas lokal `idx-web/.playwright-cli/` yang tidak dilacak dan tidak boleh ikut.)
