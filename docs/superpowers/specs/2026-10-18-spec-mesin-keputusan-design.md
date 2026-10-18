# Spec Desain — Mesin Keputusan IDX (P2)

- **Tanggal:** 18 Okt 2026
- **Status:** **disetujui 18 Okt 2026** oleh pemilik keputusan. Langkah berikutnya plan tertulis (P3), bukan kode. Mulai menulis kode model hanya setelah plan itu disetujui.
- **Revisi:** 2 (18 Okt 2026) — empat keputusan penulis dijawab "ok"; lima pertanyaan terbuka dijawab pemilik (§8); persetujuan diberikan setelah pembacaan
- **Pemilik keputusan:** pemilik proyek (pengguna dashboard)
- **Penulis:** Lucif
- **Cakupan:** mesin keputusan 4-lapis (`idx-scraper`) + permukaan yang menampilkannya (`idx-web`)
- **Dokumen ini menggantikan:** catatan lisan di percakapan. Sesudah disetujui, ini sumber kebenaran P2.

---

## 1. Kenapa dokumen ini ada

Pemilik proyek membuka dashboard hampir tiap hari, tapi **tidak mempercayai satu menu pun**, dan bertanya apakah angka di layar masih bisa dipercaya.

Cerita aslinya: pegang satu saham dengan untung mengambang +5%, ditahan tanpa rencana, lalu *average down*, akhirnya dijual di harga rata-rata — bukan di harga rencana. Kerugiannya bukan dari analisis yang salah, tapi dari **tidak adanya rencana sebelum uang bergerak**.

Percakapan menghasilkan tiga jawaban berurutan dari pemilik keputusan:

1. Ragu utamanya bukan "angkanya salah", tapi **"angkanya benar, tapi aku tidak tahu harus apa."**
2. Tidak ada satu pun dari 13 menu yang pernah membuatnya bertindak, karena **"belum yakin"**.
3. Syarat yakin, kalau ditanya: **"skornya meyakinkan dan volumenya tebal"** — dan kalau itu ada, **"sudah tidak ada yang menahan."**
4. Penyebab ragu yang tersisa, ditemukan belakangan: **"kadang datanya tidak update."**

Jadi dokumen ini bukan soal menambah analisis. Isinya soal **mengubah angka menjadi keputusan yang boleh dipercaya**, dan soal **kesegaran data** yang jadi prasyaratnya.

---

## 2. Fakta hasil pemeriksaan (bukan asumsi)

Diperiksa langsung dari kode kedua repo, 18 Okt 2026. Rincian per menu ada di Lampiran A.

| # | Temuan | Bukti |
|---|---|---|
| F1 | **Ragu pemilik benar dan terkalibrasi.** Bukti out-of-sample terbaik yang dimiliki mesin ini: satu horizon saja (21 hari), pooled IC **+0.038**, **t = 1.95** — di bawah ambang 2 yang lazim. Dasar untuk yakin memang tipis. | `research/recommend.py` (docstring provenance, run 9 Okt 2026) |
| F2 | **Skor yang ada bukan ukuran kekuatan.** Bobotnya semua negatif; skornya = "seberapa bersih nama ini dari sifat yang historis merugikan". Titik netral 50 dan distribusinya menumpuk di titik itu. | `research/recommend.py` (`COMPONENT_WEIGHTS`, `NEUTRAL_SCORE = 50.0`) |
| F3 | **Grade A/B/C adalah peringkat harian, bukan mutu.** A = top 2% kolam **hari itu**, B = 10%, C = 25%. Skor hari ini tak bisa dibandingkan langsung dengan hari lain. | `research/recommend.py` (`BAND_SHARES`, `assign_grades`) |
| F4 | **Yang menyuruh bertindak hanya satu halaman, dan hanya sisi keluar**: Kuat / Tahan / Kurangi sebagian / Pertimbangkan keluar. Sisi masuk ada di halaman lain ("Harga beli / Batas rugi / Target") dan tak pernah berkata "yang ini, hari ini". | `app/keputusan/DecisionWorkspace.tsx`, `app/rekomendasi/page.tsx` |
| F5 | **Mesin klaim beku sudah ada.** `research.recommendation_log` menyimpan grade, score, entry_ref, stop, target, rr, horizon **per (code, trade_date)** — direkam sebelum hasilnya keluar. Ia tidak pernah sampai ke pemilik sebagai bukti. | `sql/research_schema.sql` (recommendation_log), `research/recommendation_log.py` |
| F6 | **Lapis 4 benar-benar nol.** Tidak ada satu pun tabel jurnal / posisi / pre-commit / portofolio di seluruh repo. Yang ada hanya log hasil (`signal_log`, `recommendation_log`) — catatan mesin, bukan catatan keputusan pemilik. | grep DDL di `sql/` |
| F7 | **Data untuk "volume tebal" sudah lengkap.** `research.raw_eod` menyimpan `volume`, `value` (rupiah), `frequency`, plus `foreign_buy/sell/net` dan `nr_volume/nr_value/nr_freq`. | `sql/research_schema.sql`, `sql/research_eod_meta.sql`, `sql/market_segment_daily.sql` |
| F8 | **Kesegaran tidak punya verdict.** `components/LastUpdated.tsx` hanya **menuliskan** tanggal sesi — tidak pernah menyimpulkan segar/basi. Satu-satunya tempat yang benar-benar menghitung basi adalah dossier. | `components/LastUpdated.tsx`; `api/dossier.py` (`mark_stale`, `latest_session_date`) |
| F9 | **Sebagian "data tidak update" bersifat permanen, bukan gangguan.** 7 sesi bursa lampau tidak punya close indeks resmi dan tidak punya sumber non-live untuk menariknya ulang. | dicatat saat startup serve 18 Okt 2026 |
| F10 | **Kanal pengingat sudah ada dan hidup.** Job `health_eod` 16:45 mengirim Telegram; aturan alarm tersimpan di `data/alerts.json`. | `cli.py` (job `health_eod`), `api/alerts.py`, `alerts_store.py` |
| F11 | **Dua jejak berpijak pada dasar berbeda.** `recommendation_log` memakai panel **mentah**, `signal_log` memakai panel **terkoreksi** (adjusted). | `research/recommendation_log.py` (`latest_pit`), `research/signal_log.py` (`ic.load_panel`) |
| F12 | Peta menu di dokumen dashboard sudah basi: menyebut `/ruang-keputusan` dan `/watchlist` yang tidak ada di `app/`. | `idx-web/docs/README.md` |

---

## 3. Tujuan, non-tujuan, anti-tujuan

**Tujuan**

- Satu tempat menjawab "hari ini, apa" — daftar pendek berisi keputusan untuk dijalankan, bukan daftar panjang untuk dibaca.
- Setiap angka yang tampil membawa **tanggal sesinya** dan **status kesegarannya**, dan basi tidak pernah tampil sebagai segar.
- Setiap perintah membawa rekamannya sendiri: berapa kali aturan itu diucapkan, berapa kali benar.
- Rencana keluar sudah tertulis **sebelum** uang bergerak.
- Ukuran posisi ikut ditetapkan, dibatasi ketebalan volume.

**Non-tujuan**

- Auto-trading. Eksekusi tetap manual di tangan pemilik.
- Menambah menu ke-14.
- Membangun model prediktif baru. Yang ada belum terbukti; memperbaiki yang cacat lebih dulu (P7 tetap kondisional).
- Mengganti mesin yang sudah ada tanpa bukti bahwa penggantinya lebih baik.

**Anti-tujuan** (kalau ini terjadi, proyek gagal walau kodenya jalan)

- Menambah kartu cantik yang tidak mengubah satu pun perilaku.
- Menampilkan verdict yang lahir dari data basi.
- Menampilkan skor telanjang tanpa arti skala, sehingga "tertinggi" berarti seri.
- Menampilkan jejak sebagai statistik agregat abstrak, bukan sebagai klaim yang bisa ditelusuri.

---

## 4. Keputusan pemilik (terkunci — tidak dibuka lagi)

| Kode | Keputusan |
|---|---|
| U1 | Ini **decision support**, bukan auto-trading. Eksekusi manual. |
| U2 | Horizon **swing mingguan–bulanan**, bukan harian. |
| U3 | Aturan yakin, dengan kata pemiliknya sendiri: **skor tertinggi + volume tertebal → bertindak.** Tidak ada syarat ketiga, tidak ada penahan. |
| U4 | Ragu utamanya adalah **"tidak tahu harus apa"**, bukan ragu data. |
| U5 | Kesegaran data adalah syarat nyata: kalau data tidak update, seluruh layar kehilangan arti. |
| U6 | Melindungi dari kegagalan BBRI itu wajib: **keluar ditulis sebelum masuk.** |

Kerangka 4-lapis tetap: (1) gerbang rezim → (2) pemeringkat kandidat → (3) rencana posisi → (4) pengikat & pengingat.

---

## 5. Keputusan desain

Ditandai **[call penulis]** bila ini pilihan saya, bukan permintaan pemilik. Semuanya boleh ditolak.

### D1 — Kesegaran adalah gerbang, bukan hiasan **[call penulis]**

Setiap permukaan menampilkan **satu** dari tiga status, dihitung di backend (bukan dibandingkan di browser):

- `segar` — data berasal dari sesi pasar terakhir yang diharapkan.
- `basi` — berasal dari sesi yang lebih lama, dan sesi yang lebih baru ada.
- `tak diketahui` — sesi pasar terakhir tidak bisa ditentukan (mis. penjaga tidak terbaca).

**Aturan keras:** verdict yang lahir dari data `basi` **ditahan**. Yang tampil hanya angka, tanggalnya, dan sebabnya. Aturan ini berlaku di semua halaman, bukan hanya di `/`.

Alasan: F8. Sekarang 12 dari 13 halaman tidak pernah menyimpulkan basi, jadi pemilik harus menebak — dan tebakannya adalah "semuanya tidak bisa dipercaya".

Sesi pasar terakhir punya **dua basis yang sudah ada di kode**: basis **indeks** (`index_summary_daily` ∪ `index_quotes` COMPOSITE, dipakai penjaga `idx health`) dan basis **panel saham** (`api/analytics._latest_eod_date` — `max(trade_date)` dari `research.latest_pit`, dipakai dossier).

**Keputusan pemilik (§8 Q2): verdict berpijak pada panel saham.** Konsekuensinya: 7 sesi lampau tanpa close indeks (F9) tidak membisukan seluruh layar. Bagian yang memang bersumber indeks — kartu rezim, banner indeks — membawa **penanda terpisah**: ia basi sendiri, tanpa menularkan statusnya ke seluruh halaman.

### D2 — Daftar pendek, bukan daftar panjang **[call penulis]**

Halaman utama menjawab **"hari ini, apa"**: daftar pendek **3–5 nama** yang lolos gerbang, terurut, dan setiap barisnya membawa

1. kalimat perintah,
2. ukuran posisi (persen dari modal),
3. harga masuk, harga batas rugi, target,
4. tanggal sesi + status kesegaran,
5. rekaman aturan yang melahirkannya (lihat D6).

Plafon 5 itu keras: kalau kolam mengeluarkan 12 nama, yang tampil tetap 5 teratas. Daftar penuh tetap ada, tapi statusnya turun jadi lampiran — dibuka kalau mau, bukan yang harus dibaca dulu.

Batasan: maksimal satu halaman digarap ulang, atau satu halaman baru. Tidak menambah menu ke-14. Pilihan halaman (rombak `/` atau perluas `/keputusan`) diserahkan ke pembangun — §7.

### D3 — "Skor meyakinkan" didefinisikan ulang **[call penulis]**

Karena F2 dan F3, angka skor 0–100 yang ada **tidak boleh** ditampilkan sebagai kekuatan keyakinan. Yang ditampilkan:

1. **Peringkat persentil** di kolam hari itu, dan peringkat dalam riwayat saham itu sendiri.
2. **Tie-break eksplisit.** Kalau beberapa nama menumpuk di titik netral 50, mesin tidak boleh berkata "tertinggi". Urutan pemutus ditulis dan ditampilkan, mis. ketebalan nilai transaksi lalu jarak ke puncak 52 minggu.
3. Label "meyakinkan" hanya keluar kalau komponennya sudah lolos gate bukti yang sama dengan yang dipakai `score_eval.py` (`EDGE_GATE_T`, `MIN_EDGE`, konsistensi antar-fold). Komponen yang belum lolos boleh muncul sebagai informasi, **tidak** sebagai alasan keyakinan.

### D4 — "Volume tebal" jadi ambang terukur **[call penulis]**

Definisi awal, semuanya bisa dihitung dari `raw_eod` (F7):

- **Nilai transaksi harian** (`value`, rupiah) ≥ ambang pasar pada sesi itu. Ambang awal: persentil ke-60 lintas saham. Angka ini jadi **konstanta bernama**, bukan angka tersebar di beberapa file.
- **Konsisten:** syarat di atas terpenuhi ≥ 3 dari 5 sesi terakhir. Lonjakan satu hari tidak dihitung tebal.

Konsekuensi yang penting: ketebalan volume **menetapkan plafon ukuran posisi**, karena volume tipis berarti tidak ada pintu keluar. Jadi volume masuk ke lapis 3 (`position_size_pct`), bukan cuma jadi filter tampilan.

Ambang pastinya diserahkan ke pembangun — yang dikunci di sini cuma syaratnya *terukur, bernama, dan satu tempat*.

### D5 — Jurnal adalah buku bukti, bukan diari **[call penulis]**

Tabel baru (nama diserahkan ke pembangun) menyimpan satu baris per klaim:

`tanggal klaim · kode · arah · aturan yang melahirkan · harga referensi · stop · target · horizon · ukuran posisi · status · hasil`

Aturan:

- Klaim ditulis **sekali**, sebelum hasilnya keluar — idempoten per (`code`, `trade_date`, `aturan`). Rekam ulang tidak boleh menimpa klaim lama.
- Hasil diisi belakangan, apa adanya. Hasil jelek ditulis.
- Yang pertama ditampilkan bukan isi jurnalnya, tapi **empat angka**: jumlah klaim, berapa kena target, berapa kena batas rugi, rata-rata hasil relatif pasar.

Alasan: F5. Mesin klaim beku sudah ada; yang tidak ada adalah (a) cara klaim disimpan sebagai keputusan pemilik, dan (b) cara ia dikembalikan sebagai bukti yang bisa dibaca.

### D6 — Setiap perintah membawa rekamannya sendiri **[call penulis]**

Ini yang menyelesaikan tegangan antara U3 ("tidak ada yang menahan") dan kenyataan F1 (bukti masih tipis).

Mesin **tidak menahan diri** — kalau aturannya berkata beli, ia berkata beli, dengan ukuran posisi. Tapi setiap kalimat perintah membawa satu baris kecil rekaman di bawahnya:

> *Aturan ini: 12 klaim · 5 kena target · 4 kena batas rugi · rata-rata +1,4% vs pasar*

Dengan 3 klaim pun baris itu tampil, apa adanya, termasuk kalau angkanya jelek. Kepercayaan datang dari rekaman yang bertambah, bukan dari keyakinan yang diminta.

Akibat lanjutan: kata kerja keras ("beli", "jual") hanya boleh keluar dari **aturan yang sudah terekam** di jurnal. "Terekam" berarti **klaimnya sudah tertulis**, bukan sudah ada hasilnya — kalau menunggu hasil, halaman utama baru boleh bicara ±1 bulan setelah klaim pertama, dan selama itu ia bisu. Pada hari-hari pertama, jurnal belum punya hasil, dan halaman mengatakannya apa adanya: *aturan ini belum punya rekaman*.

### D8 — Klaim ditulis oleh jam, bukan oleh kunjungan **[call penulis]**

Klaim harian ditulis oleh **job terjadwal** di akhir sesi, bukan saat dashboard dibuka. Kalau bergantung pada kunjungan, hari-hari ketika pemilik tidak membuka layar tidak punya klaim — rekamannya jadi berbias (hanya hari yang menarik) dan jumlahnya tidak pernah sah sebagai bukti. Job baru memakai jalur yang sudah ada di `cli.py` dan kanal alarm yang sudah hidup (F10).

### D7 — Tidak ada model baru di P2–P6 **[call penulis]**

Yang ada sudah cukup untuk dikerjakan; yang perlu dibetulkan adalah definisi (D3), gerbang kesegaran (D1), lapis yang kosong (D5), dan permukaannya (D2). Model prediktif tetap P7, dan tetap kondisional pada hasil P3.

---

## 6. Tujuh poin spec, urutan baru

Urutan lama (1 lingkup → 2 data → 3 rezim → 4 pemeringkat → 5 posisi → 6 pengikat → 7 penilaian) dibalik di bagian hulu, karena percakapan menunjukkan kepercayaan — bukan analisis — yang jadi penghambat.

### 6.1 Cara dinilai & cara gagal — **poin #1**

- Apa yang disebut klaim: satu (kode, tanggal, arah, aturan, stop, target, horizon, ukuran).
- **Berapa banyak per sesi** (keputusan pemilik, §8 Q1): satu klaim untuk **setiap nama yang tampil** di daftar pendek — 3 sampai 5 baris per sesi, lebih sedikit kalau kolamnya tipis. Pembacaan penulis atas "kandidat yang lolos": nama yang benar-benar tampil, bukan seluruh kolam.
- Kapan klaim dianggap beku: saat ditulis ke jurnal, sebelum hasilnya keluar.
- Cara dinilai: hasil per klaim dibanding **pasar berimbang**, bukan dibanding nol; plus agregat D5 (jumlah, target, stop, rata-rata relatif pasar).
- **Dua ukuran hasil, keduanya ditulis:** (a) *abnormal pada horizon tetap* — harga terkoreksi dibanding pasar berimbang selama horizon klaim; ini yang dipakai untuk skor agregat karena tidak bergantung jalur; (b) *sentuhan level* — apakah harga menyentuh target atau batas rugi, dibaca dari high/low harian. Kalau satu sesi menyentuh keduanya, yang dicatat **batas rugi** (asumsi terburuk, karena urutan intra-sesi tidak diketahui). **[call penulis]**
- **Dasar harga:** level yang ditampilkan ke manusia (entry/stop/target) memakai harga **nyata (mentah)** — itu harga yang benar-benar ditransaksikan; hasil dan skor diukur pada harga **terkoreksi**. Satu klaim menyimpan keduanya. **[call penulis]**
- *Cara gagal, ditulis di depan:* **ambangnya 30 klaim, atau 6 minggu, mana yang lebih dulu** (keputusan pemilik, §8 Q4). Kalau pada titik itu rata-rata hasil relatif pasar tidak melewati nol, mesin ini dinyatakan **tidak terbukti** dan berhenti mengeluarkan perintah — yang tersisa hanya alat baca. Ini ditulis sekarang supaya tidak bisa ditawar nanti saat hasilnya jelek.
- **Agregat dihitung per sesi, bukan per klaim** **[call penulis]**: klaim-klaim di hari yang sama berbagi pasar yang sama, jadi 30 klaim dalam 8 sesi bukan 30 bukti yang saling bebas. Yang dilaporkan: rata-rata per sesi, sebaran antar sesi, dan jumlah sesi. Ini mencegah sampel terlihat lebih besar daripada kenyataannya.
- Harness-nya sudah ada: `research/score_eval.py` (`walk_forward`) dengan gate `EDGE_GATE_T = 1.5`, `MIN_EDGE = 0.0015`, `MIN_FOLD_CONSISTENCY = 2/3`, `SHRINK = 0.5`. **Syarat kerja P3: harness ini harus punya tes sebelum dipakai sebagai bukti** (sekarang nol tes, F-fakta pendukung).

### 6.2 Pengikat & pengingat — **poin #2** (lapis 4, yang nol)

- Jurnal bukti (D5) — satu klaim, satu baris, idempoten.
- Aturan pre-commit: rencana keluar + ukuran posisi wajib ada **sebelum** baris jurnal ditulis. Klaim tanpa stop dan target **ditolak sistem**, bukan diberi peringatan.
- Pengingat memakai kanal yang sudah hidup (F10): Telegram. Isinya pendek dan menyebut satu hal: klaim mana yang menyentuh batasnya.
- **Jurnal mencatat klaim mesin saja** (keputusan pemilik, §8 Q3) — bukan posisi nyata yang benar-benar dibeli pemilik. Konsekuensinya diakui terbuka: *average down* ala cerita BBRI tidak akan pernah muncul di jurnal ini, karena ia bukan klaim mesin. Menutup celah itu bukan tugas jurnal, tapi tugas aturan pre-commit dan pengingat (dua butir di atas).

### 6.3 Kesegaran data — **poin baru**

Lahir dari percakapan, bukan dari daftar awal. Isinya D1. **Keputusan pemilik (§8 Q2):** verdict berpijak pada panel saham, dan bagian yang bersumber indeks membawa penandanya sendiri.

### 6.4 Kontrak data — **poin #3** (dulu #2)

- Semua angka analitik berasal dari `research.latest_pit` (bitemporal, `as_of`), bukan dari `raw_eod` langsung.
- Harga terkoreksi selalu **turunan** (`prices_adjusted` / `prices_asof_adj`), tidak pernah disimpan.
- Larangan: tidak ada faktor yang melihat ke depan. Sudah ada tes yang membuktikannya (`test_factors_do_not_use_future_data`); aturan ini dipertahankan untuk semua fitur baru.
- **Dua dasar, dua peran, satu aturan** (F11): harga **mentah** untuk apa pun yang akan ditransaksikan manusia — level entry/stop/target, harga di layar; harga **terkoreksi** untuk apa pun yang diukur sebagai hasil — return, abnormal, skor, perbandingan antar waktu. Satu klaim menyimpan kedua dasar itu. Satu tabel tidak boleh mencampur keduanya tanpa kolom yang menyatakan dasarnya.

### 6.5 Lapis 1 — gerbang rezim — **poin #4**

- Sudah nyata: `research.regime.py` → `research.regime_daily` (`regime`, `adx`, `plus_di`, `minus_di`, `realized_vol`, `vol_state`), sumber `index_summary_daily` COMPOSITE, warmup 40 sesi.
- Yang ditambahkan: **gerbang** yang bisa gagal. **Keputusan pemilik (§8 Q5):** halaman utama hanya diam saat rezim **secara eksplisit menolak**, dan saat itu alasannya disebut di layar. Rezim datar atau tidak jelas tidak memblokir. Alasan: rezim hari ini hanya jadi label di `/sectors` dan banner di `/`.
- Catatan: kalau close indeks sesi berjalan belum ada, rezim **basi** — kena D1.

### 6.6 Lapis 2 — pemeringkat kandidat — **poin #5**

- Sudah nyata: `composite.market_factor_ranks` (persentil lintas seksi, `MAX_ADJUSTMENT = 10.0`), `broker_activity.composite_scores` (gate `|IC| ≥ 0.05` & `|ICIR| ≥ 0.5`), `recommend.score_candidate` / `rank_candidates` / `assign_grades`.
- Yang ditambahkan: D3 (skala yang bisa dibandingkan + tie-break eksplisit) dan D4 (gerbang volume tebal sebagai syarat masuk kolam).
- **Satu kolam, bukan dua.** Gerbang volume (D4) **memperluas** lantai nilai harian yang sudah dipakai `/rekomendasi`, tidak menumpuk di atasnya, dan grade A/B/C dihitung dari kolam tunggal itu. Kalau ada dua kolam, satu nama bisa bergrade berbeda di dua halaman — persis "seri yang tak bisa dibandingkan" yang dilarang di §3. **[call penulis]**
- Definisi kolam harus eksplisit: siapa yang berhak masuk (mis. nilai transaksi minimum + tidak dalam karantina `quarantine_eod`), berapa banyak nama yang keluar.

### 6.7 Lapis 3 — rencana posisi — **poin #6**

- Sudah nyata sebagai fungsi murni: `entry_plan` (setup, entry, stop, target, RR) dan `position_size_pct` (batas `MAX_POSITION_PCT = 25.0`, risiko `DEFAULT_RISK_PCT = 1.0`, `TARGET_RR = 2.0`, `STOP_VOL_MULT = 3.0`).
- Yang ditambahkan: plafon dari ketebalan volume (D4), dan kewajiban rencana ada sebelum klaim ditulis (6.2).
- Aturan pembatalan ditulis eksplisit: kalau harga menembus level invalidasi, rencana berhenti — tidak ada menambah posisi di bawah level itu. Ini pelajaran langsung dari cerita pemilik.

### 6.8 Lingkup & non-lingkup — **poin #7** (dulu #1)

Dirangkum di §3. Tidak berubah isinya, hanya turun jadi penutup.

---

## 7. Yang diserahkan ke pembangun

Bebas diputuskan tanpa bertanya lagi:

- Nama tabel jurnal, bentuk skema, dan apakah klaim disimpan terpisah atau menempel pada `recommendation_log`. (Bentuk skema milik pembangun; **apa yang dihitung sebagai satu klaim** sudah dikunci di 6.1.)
- Bentuk endpoint dan apakah status kesegaran dihitung sekali per request atau disimpan.
- Halaman mana yang dirombak untuk D2 (`/` atau `/keputusan`), selama tidak menambah menu.
- Angka persentil pastinya di D4, dan jumlah sesi konsistensi (3 dari 5 adalah titik awal, bukan angka suci).
- Detail teknis basis kesegaran sesuai keputusan §8 Q2 — termasuk penjadwalan job D8 dan bentuk penanda terpisah untuk bagian berindeks.
- Bentuk job D8: subperintah baru di `cli.py`, jadwalnya, dan cara ia melapor kalau gagal.
- Urutan pengerjaan teknis dan pemecahan jadi ticket.

---

## 8. Keputusan pemilik (18 Okt 2026)

Lima pertanyaan yang membentuk dokumen ini sudah dijawab. Jawabannya mengikat dan tercermin di bagian-bagian yang bersangkutan.

| # | Pertanyaan | Keputusan | Terpasang di |
|---|---|---|---|
| Q1 | Bentuk hariannya, dan apa yang dihitung satu klaim | **Daftar pendek 3–5 nama**, dan klaim ditulis **untuk setiap kandidat yang lolos** | D2, 6.1 |
| Q2 | Basis kesegaran: indeks atau panel saham | **Panel saham** untuk verdict, plus penanda terpisah untuk bagian yang bersumber indeks (ikut usulan penulis) | D1, 6.3 |
| Q3 | Cakupan jurnal | **Klaim mesin saja** — posisi nyata yang dibeli pemilik tidak dicatat | 6.2 |
| Q4 | Ambang terbukti / tidak terbukti | **30 klaim, atau 6 minggu, mana yang lebih dulu** (ikut usulan penulis) | 6.1 |
| Q5 | Kapan halaman utama boleh diam | **Hanya saat rezim eksplisit menolak**, dan alasannya disebut di layar (ikut usulan penulis) | 6.5 |

Empat keputusan penulis yang lain juga diterima apa adanya: dasar harga mentah-vs-terkoreksi (6.1, 6.4), asumsi terburuk saat stop dan target tersentuh di sesi yang sama (6.1), klaim ditulis job terjadwal bukan saat dashboard dibuka (D8), dan satu kolam dengan grade tunggal (6.6).

**Satu penafsiran yang dikunci di sini:** "kandidat yang lolos" pada Q1 dibaca sebagai **nama yang benar-benar tampil di daftar pendek 3–5**, bukan seluruh kolam. Kalau maksudnya seluruh kolam — yang bisa puluhan nama per sesi — jumlah klaim per hari berubah besar dan agresivitasnya perlu ditinjau ulang.

---

## 9. Batas dan risiko

| # | Batas / risiko | Sikap |
|---|---|---|
| R1 | **Bukti masih tipis** (t = 1.95, satu horizon). | Jangan menjanjikan apa pun di P2–P6. Rancang supaya bisa terbukti atau gagal, bukan supaya terlihat pintar. |
| R2 | **7 sesi lampau basi permanen.** | Akui di layar. Jangan menambal dengan data yang lebih tua lalu menyebutnya segar. |
| R3 | **Dua dasar harga** (mentah vs terkoreksi, F11). | Satu klaim menyimpan keduanya; tidak ada tabel yang mencampurnya tanpa kolom dasar. |
| R4 | **Harness tanpa tes** (`score_eval.py`). | Tesnya dikerjakan sebelum hasilnya dipakai sebagai bukti (P3). |
| R5 | **Peta menu di `idx-web/docs/README.md` sudah basi** (F12). | Perbaiki saat menyentuh dokumentasi; bukan pekerjaan tersendiri. |
| R6 | **Godaan menambah menu.** | Setiap penambahan halaman harus menggantikan yang ada, bukan menumpuk. |
| R7 | **Klaim seharian berkorelasi.** 3–5 klaim per sesi membuat ambang 30 klaim tercapai dalam ±8 sesi bursa, jauh sebelum 6 minggu. | Agregat dilaporkan **per sesi** (6.1), bukan per klaim, supaya sampel tidak terlihat lebih besar daripada kenyataannya. Ambang 6 minggu tetap dilaporkan berdampingan. |
| R8 | **Jurnal tidak melihat posisi nyata** (keputusan Q3). Celah *average down* tetap terbuka. | Ditutup lewat aturan pre-commit dan pengingat (6.2), bukan lewat jurnal. |

---

## Lampiran A — Peta 13 menu: data → verdict hari ini

| Menu | Isi | Verdict? | Sumber |
|---|---|---|---|
| `/` | MarketOverview, RegimeBanner, DataHealthCard, NewsCards, TopLeaders, TopBrokers, SignalsPanel | tidak (badge BUY/SELL/HOLD) | berbagai endpoint |
| `/screener` | tabel saham + badge | badge saja | `/api/screener` |
| `/rekomendasi` | "Rekomendasi Beli", grade A/B/C → Teratas/Bagus/Cadangan, Harga beli/Batas rugi/Target | sisi masuk, tanpa "yang ini hari ini" | `/api/recommendations`, `/api/recommendations/track` |
| `/keputusan` | Kuat / Tahan / Kurangi sebagian / Pertimbangkan keluar | **ya — satu-satunya, dan hanya sisi keluar** | `/api/stocks/{code}/decision`, `/api/hold-check` |
| `/jejak-sinyal` | hit-rate BUY/SELL, gate \|t\| ≥ 2 | agregat, bukan per klaim | `/api/signals/track` |
| `/backtest` | simulasi, biaya | tidak | `/api/backtest/run` |
| `/faktor` | statistik faktor | tidak | `/api/factors/overview` |
| `/radar` | label smart-money, ambang rasio ≥ 0.5, track-record \|t\| ≥ 2 | label, bukan skor | `/api/smart-money/*` |
| `/foreign` | arus asing, kepemilikan | tidak | `/api/foreign-flow` |
| `/flow` | aliran broker, timeline | tidak | `/api/broker-flow` |
| `/sentimen` | skor pasar 0–100 (>60 risk-on, <40 risk-off) | tidak | `/api/sentiment` |
| `/broker-activity` | skor akumulasi 0–100 (50 = median) | tidak | `/api/broker-activity` |
| `/sectors` | fase AKUMULASI/MEMBAIK/MEMUDAR/TERPURUK/STABIL, regime | label lunak | `/api/sectors/rotation` |

## Lampiran B — Data yang tersedia

| Kebutuhan | Tersedia? | Sumber |
|---|---|---|
| Harga harian per saham | ya | `research.raw_eod` (open/high/low/close/prev_close) |
| **Volume** | ya | `research.raw_eod.volume` |
| **Nilai rupiah** | ya | `research.raw_eod.value` |
| Frekuensi | ya | `research.raw_eod.frequency` |
| Non-reguler (volume/nilai/frekuensi) | ya | `nr_volume`, `nr_value`, `nr_freq` |
| Arus asing | ya | `foreign_buy`, `foreign_sell`, `foreign_net` |
| Harga terkoreksi | turunan | `prices_adjusted` view / `prices_asof_adj(T)` |
| Rezim harian | ya | `research.regime_daily` |
| Indeks | ya | `index_summary_daily` (COMPOSITE), `index_quotes` |
| Jejak sinyal | ya | `research.signal_log` |
| Jejak rekomendasi (klaim beku) | ya | `research.recommendation_log` |
| Bobot faktor dari IC | ya | `research.factor_ic_history` |
| Baris bermasalah | ya | `research.quarantine_eod` |
| **Jurnal keputusan / posisi / pre-commit** | **tidak** | — |
| Kanal pengingat | ya | Telegram (`health_eod` 16:45, `data/alerts.json`) |

---

*Dokumen ini bagian dari rangkaian P0–P7. Sesudah disetujui, langkah berikutnya adalah plan tertulis (P2 → P3), bukan kode.*
