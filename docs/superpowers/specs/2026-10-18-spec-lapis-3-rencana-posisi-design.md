# Spec Desain — Lapis 3: Rencana Posisi (P5)

- **Tanggal:** 18 Okt 2026
- **Status:** **disetujui 18 Okt 2026** oleh pemilik keputusan (menjawab "saya ikut rekomendasi", termasuk kelima call penulis di bagian 4b). Langkah berikutnya plan tertulis, bukan kode. Kode P5 baru ditulis setelah plan itu disetujui.
- **Revisi:** 3 (18 Okt 2026). Revisi 1 sesudah percakapan; revisi 2 sesudah pemeriksa konteks segar menemukan enam lubang (empat ditutup sebagai call penulis di bagian 4b, dua naik jadi pertanyaan); revisi 3 sesudah pemilik menjawab kelima pertanyaan (bagian 7).
- **Pemilik keputusan:** pemilik proyek (pengguna dashboard)
- **Penulis:** Lucif
- **Cakupan:** lapis 3 mesin keputusan di `idx-scraper` + tiga permukaan di `idx-web`
- **Bergantung pada:** spec mesin keputusan P2 (`docs/superpowers/specs/2026-10-18-spec-mesin-keputusan-design.md`), khususnya D1, D4, 6.5, 6.7; dan hasil P4 (gerbang rezim, penahanan verdict).
- **Dokumen ini menggantikan:** catatan lisan percakapan 18 Okt 2026 (pilihan permukaan A+B+C, modal sebagai input dinamis di browser).

---

## 1. Kenapa dokumen ini ada

P2 §6.7 mencatat lapis 3 "sudah nyata sebagai fungsi murni". Itu benar untuk satu nama, tapi belum benar untuk seseorang yang mau memindahkan uang. Tiga hal belum ada:

1. **Angka yang bisa dieksekusi.** `position_size_pct` mengembalikan persen modal. Tidak ada rupiah, tidak ada lot, tidak ada "kalau stop kena, ruginya sekian".
2. **Plafon ketebalan volume.** P2 D4 sudah memutuskan konsekuensinya: "ketebalan volume menetapkan plafon ukuran posisi, karena volume tipis berarti tidak ada pintu keluar". Yang dibangun P4 baru setengahnya: ketebalan menyaring siapa yang boleh masuk papan, tapi tidak pernah membatasi besar posisinya.
3. **Batas tingkat portofolio.** Tidak ada satu pun tempat yang bisa berkata "masih boleh masuk berapa nama lagi". P2 F6 menyebut lapis 4 nol; lapis 3 juga tidak punya tempat untuk bertanya.

Cerita yang melahirkan aturan ini sudah ada di P2 §1: posisi ditahan tanpa rencana, lalu *average down*, lalu dijual bukan di harga rencana. P4 memasang gerbang yang bisa menolak. P5 memasang ukuran dan plafonnya, supaya tawaran yang lolos pun datang dengan besar yang bisa dipertanggungjawabkan.

Keputusan yang sudah diambil pemilik keputusan hari ini:

> "modalnya ada pokoknya, harusnya ini bisa dinamis input untuk modalnya." Modal jadi **input dinamis**, disimpan **di browser saja**.
> Pilihan permukaan: **A, B, dan C** (ketiganya).

---

## 2. Fakta hasil pemeriksaan (bukan asumsi)

Diperiksa dari kode kedua repo, 18 Okt 2026.

| # | Temuan | Bukti |
|---|---|---|
| F1 | Ukuran posisi sudah nyata sebagai fungsi murni, tapi hanya persen: `posisi % = risk_pct / jarak_stop(%) * 100`, dibatasi `max_pct`. Tidak pernah menerima modal, jadi rupiah dan lot tidak pernah ada. | `research/recommend.py:893-914` (`position_size_pct`, `DEFAULT_RISK_PCT = 1.0`, `MAX_POSITION_PCT = 25.0`) |
| F2 | Level eksekusi sudah lengkap: zona entry, `entry_ref`, stop, target, RR. Stop struktural dulu (low 20 sesi), volatilitas sebagai cadangan (`STOP_VOL_MULT = 3.0`); target dari `TARGET_RR = 2.0`. | `research/recommend.py:442-556` (`entry_plan`) |
| F3 | Sizing dipasang di satu tempat saja dan dipakai kedua permukaan (papan + satu emiten). Kalau aturan berubah, satu tempat ini yang berubah. | `api/analytics.py:2725-2731` (`_with_sizing`) |
| F4 | Tidak ada state portofolio di mana pun: tidak ada tabel posisi, tidak ada jumlah posisi terbuka, tidak ada eksposur. Batas "berapa nama lagi" secara harfiah tidak bisa dijawab sistem hari ini. | P2 F6 (grep DDL di `sql/`), lapis 4 = nol |
| F5 | Baris kandidat **sudah** membawa nilai transaksi sesinya, dan urutan pemutus peringkat sudah memakainya. Plafon likuiditas bisa dihitung tanpa data baru. | `research/recommend.py:866` (`metrics.value`), `:259` (tie-break) |
| F6 | Repo sudah punya konvensi partisipasi: plafon **2%** dari nilai transaksi harian (volume x close mentah) di model biaya. Kalau lapis 3 memakai angka lain, layar dan backtest akan bercerita beda. | `api/simulation.py:56-61` (`max_adv_pct = 0.02`), `backtest.py:307-319` (`_liquidity_cap`) |
| F7 | Di muka web, satu-satunya jejak porsi adalah `Saran porsi X% modal` di dalam bukaan "Lihat detail", dan ia disembunyikan saat verdict ditahan. Tiga permukaan yang dipilih hari ini belum ada satu pun. | `idx-web/app/rekomendasi/page.tsx:175-180` |
| F8 | Penyimpanan lokal sudah jadi pola di web (`ml-theme`, `ml-sidebar-collapsed`), jadi modal bisa disimpan klien-side tanpa tabel baru. | `idx-web/app/layout.tsx:21`, `components/Sidebar.tsx:80`, `components/ThemeToggle.tsx:29` |
| F9 | Papan di-cache 30 menit dan penahanan verdict terjadi **sesudah** cache. Apa pun yang bergantung pada modal harus dihitung sesudah cache, kalau tidak cache papan pecah tiap angka modal yang dicoba. | `api/analytics.py` (`_research_cache_put(..., ttl=1800.0)`, `_withhold_panel`, `_gate_withhold`) |
| F10 | Saat gerbang menolak, `grade` dan `position_pct` sudah ditahan (jadi `null`) dan barisnya ditandai `withheld`. Rencana posisi harus tunduk pada aturan yang sama. | P4 `api/analytics.py` (`_gate_withhold`), `api/schemas.py` |
| F11 | Rencana pembatalan sudah diputuskan di P2 §6.7: begitu harga menembus level invalidasi, rencana berhenti dan tidak ada menambah posisi di bawah level itu. | spec P2 §6.7 |
| F12 | Papan tidak punya state portofolio, jadi plafon tingkat portofolio hanya bisa berpijak pada angka yang **diketik pemilik**. | F4, dan pilihan pemilik hari ini (modal di browser) |

---

## 3. Tujuan, non-tujuan, anti-tujuan

**Tujuan**

- Setiap tawaran membawa besar yang bisa dieksekusi: persen modal, rupiah, jumlah lot, dan rugi maksimum kalau stop kena.
- Besar itu dibatasi plafon yang bernama dan terlihat. Kalau ada plafon yang memotong, layar menyebut plafon mana.
- Ukuran posisi jadi fungsi murni dari (modal, risiko, kandidat, keadaan pasar, keadaan portofolio yang diketik), supaya bisa diuji dan supaya layar dan jurnal (P6) memakai aturan yang sama.
- Di layar porsi, kalimat pembatalannya ikut: level invalidasi, dan "di bawah level itu tidak ada menambah posisi".
- Angka yang tidak bisa dihitung tidak ditampilkan sebagai angka. Modal kosong berarti rupiah dan lot kosong, bukan nol.

**Non-tujuan**

- Tidak mencatat posisi nyata, tidak ada jurnal, tidak ada "saya benar-benar beli ini". Itu lapis 4 (P6).
- Tidak menyambung ke broker, tidak mengirim order, tidak ada auto-trading. Eksekusi tetap manual di tangan pemilik.
- Tidak menyimpan modal di server, tidak menambah akun. Modal hanya di browser (keputusan pemilik hari ini).
- **Tidak mengalokasikan anggaran berurutan antar kandidat.** Papan adalah menu, bukan daftar beli: P5 tidak membagi modal ke nama-nama yang belum tentu dibeli.

**Anti-tujuan** (kalau salah satu ini terjadi, pekerjaannya gagal meski kodenya jalan)

- Angka yang tampak seperti ukuran posisi tapi lahir dari modal yang diasumsikan diam-diam.
- Ukuran posisi yang mengabaikan likuiditas: posisi besar di nama tipis, tidak ada pintu keluar.
- Plafon yang memotong tanpa berkata ia memotong.
- **Dua angka porsi berbeda untuk satu nama pada satu layar.**

---

## 4. Keputusan yang dikunci

| # | Keputusan | Sumber |
|---|---|---|
| D1 | **Modal adalah input dinamis**, diketik pemilik, disimpan **di browser** (localStorage, kunci `ml-modal`). Tidak ada tabel, tidak ada endpoint penyimpanan. | pemilik, 18 Okt 2026 |
| D2 | **Tiga permukaan**: (A) satu baris porsi di kartu papan, (B) panel Rencana posisi di halaman emiten, (C) strip portofolio di atas papan. | pemilik, 18 Okt 2026 |
| D3 | **Risiko per transaksi juga input**, default 1% modal (default yang sudah ada di kode, `DEFAULT_RISK_PCT`). Disimpan di browser dengan kunci `ml-risk-pct`. | pemilik, 18 Okt 2026 |
| D4 | **Plafon per nama tetap 25% modal** (`MAX_POSITION_PCT`), tidak diubah. | kode hari ini |
| D5 | **Plafon likuiditas memakai angka yang sama dengan backtest: 2% dari nilai transaksi harian.** Nilai yang dipakai: **median nilai transaksi 5 sesi terakhir**. | pemilik, 18 Okt 2026 |
| D6 | **Satu nilai final per nama**: porsi = yang terkecil dari plafon yang berlaku, dan plafon yang memotong disebut namanya. | usulan penulis |
| D7 | **Tanpa modal, hanya persen.** Rupiah, lot, dan rugi maksimum kosong. | penulis |
| D8 | **Pembulatan lot ke bawah**, 1 lot = 100 lembar. Nilai yang ditampilkan adalah nilai lot yang benar-benar bisa dibeli, bukan nilai teoretis. | penulis |
| D9 | **Hitungan dijalankan di backend sebagai fungsi murni, sesudah cache** (F9). Web mengirim keadaan yang dibutuhkan sebagai query: `modal`, `risk_pct`, `open_positions`, `exposure_pct`. | penulis, revisi 2 |
| D10 | **Rencana pembatalan tampil di blok yang sama** dengan porsinya (P2 §6.7). | sudah diputuskan di P2 |
| D11 | **Verdict ditahan berarti porsi ditahan.** Saat gerbang rezim menolak atau data basi, yang tampil hanya level dan sebabnya, bukan ukuran posisi. | P4 (F10) |
| D12 | **Keadaan portofolio diketik pemilik** di strip (jumlah posisi terbuka, eksposur sekarang). P6 yang menggantinya dengan catatan sebenarnya. | pemilik, 18 Okt 2026 |
| D13 | **Risiko serentak diturunkan, bukan input kelima**: `risiko serentak = posisi terbuka x risiko per transaksi`. Strip tetap empat kotak isian seperti mockup yang disetujui. | call penulis, bagian 4b |
| D14 | **Plafon jumlah posisi bekerja sebagai gerbang, bukan potongan**: kalau posisi terbuka sudah mencapai batas, porsi = 0 dan sebabnya ditulis ("sudah penuh"). | call penulis, bagian 4b |
| D15 | **Papan dan halaman emiten menampilkan angka yang sama.** Keempat angka keadaan ikut di kedua permintaan, jadi tidak mungkin satu nama punya dua porsi. | call penulis, bagian 4b |
| D16 | **Baris "Saran porsi X% modal" yang lama digantikan**, bukan didampingi. Satu nama, satu angka porsi. | call penulis, bagian 4b |
| D17 | **Batas portofolio terkunci: 6 posisi terbuka, risiko serentak 6% modal, eksposur total 80% modal.** | pemilik, 18 Okt 2026 (paket iii) |
| D18 | **Potongan rezim saat VOLATILE: separuh.** | pemilik, 18 Okt 2026 |

---

## 4b. Call penulis sesudah pemeriksa konteks segar

Lima lubang ditutup tanpa bertanya, karena jawabannya tidak mengubah apa yang dibangun:

1. **Risiko serentak (D13).** Menambah kotak isian kelima berarti menyimpang dari mockup yang sudah kamu setujui, dan menambah satu angka yang harus kamu isi tiap kali. Menurunkannya dari jumlah posisi x risiko juga bersikap hati-hati: kalau posisi lama berisiko lebih kecil dari 1%, turunannya melebih-lebihkan risiko, bukan menyepelekan.
2. **Plafon jumlah posisi (D14).** "Masih boleh masuk berapa nama lagi" adalah pertanyaan hitung, bukan persen: kalau buku penuh, jawabannya nol, dan potongan persen apa pun akan mengaburkan itu.
3. **Satu angka di semua permukaan (D15).** Ini menutup anti-tujuan "dua angka porsi berbeda untuk satu nama". Konsekuensinya endpoint menerima empat angka keadaan, bukan dua; harganya satu parameter tambahan, dan cache papan tidak tersentuh karena hitungannya sesudah cache (F9).
4. **Baris porsi lama digantikan (D16).** Kalau keduanya dibiarkan, kartu yang sama bisa menampilkan 16,0% di baris baru dan 16,0% di "Lihat detail" pada hari tenang, lalu berbeda saat ada plafon memotong. Satu angka, satu tempat.
5. **Urutan beli antar kandidat: tidak ada alokasi berurutan.** Mockup C sempat menggambar rantai "16% · berikutnya 13% · berikutnya 11%". Itu ilustrasi bentuk, bukan janji aturan: membagi modal ke nama yang belum tentu kamu beli bukan tugas lapis 3, dan hasilnya akan berubah tiap kali kamu menambah nama yang lain. Yang berlaku: tiap nama punya plafonnya sendiri, dan strip yang berkata berapa ruang sisa secara total.

---

## 5. Aturan yang diminta

### 5.1 Satu fungsi murni, satu aturan

```
position_plan(
  entry, stop,
  modal=None,               # rupiah; None = mode persen saja
  risk_pct=1.0,
  typical_day_value=None,   # median nilai transaksi 5 sesi terakhir nama itu (rupiah)
  vol_state=None,           # NORMAL | VOLATILE | QUIET
  open_positions=None,      # jumlah posisi terbuka (diketik pemilik)
  exposure_pct=None,        # eksposur sekarang, persen modal (diketik pemilik)
  limits=...,               # plafon: per nama, likuiditas, rezim, portofolio
)
```

Keluaran (persen selalu ada bila stop valid; sisanya hanya bila modal ada):

| Field | Aturan |
|---|---|
| `pct_raw` | `risk_pct / jarak_stop(%)` (aturan lama, tidak diubah) |
| `pct` | `min(pct_raw, plafon per nama, plafon likuiditas, plafon rezim, plafon portofolio)` |
| `binding_cap` | nama plafon yang benar-benar memotong, atau `null` kalau tidak ada |
| `rupiah` | `pct% x modal`, lalu dibulatkan ke bawah oleh jumlah lot |
| `lots` | `floor(rupiah / (100 x entry_ref))`, minimum 0 |
| `rupiah` (final) | `lots x 100 x entry_ref`, jadi angka di layar = yang benar-benar bisa dibeli |
| `risk_rupiah` | `lots x 100 x (entry_ref - stop)`, yaitu rugi kalau stop kena |
| `note` | kalimat pendek: plafon mana yang memotong, atau "sudah penuh", atau "modal kurang untuk 1 lot", atau "tanpa modal, hanya persen" |

**Plafon likuiditas (D5).** `plafon_pct = 2% x typical_day_value / modal x 100`. Nilai `typical_day_value` = **median nilai transaksi 5 sesi terakhir** (jendela yang sama dengan definisi tebal di P2 D4), supaya satu hari lonjakan tidak memberi izin posisi besar. Tanpa modal, plafon ini dilewati dan `note` mengatakannya.

**Plafon rezim (D18).** Saat `vol_state = VOLATILE`, porsi dipotong separuh. Saat gerbang rezim menolak, porsi ditahan sepenuhnya, dan itu urusan penahanan P4 (F10, D11), bukan fungsi murni ini.

**Plafon portofolio (D12, D13, D14, D17).** Batasnya 6 posisi terbuka, eksposur total 80% modal, risiko serentak 6% modal. Berpijak pada keadaan yang diketik pemilik:

- jumlah posisi terbuka sudah mencapai 6: porsi 0, `binding_cap = "portfolio"`, `note` berkata "sudah penuh" (D14);
- eksposur sekarang ditambah porsi baru akan melewati 80%: porsi dipotong sampai plafon itu saja;
- risiko serentak (jumlah posisi terbuka x risiko per transaksi) dipakai sebagai penanda di layar, bukan plafon terpisah (D13).

### 5.2 Yang ditampilkan, per permukaan

**(A) Kartu papan** mendapat satu baris di bawah baris harga beli / batas rugi / target:

```
Porsi 16,0%  ·  Rp 16.000.000  ·  1.410 lot
stop kena = rugi Rp 999.690 (1% modal)
```

Kalau ada plafon yang memotong: `Porsi 6,0% (plafon likuiditas)  ·  Rp 6.000.000  ·  529 lot`.
Tanpa modal: `Porsi 16,0% modal` (tanpa rupiah, tanpa lot).
Saat verdict ditahan (D11): baris ini tidak muncul, digantikan kalimat penahanan yang sudah ada.

**(B) Panel "Rencana posisi" di halaman emiten** berisi kotak isian modal dan risiko, lalu tabel: porsi, nilai beli, jumlah lot, rugi maksimum, plafon likuiditas, plafon rezim, dan baris penutup berisi aturan pembatalan (D10).

**(C) Strip portofolio di atas papan** berisi empat kotak isian (modal, risiko per transaksi, posisi terbuka, eksposur sekarang) dan angkanya: modal, risiko per transaksi, posisi terbuka (mis. 3 dari 6), eksposur, dan **ruang sisa = batas eksposur dikurangi eksposur sekarang** (bukan 100% dikurangi eksposur; mockup revisi 1 salah menulis 66% untuk eksposur 34% dengan batas 80%, angka yang benar 46%). Risiko serentak ditampilkan sebagai turunan, dengan penanda bahwa ia dihitung dari angka yang kamu isi sendiri.

### 5.3 Pemisahan dari penahanan (P4)

Penahanan verdict dan perhitungan posisi adalah dua hal berbeda dan tidak boleh saling menimpa:

1. Papan dihitung dan di-cache seperti sekarang.
2. Penahanan berlaku (data basi, atau gerbang menolak).
3. Baru sesudah itu rencana posisi dihitung untuk baris yang benar-benar tampil, memakai empat angka keadaan yang dikirim permintaan itu (D9).

Urutan ini yang menjaga cache tetap utuh, menjaga aturan D11 tetap dihormati, dan menjaga angka di papan dan di halaman emiten sama (D15).

---

## 6. Yang diserahkan ke pembangun

- Nama modul dan fungsi, bentuk payload, dan nama field JSON.
- Apakah `typical_day_value` dihitung di jalur `pool` yang sudah ada atau di `recommend.py` (asalkan satu tempat dan teruji).
- Bentuk kotak isian di web dan cara menyimpan state (hook `useLocalStorage` atau pola yang sudah dipakai `ThemeToggle`).
- Susunan tes: satu tes per aturan di bagian 5.1, satu tes tanpa modal (tidak ada rupiah/lot), satu tes plafon yang memotong menyebut namanya, satu tes papan dan halaman emiten menghasilkan angka yang sama.
- Cara menandai angka keadaan portofolio di layar sebagai "angka yang kamu isi sendiri" (D12).

---

## 7. Jawaban pemilik keputusan (18 Okt 2026)

| # | Pertanyaan | Jawaban | Akibat di aturan |
|---|---|---|---|
| Q1 | Batas portofolio, satu paket yang konsisten | **(iii) 6 posisi, risiko serentak 6%, eksposur 80%** | D17, bagian 5.1 plafon portofolio |
| Q2 | Potongan rezim saat VOLATILE | **separuh** | D18, bagian 5.1 plafon rezim |
| Q3 | Plafon likuiditas: median 5 sesi atau sesi terakhir | **median 5 sesi** | D5, bagian 5.1 plafon likuiditas |
| Q4 | Keadaan portofolio: diketik manual atau ditunda ke P6 | **diketik manual dulu** | D12, permukaan (C) |
| Q5 | Kunci simpan browser `ml-modal` dan `ml-risk-pct` | **ya** | D1, D3 |

Tidak ada pertanyaan terbuka tersisa.

---

## 8. Batas dan risiko

| # | Batas / risiko | Sikap |
|---|---|---|
| R1 | Tanpa modal, ukuran tidak bisa jadi rupiah. | Diterima sebagai mode: layar menampilkan persen saja dan mengatakannya. |
| R2 | Plafon portofolio berpijak pada angka yang diketik pemilik, bukan catatan posisi sebenarnya. | Dinyatakan di layar sebagai "angka yang kamu isi". Ditutup P6. |
| R3 | Plafon likuiditas memakai median nilai transaksi 5 sesi; nama yang likuiditasnya kering mendadak tidak terlihat. | Risiko diterima, dicatat. Bisa ditambah penanda "nilai sesi ini di bawah biasanya" di iterasi berikutnya. |
| R4 | Lot dibulatkan ke bawah, jadi porsi efektif selalu kurang atau sama dengan porsi teoretis. Di modal kecil, satu lot bisa melebihi porsi yang diizinkan. | Layar harus bisa berkata "modal kurang untuk 1 lot", bukan menampilkan 0 tanpa sebab. |
| R5 | Risiko serentak bukan jaminan kerugian maksimum: harga bisa melewati stop (gap). | Di layar disebut "risiko rencana", bukan janji. |
| R6 | Penyimpanan hanya di browser: hilang kalau ganti browser atau bersihkan data. | Dinyatakan di layar. |
| R7 | Risiko serentak diturunkan dari posisi terbuka x risiko per transaksi (D13), jadi ia melebih-lebihkan kalau posisi lama berisiko lebih kecil dari 1%. | Diterima: salah ke arah hati-hati, dan angkanya ditandai sebagai turunan, bukan catatan. |
| R8 | Angka posisi terbuka dan eksposur bisa basi begitu kamu membeli tanpa memperbarui strip. | Dinyatakan di layar. Ditutup P6 saat posisi benar-benar dicatat. |

---

## 9. Lampiran

**A. Angka contoh dari data nyata papan 18 Okt 2026** (sesi 2026-10-16)

PEGE: entry 113,42 / stop 106,33 / target 127,60. Jarak stop = (113,42 - 106,33) / 113,42 = 6,25%.
Risiko 1% modal, modal contoh Rp 100 juta:

| Langkah | Angka |
|---|---|
| porsi teoretis | 1% / 6,25% = 16,0% |
| nilai teoretis | 16,0% x Rp 100.000.000 = Rp 16.000.000 |
| lot (dibulatkan ke bawah) | 16.000.000 / (100 x 113,42) = 1.410,68 → **1.410 lot** |
| nilai final (yang benar-benar dibeli) | 1.410 x 100 x 113,42 = **Rp 15.992.220** |
| rugi kalau stop kena | 1.410 x 100 x 7,09 = **Rp 999.690** (0,9997% modal) |

Mockup yang disetujui menulis Rp 999.669. Itu salah hitung di mockup; rumus di tabel ini yang berlaku.

**Diperiksa ulang 18 Okt 2026 terhadap metrik DB nyata** (lewat fungsi produksi, bukan hitungan tangan):

- Plafon likuiditas PEGE = 2% x Rp 950.034.000 / Rp 100.000.000 = **19,0%**, jadi ia **tidak** memotong: seluruh angka tabel A berlaku apa adanya, tanpa koreksi.
- Angka tabel A berlaku untuk rezim **NORMAL**. Sesi berjalan 18 Okt 2026 berstatus `VOLATILE` (D18), jadi porsi PEGE nyata terpotong separuh: 8,0% / 705 lot / Rp 7.996.110 / rugi Rp 499.845. Kartu menampilkan angka final yang benar-benar dibeli (Rp 15.992.220 pada keadaan NORMAL), bukan nilai teoretis Rp 16.000.000.
- Label risiko di baris rugi adalah **rugi nyata dibagi modal**, jadi ia bisa lebih kecil dari risiko yang diketik ketika ada plafon memotong (contoh: risiko 1%, pasar VOLATILE → 0,5% modal).
- D6 berlaku harfiah saat dua plafon memotong bersamaan: yang disebut adalah plafon yang menyentuh angka akhir (buku penuh saat pasar VOLATILE → `portfolio`, bukan `regime`).

**B. Mockup yang dipilih pemilik**

`idx-scraper/.superpowers/brainstorm/570-1792320980/content/posisi-card.html` (tiga permukaan, angka contoh yang sama dengan lampiran A).

**C. Bukti yang harus ada sebelum P5 dinyatakan selesai**

1. Tes fungsi murni untuk setiap baris tabel bagian 5.1, termasuk kasus tanpa modal, kasus plafon memotong, dan kasus buku penuh.
2. Angka nyata yang diperiksa ulang pada data sesi 2026-10-16 (kolam 191, kandidat 46, PEGE seperti lampiran A).
3. Tiga permukaan terlihat di render nyata, bukan hanya HTTP 200.
4. Papan dan halaman emiten menghasilkan porsi yang identik untuk nama yang sama (D15).
5. Suite penuh lulus dan `ruff check` bersih, seperti standar P1 sampai P4.
