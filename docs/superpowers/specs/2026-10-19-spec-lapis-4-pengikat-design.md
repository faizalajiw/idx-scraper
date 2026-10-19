# Spec Desain: Lapis 4 (Pengikat & Pengingat) P6

- **Tanggal:** 19 Okt 2026
- **Status:** draf siap dikerjakan, ditulis atas permintaan pemilik keputusan ("langsung mulai saja", 19 Okt 2026), jadi tanpa tanya-jawab. Setiap keputusan yang menambah atau menafsirkan aturan lama ditandai **call penulis** di bagian 4b dan boleh diveto sebelum plan dikerjakan.
- **Revisi:** 1 (19 Okt 2026)
- **Pemilik keputusan:** pemilik proyek (pengguna dashboard)
- **Penulis:** Lucif
- **Cakupan:** lapis 4 di `idx-scraper` (buku klaim, aturan pre-commit, kill-switch, pengingat) + permukaan di `idx-web` tanpa menu baru
- **Bergantung pada:** spec mesin keputusan P2 (`docs/superpowers/specs/2026-10-18-spec-mesin-keputusan-design.md`), khususnya D5, D6, D8, 6.1, 6.2, 6.4, Q1, Q3, Q4, R6, R8; hasil P3 (`research/claim_eval.py`), P4 (gerbang rezim + penahanan), P5 (`research/position.py`, plafon portofolio)
- **Dokumen ini menggantikan:** tidak ada. Lapis 4 belum pernah punya dokumen; yang ada hanya butir-butirnya di P2 6.2 dan satu baris di rencana induk.

---

## 1. Kenapa dokumen ini ada

Cerita yang melahirkan proyek ini ada di P2 §1: satu posisi ditahan tanpa rencana, lalu *average down*, lalu dijual di harga rata-rata, bukan di harga rencana. Kerugiannya bukan dari analisis, tapi dari tidak adanya rencana sebelum uang bergerak.

P4 memasang gerbang yang bisa menolak. P5 memasang ukuran dan plafonnya. Keduanya membuat tawaran datang dengan besar yang bisa dipertanggungjawabkan. Yang belum ada satu pun: tempat **klaim itu disimpan sebagai keputusan yang beku**, cara sistem **menolak klaim yang tidak lengkap**, dan cara aturan itu **menagih diri sendiri** saat dilewati.

Tiga hal yang belum ada, semuanya sudah diputuskan di P2 tapi belum dibangun:

1. **Buku bukti (P2 D5).** Satu baris per klaim, ditulis sekali sebelum hasilnya keluar, idempoten, tidak boleh ditimpa. Sekarang tidak ada tabelnya (F1), jadi tidak ada satu pun klaim yang bisa ditagih.
2. **Aturan pre-commit (P2 6.2).** Rencana keluar dan ukuran posisi wajib ada **sebelum** baris ditulis; klaim tanpa stop dan target **ditolak sistem**, bukan diberi peringatan.
3. **Pengingat (P2 6.2).** Kanal Telegram sudah hidup untuk `idx health` (F11), tapi tidak ada yang mengabarkan klaim mana yang menyentuh batasnya.

Yang **tidak** ada di P2 dan harus diputuskan di sini: **kill-switch**. Ia disebut di kerangka 4 lapis rencana induk (`RENCANA-P0-P7.md:44`) dan di judul plan P6, tapi tidak punya aturan. Bagian 5.6 mengisinya, dengan pilihan yang sengaja sempit (F16).

Satu akibat yang harus ditulis di depan: **P6 tidak menutup celah cerita BBRI.** P2 Q3 memutuskan jurnal hanya mencatat klaim mesin, jadi posisi nyata yang kamu beli tidak masuk buku mana pun (F15, D12). Yang bisa dilakukan lapis 4 adalah menagih aturan dan mengabarkan pelanggaran level, bukan melarang transaksi yang tidak dilihatnya.

---

## 2. Fakta hasil pemeriksaan (bukan asumsi)

Diperiksa dari kode kedua repo, 19 Okt 2026.

| # | Temuan | Bukti |
|---|---|---|
| F1 | **Lapis 4 masih nol.** Tidak ada tabel jurnal, posisi, pre-commit, atau portofolio di `sql/` mana pun. Grep `journal\|kill_switch\|killswitch\|halt\|pre-commit` di `*.py` hanya menemukan `PRAGMA journal_mode=WAL` milik SQLite. | grep seluruh repo, `src/idx_scraper/storage.py:122` |
| F2 | Yang ada hanya catatan mesin, bukan catatan keputusan: `research.signal_log` (BUY/SELL per kode-sesi) dan `research.recommendation_log` (kandidat beli). Keduanya upsert idempoten. | `sql/signal_log.sql:19-32`, `sql/research_schema.sql:261-280`, `src/idx_scraper/research/recommendation_log.py:60-100` |
| F3 | DDL ditulis dua kali: berkas `sql/*.sql` untuk pemasangan manual, dan konstanta `_DDL` + `ensure_table()` di Python untuk runtime. Berkas sql menyebutkan pasangannya di Python. | `sql/market_segment_daily.sql:7-9`, `src/idx_scraper/research/recommendation_log.py:60` |
| F4 | Berkas `sql/` dipasang manual lewat `psql -f` berurutan, terdokumentasi; tidak ada loader Python yang membaca `sql/*.sql`. | `DEPLOY-LINUX.md:194-201` |
| F5 | **Harness penilaian klaim dan kalimat rekamannya sudah ada dan belum dipakai siapa pun.** `claim_outcomes` (baris 85), `session_aggregate` (171), `claim_verdict` (239) sudah menerapkan dua ukuran hasil P2 6.1 (abnormal horizon tetap + sentuhan level, stop menang saat satu sesi menyentuh keduanya); ambangnya `MIN_CLAIMS = 30`, `MIN_SESSIONS = 30`, `VERDICT_HORIZON = 21` (234-236). `format_record_line` (316) sudah menyusun kalimat "Aturan ini: N klaim · X kena target · Y kena batas rugi · rata-rata +Z% vs pasar" dan mengembalikan "Aturan ini: belum punya rekaman" saat buku kosong, dengan kunci masukan `claims`, `hit_target`, `hit_stop`, `mean_abnormal`. Pemanggil di `src/`: tidak ada, hanya tes. | `src/idx_scraper/research/claim_eval.py:85-334`, `tests/test_claim_eval.py:1-10` |
| F6 | Produsen keadaan yang dibutuhkan gerbang klaim sudah ada: gerbang rezim dan penahanan panel. | `src/idx_scraper/research/regime.py:170-213`, `src/idx_scraper/api/analytics.py:2786-2811`, `src/idx_scraper/freshness.py:63-155` |
| F7 | Rencana posisi sudah fungsi murni dan **sudah menyebut jurnal P6 sebagai konsumen berikutnya** di header modulnya. | `src/idx_scraper/research/position.py:1-10`, `:41-130` |
| F8 | Tidak ada test runner di repo web. Satu-satunya gerbang: `npm ci` lalu `npx tsc --noEmit` lalu `next build`; bukti render diambil manual. | `idx-web/package.json:5`, `idx-web/README.md:66` |
| F9 | Menu didaftarkan di dua tempat (`Sidebar.GROUPS` dan `CommandPalette.NAV_ACTIONS`) dan P2 R6 melarang menumpuk menu. Maka permukaan baru harus menempel di halaman yang sudah ada. | `idx-web/components/Sidebar.tsx:31`, `idx-web/components/CommandPalette.tsx:21` |
| F10 | Jalur tulis dari web ke server sudah punya pola: berkas JSON dengan tulis atomik (tmp + replace), path bisa ditimpa env untuk tes, plus `data/alerts.json` sebagai contoh yang hidup. | `src/idx_scraper/alerts_store.py:8`, `:23-56` |
| F11 | Pengingat: satu kelas pengirim dengan `send_message(text) -> bool`, formatter murni terpisah per jenis alarm, dan job EOD yang mengirim Telegram per temuan. Ada juga pola penjaga "bisu" kalau tidak ada yang diperiksa. | `src/idx_scraper/notify.py:41-99`, `:102-203`, `src/idx_scraper/cli.py:280-313`, `:316-408` |
| F12 | Penjadwalan job ada di `cmd_serve` (apscheduler) dan jamnya dikunci tes. Menambah job berarti menambah id cron + mengunci jamnya di tes itu. | `src/idx_scraper/cli.py:1685-1811`, `tests/test_serve_schedule.py:129-146` |
| F13 | Tes menyentuh lapisan DB disimulasikan (fake cursor + `monkeypatch`), tanpa `conftest.py`, tanpa Postgres di tes. | `tests/test_position_plan_api.py:1-13`, `tests/test_corp_action_filter.py:91-114` |
| F14 | P2 sudah mengunci isi lapis 4 dan batasnya: buku bukti satu baris per klaim (D5), pre-commit menolak bukan memperingatkan (6.2), pengingat lewat Telegram (6.2), jurnal klaim mesin saja (Q3), dan celah average down ditutup lewat aturan pre-commit + pengingat, bukan lewat jurnal (R8). | spec P2 D5, 6.1, 6.2, Q3, R8 |
| F15 | P5 menjanjikan hal yang bertabrakan dengan Q3: "keadaan portofolio diketik pemilik... P6 yang menggantinya dengan catatan sebenarnya", dan mencatat risiko angka basi sebagai "ditutup P6 saat posisi benar-benar dicatat". | spec P5 D12, R2, R8 |
| F16 | **Kill-switch belum punya aturan di dokumen mana pun.** Yang ada hanya namanya di kerangka 4 lapis dan satu aturan gagal di P2 6.1: kalau pada 30 klaim atau 6 minggu rata-rata hasil tidak melewati nol, mesin "berhenti mengeluarkan perintah". | `RENCANA-P0-P7.md:44`, spec P2 6.1 |
| F17 | Konvensi harga sudah dikunci: level yang ditransaksikan manusia pakai harga mentah, hasil diukur pada harga terkoreksi, dan satu klaim menyimpan keduanya. | spec P2 6.4, 6.1 |
| F18 | Patokan "hasil relatif pasar" sudah punya satu tempat di kode: hasil rekomendasi dinilai sebagai abnormal terhadap pasar berimbang, bukan return mentah. | spec P4 (baris tabel status P4), `src/idx_scraper/api/analytics.py` (`recommendations/track`) |

---

## 3. Tujuan, non-tujuan, anti-tujuan

**Tujuan**

- Setiap klaim yang lahir dari papan masuk buku **sebelum** hasilnya keluar, sekali saja, dan tidak bisa ditimpa (P2 D5).
- Klaim tanpa rencana keluar lengkap dan ukuran posisi **ditolak sistem** dengan sebab yang bisa dibaca, bukan cuma diberi peringatan (P2 6.2).
- Buku klaim bisa menjawab pertanyaannya sendiri dalam empat angka: berapa klaim, berapa kena target, berapa kena batas rugi, rata-rata hasil relatif pasar (P2 D5). Sebelum ada rekaman, dikatakan apa adanya: "aturan ini belum punya rekaman" (P2 D6).
- Hasil diisi belakangan apa adanya, memakai harness P3 yang sudah ada, dengan dua ukuran dan asumsi terburuk saat satu sesi menyentuh target dan stop (P2 6.1).
- Ada **gerbang klaim** yang bisa menolak menulis klaim baru, dengan empat sebab bernama, dan penolakan itu tercatat sehingga hari tanpa klaim selalu bisa dijelaskan (bukan hilang diam-diam).
- Pemilik bisa menghentikan mesin dengan satu tombol, dan penghentian itu dikabarkan sekali lewat kanal yang sudah hidup (P2 6.2, F11).
- Setiap kalimat perintah di layar membawa rekamannya sendiri (P2 D6).

**Non-tujuan**

- **Tidak mencatat posisi nyata yang kamu beli** (P2 Q3, D12). Tidak ada lot yang kamu isi, tidak ada harga fill, tidak ada laba-rugi uang nyata.
- Tidak menyambung ke broker, tidak mengirim order, tidak ada auto-trading. Eksekusi tetap manual.
- Tidak menambah menu di web (P2 R6), tidak mengganti halaman yang tidak perlu.
- Tidak menambah model, indikator, atau sumber data baru (P2 D7).
- Tidak mengubah perilaku P1 sampai P5: papan, penahanan, rencana posisi, dan angka baseline sesi 2026-10-16 tetap apa adanya.

**Anti-tujuan** (kalau salah satu ini terjadi, pekerjaannya gagal meski kodenya jalan)

- Klaim yang tercatat hanya pada hari-hari yang menarik, lalu buku itu disebut bukti. Karena itu ada penolakan yang tercatat dan ada penjaga bisu.
- Klaim yang ditulis ulang dengan angka baru di hari yang sama (menimpa keputusan lama) sehingga rekamannya jadi karangan belakangan.
- Klaim yang lolos tanpa stop, tanpa target, atau tanpa ukuran posisi.
- Angka hasil yang dihitung pada harga mentah, atau level yang ditampilkan dari harga terkoreksi (langgar P2 6.4).
- Hari tanpa klaim yang tidak bisa dijelaskan sebabnya.
- Kill-switch yang menghapus catatan: gerbang menolak menulis klaim baru, bukan menyembunyikan klaim lama.
- Tombol halt yang bisa menyala sendiri tanpa jejak siapa dan kapan.

---

## 4. Keputusan yang dikunci

| # | Keputusan | Sumber |
|---|---|---|
| D1 | **Buku klaim adalah tabel baru** `research.claim_journal`, satu baris per klaim, kunci (`code`, `trade_date`, `rule`), ditulis sekali dengan `on conflict do nothing`. Rekam ulang tidak menimpa klaim lama. | P2 D5, 6.2 |
| D2 | **Satu klaim menyimpan**: kode, tanggal sesi, arah, aturan yang melahirkan, harga referensi, stop, target (ketiganya harga **mentah**), horizon, ukuran posisi (persen, lot, rupiah; kosong bila modal tidak ada), **harga referensi terkoreksi**, dan keadaan saat klaim lahir (rezim, `vol_state`, kesegaran). | P2 D5, 6.1, 6.4, F17 |
| D3 | **Pre-commit adalah syarat, bukan peringatan.** Klaim ditolak bila tidak lengkap: tanpa stop, tanpa target, tanpa ukuran posisi, atau geometri yang tidak masuk akal (stop di atas entry, target di bawah entry, entry bukan angka positif). | P2 6.2 |
| D4 | **Klaim ditulis oleh job terjadwal di akhir sesi**, bukan saat dashboard dibuka, untuk nama yang benar-benar tampil di daftar pendek (3 sampai 5 nama per sesi, lebih sedikit bila kolamnya tipis). | P2 D8, 6.1, Q1 |
| D5 | **Gerbang klaim punya empat sebab**, semuanya memblokir **penulisan klaim baru** dan tidak menyentuh catatan lama: data tidak segar; gerbang rezim menolak; verdict buku "tidak terbukti"; halt manual pemilik. Urutan penyebutan sebab: manual, data, rezim, verdict. | P2 D1, 6.1, 6.2, F16 |
| D6 | **Verdict "tidak terbukti" menghentikan mesin**, sesuai P2 6.1: pada 30 klaim atau 6 minggu, mana yang lebih dulu, bila rata-rata hasil relatif pasar tidak melewati nol maka klaim baru berhenti ditulis. Yang tersisa hanya alat baca. | P2 6.1, Q4 |
| D7 | **Halt manual disimpan sebagai berkas JSON dengan tulis atomik**, bukan tabel, mengikuti pola `data/alerts.json`; path bisa ditimpa env untuk tes. Isinya: keadaan, sebab, waktu, dan siapa yang menyalakan (nama isian bebas, default "pemilik"). | call penulis, F10 |
| D8 | **Penolakan tercatat.** Setiap sesi tanpa klaim meninggalkan satu baris sebab (`research.claim_refusal`, kunci `trade_date` + sebab): ditolak gerbang, ditolak pre-commit, atau kolam kosong. Buku tanpa baris sebab adalah kegagalan, bukan kebetulan. | call penulis, P2 6.1 (anti-bias) |
| D9 | **Hasil diisi belakangan memakai harness P3** (`claim_outcomes`) dengan `VERDICT_HORIZON = 21`, dua ukuran (abnormal horizon tetap + sentuhan level), stop menang bila satu sesi menyentuh keduanya, dan status klaim: `terbuka`, `kena_target`, `kena_stop`, `kedaluwarsa`. | P2 6.1, F5 |
| D10 | **Patokan pasar memakai definisi yang sudah dipakai `/api/recommendations/track`**, tidak ada definisi kedua. Satu definisi, satu tempat. | call penulis, F18, P2 6.1 |
| D11 | **Empat angka ditampilkan lebih dulu daripada isi buku**: jumlah klaim, kena target, kena batas rugi, rata-rata relatif pasar. Rata-rata dilaporkan **per sesi** ditambah jumlah sesi, supaya sampel tidak terlihat lebih besar daripada kenyataannya. Buku kosong berarti kalimat "belum punya rekaman", bukan nol. Kalimatnya disusun `claim_eval.format_record_line` yang sudah ada (F5), bukan formatter kedua. | P2 D5, D6, 6.1 |
| D12 | **Posisi nyata tidak dicatat** (ikut P2 Q3). Konsekuensinya ditulis terang: P5 R2 dan R8 **tidak ditutup P6**, dan angka yang kamu ketik di strip tetap ditandai sebagai angka yang kamu isi sendiri. Tidak ada janji penggantinya di dokumen ini. | P2 Q3, R8, lawan F15 |
| D13 | **Pengingat Telegram: satu pesan per sesi**, isinya satu hal, klaim mana yang hari itu menyentuh batasnya. Ditambah satu pesan saat halt manual menyala dan satu saat dimatikan. Tidak ada pengingat berulang. | P2 6.2, F11 |
| D14 | **Penjaga bisu.** Kalau job penulis klaim tidak menulis klaim apa pun padahal tidak ada sebab penolakan dan ada kandidat, itu alarm. Mengikuti pola `_lapor_bisu`. | call penulis, F11, D8 |
| D15 | **Tidak menambah menu di web.** Permukaan lapis 4 menempel di halaman yang sudah ada: `/rekomendasi` mendapat baris rekaman per kartu kandidat, blok empat angka di atas papan, dan penanda gerbang klaim + tombol halt. | P2 R6, D6, §7 P2 |
| D16 | **Tidak ada pengingat yang berbunyi tanpa aksi.** Setiap pengingat menyebut klaim atau keadaan yang bisa diperiksa di layar, dengan tautan bagiannya. | call penulis, P2 6.2 |

---

## 4b. Call penulis (keputusan yang saya ambil tanpa bertanya)

Pemilik memilih "langsung mulai saja" pada 19 Okt 2026, jadi tidak ada tanya-jawab. Enam hal di bawah ini saya putuskan sendiri; semuanya bisa diveto sebelum plan jalan, dan tidak ada satu pun yang mengubah aturan P2/P5.

1. **Kill-switch dibatasi pada penulisan klaim (D5).** Pilihan lain yang saya buang: memblokir layar, atau memblokir perintah di UI. Alasannya, blokir layar menghukum pembacaan (P2 D6 menginginkan rekaman bertambah, bukan layar padam), dan blokir di UI saja tidak menyentuh penulis klaim yang berjalan sebagai job. Menolak **menulis klaim** adalah tempat paling awal dan paling murah, dan itu satu-satunya pintu masuk buku.

2. **Halt manual lewat berkas JSON (D7), bukan tabel.** Alasannya, pola tulis atomik sudah ada dan teruji (F10), sedangkan API hari ini tidak menulis ke Postgres sama sekali. Menambah jalur tulis DB dari API berarti menambah satu kelas risiko baru untuk satu tombol.

3. **Penolakan dicatat di tabel terpisah (D8).** Saya menolak menyimpannya di berkas log karena bisu-nya harus bisa dibedakan: "tidak ada klaim karena gerbang menolak" dan "tidak ada klaim karena jobnya mati" punya penanganan berbeda, dan hanya yang kedua boleh jadi alarm (D14).

4. **Posisi nyata tidak dicatat (D12).** Ini tabrakan antara P2 Q3 dan P5 D12/R2/R8 (F15). Yang saya ikuti: Q3, karena ia keputusan pemilik yang menyebut hal ini secara harfiah ("posisi nyata yang dibeli pemilik tidak dicatat"), sedangkan P5 D12 adalah harapan yang ditulis dengan asumsi Q3 bisa dilanggar nanti. Efeknya jujur: celah average down tetap terbuka, dan P5 R2/R8 tetap terbuka. Kalau kamu mau menutupnya, itu keputusan baru di luar P6, dan saya sarankan dibuat sebagai P6b yang berdiri sendiri supaya lapis 4 tidak menunggu.

5. **Patokan pasar yang dipakai adalah definisi di `/api/recommendations/track` (D10).** Saya belum memutuskan apakah angka patokan itu dihitung ulang di modul baru atau dipakai lewat fungsi yang sudah ada; yang saya kunci hanya jumlahnya satu. Bentuk teknisnya diserahkan ke pembangun (bagian 6).

6. **Tidak ada menu baru (D15).** P2 R6 melarang menumpuk menu dan P2 §7 memberi kebebasan merombak halaman yang sudah ada. Halaman yang saya pilih: `/rekomendasi` (sisi masuk, tempat kalimat perintah memang keluar). Halaman `/keputusan` tidak disentuh, karena ia satu-satunya halaman dengan kata kerja sisi keluar dan tidak ada klaim masuk di sana.

---

## 5. Aturan yang diminta

### 5.1 Gerbang klaim (fungsi murni)

```
claim_gate(
  *,
  manual_halt=False, halt_reason=None,      # dari berkas halt
  freshness_status=None,                    # "segar" | "basi" | None (tak diketahui)
  gate_blocked=False, gate_reason=None,     # dari gerbang rezim P4
  verdict=None,                             # "terbukti" | "tidak terbukti" | "belum cukup" | None
)
  -> dict   # {"allowed": bool, "reason": str|None, "message": str}
```

Aturan:

| Keadaan | `allowed` | `reason` | `message` (contoh) |
|---|---|---|---|
| halt manual menyala | tidak | `manual` | "Mesin dihentikan pemilik: {halt_reason}." |
| kesegaran bukan `segar` | tidak | `data` | "Data sesi terakhir belum masuk, klaim baru tidak ditulis." |
| gerbang rezim menolak | tidak | `regime` | "{gate_reason} Klaim baru tidak ditulis sampai kondisinya berubah." |
| verdict `tidak terbukti` | tidak | `verdict` | "Buku klaim tidak lolos ambang: rata-rata hasil belum lewat nol." |
| tidak ada sebab di atas | ya | `None` | "Klaim boleh ditulis." |

Urutan pemeriksaan mengikuti tabel ini (hal pertama yang menolak, itu sebabnya), dan hanya sebab pertama yang dilaporkan supaya pesannya satu hal (P2 6.2, D13).

### 5.2 Bentuk klaim yang sah

Klaim lahir dari baris kandidat yang sudah ada, bukan dari hitungan baru:

```
claim_from_candidate(candidate: dict, *, trade_date: str, rule: str, horizon: int = 21)
  -> dict        # klaim lengkap, atau None
claim_reject_reason(candidate: dict) -> str | None   # sebab penolakan, pola yang sama dengan claim_gate
```

Ditolak bila: `entry`, `stop`, atau `target` bukan angka positif; `stop >= entry`; `target <= entry`; ukuran posisi tidak ada (baik persen maupun lot); horizon bukan bilangan positif. **Tidak ada angka yang dikarang untuk menyelamatkan klaim**: kalau ukurannya kosong, klaimnya ditolak, bukan ditulis dengan nol.

### 5.3 Penulisan buku klaim (idempoten, sekali saja)

```
record_claims(cur, claims: list[dict]) -> int      # insert ... on conflict (code, trade_date, rule) do nothing
record_refusal(cur, *, trade_date: str, reason: str, detail: str | None) -> None   # on conflict do nothing
```

Keluaran `record_claims` adalah jumlah baris yang **benar-benar masuk** (bukan jumlah yang dicoba), supaya job bisa membedakan "tidak ada kandidat" dari "sudah pernah ditulis".

### 5.4 Pengisian hasil

```
fill_outcomes(cur, *, trade_date: str | None = None) -> dict   # {"diperiksa": n, "kena_target": n, "kena_stop": n, "kedaluwarsa": n}
```

Aturan:

- Memakai `claim_eval.claim_outcomes` (P3) apa adanya untuk kedua ukuran; tidak ada rumus hasil kedua (D9).
- Status klaim berpindah `terbuka` menjadi `kena_target`, `kena_stop`, atau `kedaluwarsa` (horizon lewat tanpa sentuhan). Hasil diisi apa adanya, termasuk yang jelek.
- Perpindahan status tidak bisa dibatalkan, dan klaim yang sudah punya status tidak diperiksa ulang.
- Angka disimpan pada kedua dasar: level mentah (D2) dan hasil pada harga terkoreksi (P2 6.4).

### 5.5 Agregat buku

```
journal_summary(cur, *, rule: str | None = None, code: str | None = None) -> dict
```

Keluaran minimal, semuanya boleh kosong (bukan nol):

| Field | Arti |
|---|---|
| `claims` | jumlah klaim tercatat |
| `sessions` | jumlah sesi berbeda yang punya klaim |
| `hit_target`, `hit_stop`, `expired`, `open` | sebaran status |
| `avg_abnormal_pct` | rata-rata hasil relatif pasar, **dihitung per sesi lalu dirata-ratakan** (D11) |
| `per_session` | daftar sesi + rata-rata sesi itu |
| `verdict` | hasil `claim_eval.claim_verdict` bila ambang tercapai, `None` bila belum |

**Nama field mengikuti pemakai yang sudah ada.** `claim_eval.format_record_line` (F5) meminta `claims`, `hit_target`, `hit_stop`, dan `mean_abnormal`, jadi keempat nama itu dipakai apa adanya, dan kalimat rekaman yang dibaca pengguna disusun fungsi itu, bukan formatter kedua.

### 5.6 Kill-switch

Tiga sebab turunan (data, rezim, verdict) dihitung saat dibutuhkan dari sumbernya masing-masing (F6, F5). Satu sebab milik pemilik disimpan:

```
journal_halt.state() -> {"halted": bool, "reason": str|None, "at": str|None, "by": str|None}
journal_halt.set_halt(*, reason: str, by: str = "pemilik") -> dict
journal_halt.clear(*, by: str = "pemilik") -> dict
```

- Sebab wajib diisi saat menyalakan halt; halt tanpa sebab ditolak.
- Penulisan berkas memakai pola atomik `alerts_store` (F10), path bisa ditimpa env.
- Keadaan halt dibaca penulis klaim (job) dan API. Kalau berkas tidak ada, keadaannya "tidak dihentikan" (fail-open), sebab itu ditulis di layar sebagai keadaan kosong.
- Menyalakan/mematikan halt **tidak menghapus** catatan apa pun.

### 5.7 Pengingat

- Satu pesan per sesi berisi klaim yang hari itu menyentuh batasnya (target atau stop), menyebut kode dan aturannya. Formatter adalah fungsi murni terpisah (pola `notify.py`, F11).
- Satu pesan saat halt manual menyala, satu saat dimatikan, masing-masing menyebut sebab dan siapa.
- Kalau tidak ada klaim yang ditulis dan tidak ada baris penolakan, kirim alarm bisu (D14).

### 5.8 Permukaan web (tanpa menu baru)

| Permukaan | Isi | Halaman |
|---|---|---|
| Baris rekaman per kartu | satu baris kecil dari `claim_eval.format_record_line` (F5): "Aturan ini: {claims} klaim · {hit_target} kena target · {hit_stop} kena batas rugi · rata-rata {avg}% vs pasar", atau "Aturan ini: belum punya rekaman" | `/rekomendasi` |
| Blok empat angka | ringkasan buku di atas papan, dengan jumlah sesi dan verdict bila ada | `/rekomendasi` |
| Penanda gerbang klaim | kalau klaim baru ditolak: sebabnya, memakai kosakata yang sudah ada (`withheld`, `gate.reason`, kesegaran) | `/rekomendasi` |
| Tombol halt | nyala/mati dengan sebab wajib, konfirmasi lewat modal yang sudah jadi pola repo, keadaan tampil di layar | `/rekomendasi` |

Aturan tampilan yang mengikat: buku kosong berarti "belum punya rekaman", bukan nol; angka yang tidak bisa dihitung tidak ditampilkan sebagai angka; tidak ada istilah baru kalau istilah lama sudah ada (F9, D11).

---

## 6. Yang diserahkan ke pembangun

- Nama modul, nama kolom, bentuk payload JSON, dan bentuk endpoint.
- Jam cron job penulis klaim dan pengisi hasil (asalkan setelah sesi tutup dan hasil EOD sudah masuk), plus bentuk pelaporan kalau jobnya gagal.
- Apakah patokan pasar dipakai lewat fungsi yang sudah ada atau dihitung ulang di modul baru, asalkan satu definisi (D10).
- Bentuk pengisian `ref_adj` klaim: kolom tersimpan atau kolom turunan saat pembacaan, asalkan hasil diukur pada dasar terkoreksi (P2 6.4).
- Susunan tes: satu tes per baris tabel 5.1, satu tes tiap sebab penolakan 5.2, satu tes idempotensi (rekam dua kali, satu baris), satu tes agregat per sesi, satu tes halt tanpa sebab ditolak, satu tes jam cron baru.
- Bentuk kalimat pesan Telegram dan label layar, selama satu pesan menyebut satu hal (D13).

---

## 7. Jawaban pemilik keputusan

Tidak ada tanya-jawab di dokumen ini. Pemilik memilih "langsung mulai saja" pada 19 Okt 2026, jadi keputusan yang tidak terkurung di P2/P5 ditandai **call penulis** di bagian 4b.

Kalau salah satu call itu tidak kamu setujui, yang perlu kamu katakan cukup nomornya (contoh: "4b nomor 4 saya mau ditutup sekarang"). Tanpa jawaban, plan dikerjakan apa adanya.

---

## 8. Batas dan risiko

| # | Batas / risiko | Sikap |
|---|---|---|
| R1 | **Celah average down tetap terbuka** (P2 Q3, D12). Sistem tidak melihat posisi nyata, jadi ia hanya bisa menagih aturan dan mengabarkan level. | Ditulis di depan, tidak dijanjikan tertutup. Kalau mau ditutup, itu keputusan baru (P6b). |
| R2 | Kelengkapan buku bergantung pada kebenaran papan hari itu (klaim lahir dari baris kandidat). | Buku mencatat keadaan saat klaim lahir (D2), jadi klaim bisa diperiksa terhadap keadaan itu belakangan. |
| R3 | Ambang verdict 30 klaim atau 6 minggu berarti kill-switch jenis verdict baru hidup setelah itu. | Diterima; tiga sebab lain (manual, data, rezim) bekerja sejak hari pertama. |
| R4 | Aggregat per sesi tetap bisa memuat sesi yang saling berkorelasi (3 sampai 5 klaim satu sesi). | D11 melaporkan jumlah sesi berdampingan supaya pembaca bisa menilai sendiri. |
| R5 | Berkas halt bisa lupa dimatikan, dan mesin diam tanpa alarm. | Pesan Telegram saat menyala (D13) dan keadaan halt tampil di layar setiap kali papan dibuka. |
| R6 | Status klaim yang tidak bisa dibatalkan bisa menahan kesalahan pengisian permanen. | Diterima demi P2 D5 (rekam ulang tidak boleh menimpa); sebab dan koreksinya dicatat sebagai klaim baru di sesi berikutnya, bukan penulisan ulang. |
| R7 | Repo web tanpa test runner, jadi bukti permukaan hanya tsc + build + render nyata. | Diterima sebagai standar repo (F8); bukti render wajib ada sebelum P6 disebut selesai. |
| R8 | Pengingat Telegram bisa dianggap berisik kalau klaim sering menyentuh batas. | D13: satu pesan per sesi, tidak ada pengingat berulang. |

---

## 9. Lampiran

**A. Bukti yang harus ada sebelum P6 dinyatakan selesai**

1. Tes fungsi murni untuk setiap baris tabel 5.1 (empat sebab penolakan + satu izin) dan setiap aturan penolakan di 5.2.
2. Tes idempotensi: merekam klaim yang sama dua kali menghasilkan satu baris, dan angka yang sudah ada tidak berubah.
3. Tes agregat: rata-rata dihitung per sesi, buku kosong mengembalikan kosong (bukan nol), verdict `None` saat ambang belum tercapai.
4. Tes jalur tulis: halt tanpa sebab ditolak, halt dengan sebab terbaca penulis klaim, dan halt menghalangi penulisan klaim baru tanpa menghapus baris lama.
5. Bukti angka nyata pada sesi yang sudah ada: menjalankan jalur penulis klaim untuk sesi 2026-10-16 dan menunjukkan jumlah klaim serta barisnya, tanpa mengubah angka baseline P1 sampai P5 (kolam 191, kandidat 46, PEGE seperti lampiran P5).
6. Tes jam cron baru di `tests/test_serve_schedule.py` (jam job ikut terkunci, F12).
7. Permukaan web terlihat di render nyata, bukan hanya HTTP 200, termasuk keadaan buku kosong dan keadaan klaim ditolak (F8).
8. Suite penuh lulus dan `ruff check` bersih di `idx-scraper`; `npx tsc --noEmit` dan `next build` bersih di `idx-web`.

**B. Urutan permukaan yang dipilih pemilik**

Tidak ada mockup untuk P6 (tidak ada tanya-jawab). Kalau kamu ingin melihat bentuknya dulu sebelum plan jalan, mockup bisa dibuat cepat lewat `brainstorm` seperti P5; kalau tidak, permukaan mengikuti pola kartu dan tabel yang sudah ada di `/rekomendasi`.

---

*Dokumen ini bagian dari rangkaian P0-P7. Sesudah dokumen ini, langkah berikutnya plan tertulis (`docs/superpowers/plans/2026-10-19-p6-pengikat-pengingat.md`), bukan kode.*
