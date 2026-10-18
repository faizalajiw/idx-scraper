# P3 — Harness Validasi Jujur: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Membuat harness walk-forward yang sudah ada bisa dipercaya (punya tes), dan membangun mesin penilaian klaim (hasil per klaim, agregat per sesi, verdict terbukti/tidak) sebagai fungsi murni yang teruji.

**Architecture:** Tiga lapis, dari bawah: (1) **harness** `research/score_eval.py` yang sudah menurunkan bobot OOS — sekarang diberi tes supaya angkanya boleh disebut bukti; (2) **pengukur hasil klaim** baru di `research/claim_eval.py` yang memakai ulang `signal_log.attach_outcomes` untuk abnormal vs pasar; (3) **verdict** di modul yang sama, memutuskan "terbukti / tidak terbukti / belum cukup" dengan ambang yang sudah disetujui pemilik. Semuanya murni (tanpa DB, tanpa jaringan), sesuai disiplin repo: tes tidak menyentuh DB.

**Tech Stack:** Python 3, pandas, numpy, pytest. Tanpa dependensi baru.

**Spec:** `docs/superpowers/specs/2026-10-18-spec-mesin-keputusan-design.md` (bagian §6.1 "Cara dinilai & cara gagal", D5, D6, R3, R7; keputusan §8 Q1/Q4)

## Global Constraints

Disalin apa adanya dari spec — berlaku di setiap tugas:

- **Ambang verdict klaim: 30 klaim, atau 6 minggu (± 30 sesi bursa), mana yang lebih dulu** (spec §8 Q4). Konstanta bernama, satu tempat.
- **Agregat dihitung per sesi, bukan per klaim** (spec §6.1, R7). 3–5 klaim sehari adalah **satu** pengamatan sesi.
- **Harga mentah untuk level, harga terkoreksi untuk hasil** (spec §6.4, R3). Level entry/stop/target tidak pernah diukur pada harga terkoreksi.
- **Benchmark = pasar berimbang dari panel penuh**, bukan dari universe kandidat (aturan yang sudah dipegang `score_eval.benchmark_prices`).
- **Stop menang** bila target dan stop tersentuh di sesi yang sama (spec §6.1; urutan intra-sesi tidak diketahui).
- **Tidak ada penggunaan data masa depan** di jalur yang menurunkan bobot; label `abn_k` boleh melihat ke depan, tapi jendela train tidak pernah memuat sesi test.
- **Tes tidak menyentuh DB atau jaringan.** Jalur DB dipisahkan dari jalur murni, seperti modul yang sudah ada.
- **Bahasa kode dan teks mengikuti repo:** nama fungsi/docstring Inggris–Indonesia campur seperti modul tetangga; kalimat yang dibaca pengguna (baris rekaman) berbahasa Indonesia.
- Perintah tes: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/<file> -v` (konfigurasi repo: `-q`).
- Lint: `ruff check src tests scripts` harus bersih. Status P3 di `RENCANA-P0-P7.md` (di luar repo) diperbarui sekali di akhir, bukan per tugas.

## Review Focus

Lima hal yang spec danplan ini tidak dijamin oleh tes tugas mana pun sampai tesnya ditulis di tugas yang bersangkutan — diurutkan dari yang paling mungkin menggigit:

1. **`feats` tanpa kolom fitur lengkap.** Kalau kunci `COMPONENT_NOTES` berubah, harness melempar `KeyError` di tengah hitungan — harus gagal jelas dan berhenti, bukan diam-diam menghitung nol. → tes di Task 2.
2. **Sesi di bawah `MIN_NAMES_PER_DAY`.** Kalau semua sesi tersaring habis, hasilnya harus "tidak ada sesi terukur" — bukan t-stat yang dibangun dari sampel kosong. → tes di Task 2 dan Task 3.
3. **Klaim yang horizonnya belum tercapai.** Baris di sesi terakhir belum punya hasil; ia harus terhitung `unresolved` dan tidak ikut dirata-ratakan sebagai nol. → tes di Task 5 dan Task 6.
4. **Panel mentah dan terkoreksi berbeda panjang atau beda kode.** Kehilangan baris saat penggabungan tidak boleh menghasilkan `abn` yang lebih kecil tanpa jejak. → tes di Task 5.
5. **Klaim dengan arah selain `BUY`.** Menghitung stop/target terbalik lebih buruk daripada menolak: harus melempar error yang jelas. → tes di Task 5.

---

### Task 1: Lipatan walk-forward tidak melihat masa depan

**Files:**
- Create: `tests/test_score_eval.py`
- Read (jangan ubah): `src/idx_scraper/research/score_eval.py` (`walk_forward`, `session_dates`)

**Interfaces:**
- Consumes: `score_eval.walk_forward(feats, *, horizons, train, test, step, min_consistency) -> dict`; `score_eval.attach_abnormal(feats, horizons, prices) -> pd.DataFrame`; `recommend.COMPONENT_NOTES`
- Produces (helper di dalam file tes, dipakai Task 2 dan 3): `_synthetic_prices(sessions: int, codes: int) -> pd.DataFrame` (kolom `code,date,close`) dan `_feats_from_prices(prices: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame` (kolom `code,date,close,setup`, satu kolom per kunci `COMPONENT_NOTES` bernilai 0.0/1.0, plus `abn_{k}` dari `attach_abnormal`)

- [ ] **Step 1: Tulis helper + tes yang gagal**

Buat `tests/test_score_eval.py`. Helper: `_synthetic_prices(sessions: int, codes: int) -> pd.DataFrame` menghasilkan panel deterministik tanpa angka acak (mis. `close = 100 + code_index + day_index * (1 + code_index % 3) / 10`) dengan kode bernama `C000`, `C001`, …, `C024` (tiga digit, urut indeks); `_feats_from_prices` menyusun frame fitur lengkap. Tes:

```python
def test_walk_forward_test_window_always_after_train():
    report = walk_forward(_feats_from_prices(_synthetic_prices(160, 25), HORIZONS), train=100, test=25, step=25)
    folds = report["by_horizon"]["21"]["fold_detail"]
    assert folds
    for fold in folds:
        assert fold["train"][1] < fold["test"][0]

def test_walk_forward_test_windows_do_not_overlap():
    ...
    spans = [set(pd.date_range(fold["test"][0], fold["test"][1])) for fold in folds]
    for a, b in zip(spans, spans[1:]):
        assert not (a & b)

def test_walk_forward_fold_count_matches_geometry():
    assert len(folds) == 2          # 160 sesi, train 100, test/step 25

def test_walk_forward_reports_zero_folds_when_sessions_too_short():
    report = walk_forward(_feats_from_prices(_synthetic_prices(80, 25), HORIZONS), train=100, test=25, step=25)
    assert report["by_horizon"]["21"]["folds"] == 0
    assert report["by_horizon"]["21"]["note"]

def test_walk_forward_train_dates_exclude_test_dates():
    report = walk_forward(_feats_from_prices(_synthetic_prices(160, 25), HORIZONS), train=100, test=25, step=25)
    for fold in report["by_horizon"]["21"]["fold_detail"]:
        train_dates = set(fold["train_dates"])
        test_start, test_end = fold["test"]
        assert not [d for d in train_dates if test_start <= d <= test_end]
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v`
Expected: FAIL/ERROR — `tests/test_score_eval.py` belum ada isinya yang benar (import error atau assertion).

- [ ] **Step 3: Rapikan helper sampai tes bisa lulus**

Hanya helper dan perakitan frame yang perlu ditulis; `walk_forward` sudah ada dan **tidak boleh diubah** di tugas ini. Kalau sebuah tes tidak bisa lulus tanpa mengubah `score_eval.py`, itu temuan: laporkan, jangan tambal.

- [ ] **Step 4: Jalankan sampai lulus**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v`
Expected: PASS untuk kelima tes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_score_eval.py
git commit -m "test: pin geometri lipatan walk-forward (tidak ada kebocoran train ke test)"
```

---

### Task 2: Gerbang edge — komponen hanya dapat bobot kalau buktinya lolos

**Files:**
- Modify: `tests/test_score_eval.py`
- Read (jangan ubah): `score_eval.edge_table`, `score_eval.weights_from_edges`

**Interfaces:**
- Consumes: `_synthetic_prices`, `_feats_from_prices` dari Task 1
- Produces: tidak ada yang dipakai tugas lain

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_edge_table_skips_sessions_below_min_names():
    feats = _feats_from_prices(_synthetic_prices(40, 5), HORIZONS)     # 5 < MIN_NAMES_PER_DAY
    table = edge_table(feats, 21)
    assert (table["days"] == 0).all()
    assert table["edge"].isna().all()

def test_edge_table_counts_session_with_single_flagged_name():
    """Satu nama tertandai per sesi sudah cukup jadi satu observasi edge."""
    prices = _synthetic_prices(60, 25)
    feats = _feats_from_prices(prices, HORIZONS)
    name = next(iter(COMPONENT_NOTES))
    feats[name] = 0.0
    flagged_rows = feats.groupby("date").head(1).index   # tepat satu baris per tanggal
    feats.loc[flagged_rows, name] = 1.0
    table = edge_table(feats, 21)
    assert int(table.loc[name, "days"]) == 38        # 60 sesi − (21 + nlags 1) sesi tanpa label
    assert int(table.loc[name, "flagged"]) == 38

def test_weights_zero_when_t_below_gate():
    edges = pd.DataFrame([{"edge": 0.05, "t_stat": 1.0, "days": 40}], index=["c"])
    assert weights_from_edges(edges) == {"c": 0.0}

def test_weights_zero_when_edge_below_min_edge():
    edges = pd.DataFrame([{"edge": 0.0005, "t_stat": 5.0, "days": 40}], index=["c"])
    assert weights_from_edges(edges) == {"c": 0.0}

def test_weights_zero_when_days_below_min_days():
    edges = pd.DataFrame([{"edge": 0.05, "t_stat": 5.0, "days": 5}], index=["c"])
    assert weights_from_edges(edges) == {"c": 0.0}

def test_weights_clamped_shrunk_and_rounded_to_half_point():
    edges = pd.DataFrame([{"edge": 0.05, "t_stat": 5.0, "days": 40}], index=["c"])
    assert weights_from_edges(edges) == {"c": 7.5}    # clip(0.05*600, 15) = 15; 15*0.5 = 7.5

def test_weights_nan_edge_is_zero_not_crash():
    edges = pd.DataFrame([{"edge": float("nan"), "t_stat": 5.0, "days": 40}], index=["c"])
    assert weights_from_edges(edges) == {"c": 0.0}

def test_edge_table_without_feature_columns_does_not_silently_report_zero_edge():
    """Review Focus #1: kolom fitur hilang harus ketahuan, bukan jadi tabel kosong yang terlihat sah."""
    feats = _feats_from_prices(_synthetic_prices(60, 25), HORIZONS)
    bare = feats[["code", "date", "close", "setup", "abn_21"]]
    with pytest.raises(KeyError):
        edge_table(bare, 21)
```

`test_edge_table_counts_session_with_single_flagged_name` memakai komponen nyata: `next(iter(COMPONENT_NOTES))`.

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v -k "edge or weight"`
Expected: FAIL pada tes yang belum bisa dipenuhi (mis. `KeyError` tidak terlempar).

- [ ] **Step 3: Sesuaikan tes dengan perilaku nyata, atau laporkan temuan**

`edge_table` dan `weights_from_edges` tidak boleh diubah di tugas ini. Kalau salah satu tes gagal karena kode berperilaku lain, **jangan tambal kodenya**: tulis tes yang mengunci perilaku nyata itu dan catat sebagai temuan di ringkasan tugas (mis. "kolom hilang menghasilkan tabel kosong, bukan KeyError"). Temuan itu dipakai di Task 3 untuk memutuskan apakah perlu perbaikan.

- [ ] **Step 4: Jalankan sampai lulus**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v`
Expected: PASS seluruh berkas.

- [ ] **Step 5: Commit**

```bash
git add tests/test_score_eval.py
git commit -m "test: pin gerbang edge (t, edge, jumlah sesi, pangkas, shrink, pembulatan)"
```

---

### Task 3: Harness membuktikan dirinya — edge yang ditanam dikenali, derau ditolak

**Files:**
- Modify: `tests/test_score_eval.py`
- Read: `score_eval.walk_forward`, `score_eval.stable_weights`

**Interfaces:**
- Consumes: helper dari Task 1
- Produces: tidak ada yang dipakai tugas lain

- [ ] **Step 1: Tulis tes yang gagal**

Dua tes, keduanya deterministik (tanpa angka acak, supaya tidak flaky):

```python
def _feats_with_deterministic_abnormal(horizons=HORIZONS) -> pd.DataFrame:
    """160 sesi × 25 kode. Kode berindeks GENAP ditanam underperform −0,2%/sesi
    relatif pasar, kode ganjil +0,2%/sesi — tanpa angka acak, jadi tidak flaky."""
    ...

def test_planted_component_get_negative_weight():
    """Komponen yang menandai kode-kode yang underperform harus dapat bobot negatif.
    Kalau tandanya terbalik, harness salah baca arah edge."""
    feats = _feats_with_deterministic_abnormal()
    name = next(iter(COMPONENT_NOTES))
    feats[name] = 0.0
    feats.loc[feats["code"].str[-1].astype(int) % 2 == 0, name] = 1.0
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    assert report["by_horizon"]["21"]["weights_all"][name] < 0

def test_pure_noise_gets_zero_weight():
    """Semua bendera nol: tidak ada edge, tidak ada komponen yang boleh dapat bobot."""
    feats = _feats_with_deterministic_abnormal()
    for name in COMPONENT_NOTES:
        feats[name] = 0.0
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    assert report["by_horizon"]["21"]["weights"] == {}

def test_verdict_records_reason_for_every_component():
    feats = _feats_with_deterministic_abnormal()
    report = walk_forward(feats, horizons=(21,), train=100, test=25, step=25)
    for name, v in report["by_horizon"]["21"]["verdict"].items():
        assert v["status"] in {"dipakai", "dimatikan"}
        assert v["status"] == "dipakai" or v.get("reason")
```

Cara menanam edge tanpa angka acak: `_feats_with_deterministic_abnormal` menyusun panel harga dari `_synthetic_prices`, lalu menimpa return kode genap/ganjil dengan selisih konstan ±0,2% per sesi relatif pasar sebelum dirakit lewat `_feats_from_prices`. Selisih 21 sesi ≈ ±4% — jauh di atas gate `MIN_EDGE = 0.15%`, jadi tanda bobot mengikuti tanda edge (`bobot = edge x 600` dipangkas `±MAX_COMPONENT_POINTS` lalu dikali `SHRINK`).

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v -k "planted or noise or reason"`
Expected: FAIL pada setidaknya satu tes (harness belum dikalibrasi untuk data uji ini).

- [ ] **Step 3: Kalibrasi data uji, bukan kodenya**

Kalau `test_planted_component_get_negative_weight` gagal, besarkan selisih return yang ditanam dan/atau panjangkan sesi sampai gate `|t| ≥ 1.5` dan `|edge| ≥ 0.15%` benar-benar terlewati. Kalau `test_pure_noise_gets_zero_weight` gagal, periksa apakah bendera benar-benar nol di seluruh baris. **`score_eval.py` tidak diubah di tugas ini.** Kalau setelah dikalibrasi harness tetap memberi bobot pada derau, itu temuan serius: hentikan tugas, tulis temuannya, jangan lanjut.

- [ ] **Step 4: Jalankan sampai lulus**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_score_eval.py -v`
Expected: PASS seluruh berkas, dan `ruff check src tests scripts` tetap bersih.

- [ ] **Step 5: Commit**

```bash
git add tests/test_score_eval.py
git commit -m "test: harness mengenali edge yang ditanam dan menolak derau"
```

---

### Task 4: Satu definisi t-stat untuk hasil klaim

**Files:**
- Modify: `src/idx_scraper/research/recommendation_log.py` (`_summarize`, t_stat)
- Modify: `tests/test_recommendation_log.py`, `tests/test_signal_log.py`
- Read: `signal_log._summarize` (pembanding)

**Interfaces:**
- Consumes: `events._nw_tstat`
- Produces: aturan tetap — **`t_stat` untuk hasil kandidat/klaim = Newey-West atas `abn_{horizon}`**, sama seperti `signal_log._summarize`

**Latar (temuan pemeriksaan):** `signal_log._summarize` memakai `abn_{k}`, sedangkan `recommendation_log._summarize` memakai `fwd_{k}`. Dua angka yang sama-sama tampil sebagai "t" dihitung dari dua hal berbeda. Spec menetapkan klaim dinilai **relatif pasar**, jadi dasar yang benar adalah `abn`.

- [ ] **Step 1: Tulis tes yang gagal**

Di `tests/test_recommendation_log.py` (import `from idx_scraper.research.events import _nw_tstat` dan `_round_opt` dari modul yang diuji):

```python
def test_summarize_t_stat_uses_abnormal_not_forward():
    sub = pd.DataFrame({
        "fwd_21": [0.50, 0.52, 0.51, 0.53, 0.49, 0.51],   # drift besar -> t besar kalau dipakai
        "abn_21": [0.001, -0.002, 0.003, -0.001, 0.002, -0.003],
    })
    out = _summarize(sub, "A", 21)
    assert out["t_stat"] == _round_opt(_nw_tstat(sub["abn_21"]))
```

Di `tests/test_signal_log.py` tambahkan pengunci arah sebaliknya:

```python
def test_summarize_t_stat_uses_abnormal():
    sub = _outcome_frame(abn_21=[0.001, -0.002, 0.003])      # frame kecil buatan tes
    assert _summarize(sub, "BUY", None, 21)["t_stat"] == _round_opt(_nw_tstat(pd.Series([0.001, -0.002, 0.003])))
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_recommendation_log.py -v -k t_stat`
Expected: FAIL — t_stat sekarang dihitung dari `fwd_21`.

- [ ] **Step 3: Ubah satu baris di `_summarize`**

Ganti input perhitungan `t_stat` di `recommendation_log._summarize` dari `fwd_{horizon}` menjadi `abn_{horizon}` (mengikuti `signal_log._summarize`). Tidak ada perubahan lain.

- [ ] **Step 4: Jalankan seluruh tes yang terdampak**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_recommendation_log.py tests/test_signal_log.py -v`
Expected: PASS. Catat di ringkasan tugas: angka `t` yang tampil di `/api/recommendations/track` **akan berubah** — itu memang tujuannya, dan API perlu di-restart memakai jalur terjaga (`_restart-api.flag` + `Start-ScheduledTask -TaskName IDX-API`).

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/research/recommendation_log.py tests/test_recommendation_log.py tests/test_signal_log.py
git commit -m "fix: t-stat hasil kandidat dihitung dari abnormal vs pasar, bukan forward mentah"
```

---

### Task 5: Ukur hasil satu klaim

**Files:**
- Create: `src/idx_scraper/research/claim_eval.py`
- Create: `tests/test_claim_eval.py`
- Read: `signal_log.attach_outcomes` (dipakai ulang, jangan disalin)

**Interfaces:**
- Consumes: `signal_log.attach_outcomes(panel, horizons, nlags) -> pd.DataFrame` (butuh kolom `code,date,close`); `signal_log.DEFAULT_HORIZONS`, `signal_log.NLAGS_DEFAULT`
- Produces (dipakai Task 6 dan 7):

```python
def claim_outcomes(
    claims: pd.DataFrame,
    *,
    raw: pd.DataFrame,      # code,date,high,low,close  -> untuk sentuhan level (harga nyata)
    adj: pd.DataFrame,      # code,date,close           -> untuk abnormal (harga terkoreksi)
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    nlags: int = NLAGS_DEFAULT,
) -> pd.DataFrame
```
Kolom `claims` yang dibutuhkan: `code, date, direction, stop, target, horizon_days`. Kolom hasil: seluruh kolom `claims` + `abn_{k}` per horizon + `hit_stop: bool`, `hit_target: bool`, `touch_date: str | None`, `outcome: str` ∈ {`"stop"`, `"target"`, `"timeout"`, `"unresolved"`}.

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_stop_wins_when_both_touched_in_same_session()
def test_target_hit_before_stop_is_target()
def test_stop_hit_before_target_is_stop()
def test_unresolved_when_neither_touched_within_horizon()
def test_abnormal_uses_adjusted_panel_while_levels_use_raw_panel()   # panel raw & adj berbeda -> abn ikut adj, sentuhan ikut raw
def test_missing_code_in_panel_is_unresolved_not_zero()              # Review Focus #4: baris hilang -> unresolved, bukan abn 0
def test_sell_direction_raises_value_error()                        # Review Focus #5
def test_timeout_when_horizon_passes_without_touch()
```

Bentuk tiap tes: bangun `claims` satu baris, `raw`/`adj` beberapa sesi kecil dengan harga yang ditulis tangan (mis. entry 100, target 110, stop 95), lalu periksa `outcome`, `touch_date`, `hit_target`, `hit_stop`, dan `abn_2` bila horizon 2.

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_claim_eval.py -v`
Expected: FAIL — `ModuleNotFoundError: idx_scraper.research.claim_eval`.

- [ ] **Step 3: Implementasi `claim_outcomes`**

Aturan yang dikunci: arah selain `"BUY"` melempar `ValueError`; sentuhan dibaca dari `raw` (high ≥ target untuk target, low ≤ stop untuk stop, mulai sesi `T+1`); sesi yang menyentuh keduanya dihitung `stop`; setelah `horizon_days` sesi tanpa sentuhan hasilnya `timeout`; tanpa baris harga yang cukup hasilnya `unresolved`. `abn_{k}` ditempel dari `attach_outcomes(adj, ...)` dengan penggabungan `how="left"` pada `(code, date)`.

- [ ] **Step 4: Jalankan sampai lulus**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_claim_eval.py -v`
Expected: PASS kedelapan tes.

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/research/claim_eval.py tests/test_claim_eval.py
git commit -m "feat: ukur hasil satu klaim (sentuhan level, asumsi terburuk, abnormal terkoreksi)"
```

---

### Task 6: Agregat per sesi, bukan per klaim

**Files:**
- Modify: `src/idx_scraper/research/claim_eval.py`
- Modify: `tests/test_claim_eval.py`

**Interfaces:**
- Consumes: `claim_outcomes` dari Task 5
- Produces (dipakai Task 7):

```python
def session_aggregate(outcomes: pd.DataFrame, *, horizon: int) -> dict[str, Any]
```
Kunci hasil: `claims: int`, `sessions: int`, `mean_abnormal: float | None`, `median_session_abnormal: float | None`, `session_dispersion: float | None` (std ddof=1, `None` bila sesi < 2), `hit_target: int`, `hit_stop: int`, `timeout: int`, `unresolved: int`, `per_session: list[dict]` berisi `{"date": "YYYY-MM-DD", "n": int, "mean_abnormal": float | None}`.

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_three_claims_in_one_session_count_as_one_session()
def test_mean_is_of_session_means_not_row_mean()      # 2 klaim +1% di satu sesi, 1 klaim -3% di sesi lain -> mean sesi = (-1.0)% bukan (-0.33)%
def test_dispersion_none_for_single_session()
def test_unresolved_excluded_from_abnormal_but_still_counted()
def test_per_session_is_sorted_by_date()
def test_empty_claims_is_safe()                        # claims=0, sessions=0, semua angka None, tidak melempar
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_claim_eval.py -v -k aggregate or session or dispersion`
Expected: FAIL — fungsi belum ada.

- [ ] **Step 3: Implementasi `session_aggregate`**

Rata-rata dilakukan dua kali: dulu lintas klaim di dalam satu tanggal, lalu lintas tanggal. Klaim `unresolved` (abn `NaN`) tidak masuk rata-rata mana pun, tapi tetap dihitung di `unresolved`.

- [ ] **Step 4: Jalankan sampai lulus**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_claim_eval.py -v`
Expected: PASS seluruh berkas.

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/research/claim_eval.py tests/test_claim_eval.py
git commit -m "feat: agregat hasil klaim per sesi (bukan per klaim)"
```

---

### Task 7: Verdict terbukti / tidak terbukti / belum cukup, dan baris rekamannya

**Files:**
- Modify: `src/idx_scraper/research/claim_eval.py`
- Modify: `tests/test_claim_eval.py`

**Interfaces:**
- Consumes: `session_aggregate` dari Task 6
- Produces:

```python
MIN_CLAIMS = 30          # spec §8 Q4
MIN_SESSIONS = 30        # 6 minggu bursa (spec §8 Q4)
VERDICT_HORIZON = 21     # satu-satunya horizon dengan bukti OOS (spec F1)

def claim_verdict(agg: dict[str, Any], *, min_claims: int = MIN_CLAIMS, min_sessions: int = MIN_SESSIONS) -> dict[str, Any]
def format_record_line(agg: dict[str, Any]) -> str
```

`claim_verdict` mengembalikan `{"status": "belum cukup" | "terbukti" | "tidak terbukti", "reason": str, "claims": int, "sessions": int, "mean_abnormal": float | None}`.
`format_record_line` mengembalikan kalimat yang dibaca pengguna, contoh persis: `Aturan ini: 12 klaim · 5 kena target · 4 kena batas rugi · rata-rata +1,4% vs pasar`. Bila `claims == 0`: `Aturan ini: belum punya rekaman`.

- [ ] **Step 1: Tulis tes yang gagal**

```python
def test_verdict_not_ready_below_both_thresholds()       # 29 klaim, 29 sesi -> "belum cukup"
def test_verdict_ready_at_thirty_claims_even_in_few_sessions()   # 30 klaim, 8 sesi -> siap (spec §8 Q4, R7)
def test_verdict_ready_at_thirty_sessions_even_with_few_claims() # 12 klaim, 30 sesi -> siap
def test_verdict_not_proven_when_mean_not_above_zero()
def test_verdict_proven_when_mean_above_zero()
def test_verdict_reason_states_which_threshold_was_reached()
def test_record_line_exact_copy()      # bandingkan string penuh, termasuk pemisah " · " dan tanda + pada persen
def test_record_line_when_no_claims()  # -> "Aturan ini: belum punya rekaman"
def test_record_line_negative_average_has_no_plus()   # rata-rata -0,8% -> "-0,8%", bukan "+-0,8%"
```

- [ ] **Step 2: Jalankan dan pastikan gagal**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_claim_eval.py -v -k "verdict or record"`
Expected: FAIL — fungsi belum ada.

- [ ] **Step 3: Implementasi keduanya**

Kunci perilaku: "siap" bila `claims >= min_claims` **atau** `sessions >= min_sessions` (mana lebih dulu); "terbukti" hanya bila siap **dan** `mean_abnormal > 0`; `reason` menyebut ambang mana yang tercapai dan berapa sesi yang terhitung. Rata-rata negatif ditulis dengan tanda minus, positif dengan `+`.

- [ ] **Step 4: Jalankan seluruh rangkaian tes**

Run: `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q` dan `.venv\Scripts\python.exe ruff check src tests scripts`
Expected: seluruh tes repo PASS (sebelumnya 716), `ruff` bersih. Jalankan `ruff format` hanya pada berkas yang dibuat/diubah tugas ini.

- [ ] **Step 5: Commit**

```bash
git add src/idx_scraper/research/claim_eval.py tests/test_claim_eval.py
git commit -m "feat: verdict klaim (30 klaim atau 30 sesi) dan baris rekaman untuk layar"
```

---

## Catatan penutup untuk pelaksana

- Tiga tugas pertama **tidak mengubah** `score_eval.py`. Kalau sebuah tes hanya bisa lulus dengan mengubahnya, itu temuan, bukan izin.
- Task 4 mengubah satu angka yang sudah tampil di dashboard. Itu disengaja dan disebutkan di spec (§6.1). Setelah merge, API harus di-restart lewat jalur terjaga — bukan `Stop-Process`.
- Modul baru `claim_eval.py` sengaja tidak menyentuh DB. Tabel jurnal dan job penjadwalnya masuk **P6**, bukan di sini; P3 hanya menyediakan pengukuran dan verdictnya.
- Setelah seluruh tugas lulus: perbarui baris status P3 di `RENCANA-P0-P7.md`, lalu minta review seluruh branch sebelum merge ke `main`.
