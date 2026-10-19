# Perbaikan: papan kandidat tanpa warm-up + hitung-ulang serentak + penutup pool bocor

Branch: `fix/api-warmup-stampede` (dibuat dari `main` `0a62a63`).
Ditulis 19 Okt 2026, sesudah temuan operasional P6 (§"Jembatan yang belum ditutup" nomor 9).

## Kenapa

Tiga sebab, semuanya terukur hari ini:

1. **Papan kandidat tidak pernah dihangatkan.** Warm-up API jalan sejak dulu
   (lima cache: event study, broker snapshot, screener, valuasi, hold check),
   tapi kelimanya tidak termasuk `get_recommendations`, jadi halaman
   /rekomendasi tetap membayar penuh di kunjungan pertama. Terukur di port uji
   sesudah papan ditambahkan: permintaan yang datang saat warm-up jalan dijawab
   **44,3 detik** (dilayani oleh hitungan warm-up, bukan menghitung ulang),
   permintaan berikutnya **0,57 detik**, dan angka dingin papan sendirian
   **44 detik** (konteks rekomendasi 43,3 detik di antaranya).

   **Koreksi diagnosis.** Catatan pertama hari ini menyebut "warm-up tidak pernah
   jalan sekali pun sejak API ditulis ulang dari Streamlit" karena
   `_warm_heavy_caches` dipasangi `@asynccontextmanager` tanpa `yield`. Itu salah.
   `contextlib` memanggil fungsinya saat context manager dibuat
   (`_GeneratorContextManagerBase.__init__`: `self.gen = func(*args, **kwds)`),
   jadi bodynya dieksekusi dan kelima thread hangatnya memang menyala. Buktinya
   ada di log yang sudah lama ada: `_api.log` memuat **130** baris
   `[warmup] ... siap`, lima di antaranya sesudah penanda "Application startup
   complete" proses yang hidup sekarang (baris 29.292, sedangkan baris warm-up di
   29.343-29.366). Yang rusak dari dekorator itu adalah pulangannya: context
   manager dengan `gen=None`, yang meledak begitu ada yang menulis `async with`.
   Dekoratornya tetap dibuang, tapi sebagai bom waktu - bukan sebagai penyebab
   warm-up yang mati.

2. **Cache dingin dihitung ulang oleh setiap permintaan yang datang bersamaan.**
   `/api/recommendations` dingin terukur 67 detik; lewat HTTP tidak selesai dalam
   240 detik karena enam koneksi browser menghitung papan yang sama secara
   paralel, masing-masing menahan satu koneksi pool (`IDX_API_POOL_MAX` = 10)
   sementara threadpool uvicorn 40. Pool habis, `/api/market/overview` kena
   `PoolTimeout` 30 detik, lima backend tertinggal `ClientWrite` 276-356 detik,
   dan seluruh API jadi tidak bisa dihubungi.

3. **Penutup pool tidak pernah terpasang di jalur non-CLI.** `api/database.py`
   membuka pool dan tidak menutupnya, jadi proses pendek keluar dengan
   `PythonFinalizationError: cannot join thread at interpreter shutdown`
   (repro: `python -c` yang memanggil `analytics.get_market_regime()` keluar
   exit 1 + jejak).

## Keputusan desain (diambil penulis, bukan hasil tanya)

- **Dekorator `@asynccontextmanager` dibuang** (dan importnya ikut), bukan diganti
  dengan menulis `async with` di `lifespan`. Badannya memang fire-and-forget
  (docstring lama sudah bilang begitu), jadi dekorator itu salah sejak awal -
  yang dipulangkannya CM rusak `gen=None`. `lifespan` cukup berupa async
  generator biasa. Tes `test_warm_heavy_caches_fungsi_biasa` menjaga supaya
  dekoratornya tidak kembali.
- **Papan ikut dihangatkan** dengan parameter yang sama dengan yang dipanggil web
  (`useRecommendations(limit = 20, minGrade = "C")` di `lib/hooks.ts`), karena
  key cache-nya `recommendations:{limit}:{min_grade}`. Sebelum ini papan tidak
  dihangatkan sama sekali.
- **Single-flight** (hitung sekali, penunggu ikut hasilnya) ditambahkan sebagai
  helper baru `research_cache_once` di `api/analytics.py`, dipakai di dua jalur
  termahal: `get_recommendations` (key `recommendations:20:C`) dan
  `_recommendation_context` (key `recommendation_context`, dipakai bersama papan,
  halaman emiten, gerbang, dan job rekaman).
- **Gagal-terbuka**: kalau yang menghitung mati atau lebih lambat dari batas
  tunggu 180 detik, penunggu menghitung sendiri. Tidak ada jalur yang menahan
  permintaan selamanya, dan tidak ada perubahan bentuk keluaran.
- **`clear_research_cache` tidak ikut membersihkan daftar in-flight.** Yang
  sedang dihitung boleh selesai; pemanggil berikutnya tetap melihat cache yang
  baru saja dibersihkan. Ini tidak mengubah perilaku refresh serve loop (cache
  dibersihkan, permintaan sesudahnya menghitung ulang sekali).
- **`atexit.register(close_pool)` dipindah ke dalam `init_pool()`** supaya semua
  entrypoint bersih, bukan cuma `main()` CLI. `close_pool()` idempoten (dijaga
  `if _pool is not None`), jadi pendaftaran di CLI pada P6 tidak bentrok sesudah
  kedua branch bertemu.
- **Warm-up dijalankan BERURUTAN di satu thread, papan paling depan.** Kode lama
  menjalankan lima cache sebagai lima thread bersamaan; begitu papan ditambahkan
  menjadi enam, dan percobaan itu hasilnya buruk: di port uji, permintaan
  `/api/recommendations` yang datang saat warm-up jalan menunggu **298 detik**
  karena keenam hitungan berat berebut CPU dan pool 10 koneksi, sehingga warm-up
  papannya sendiri jadi lambat dan penunggunya kehabisan batas tunggu 180 detik
  lalu menghitung ulang. Berurutan menghapus rebutan itu dan menaruh cache yang
  paling sering dipakai di depan; event bundle ditaruh paling belakang.
  (Yang tidak diukur: perilaku lima thread lama tanpa papan. Yang terukur cuma
  enam-versus-berurutan, keduanya dengan papan.)
- **Baris warm-up dicetak lewat `_catat()` yang me-flush setiap baris.** Di
  percobaan kedua, log proses uji hanya memuat dua dari enam baris walau rantai
  warm-up-nya jelas selesai (direproduksi di dalam proses, dengan penanda waktu:
  papan 46,8 detik, broker 1,3 detik, valuasi 14,2 detik, screener 20,7 detik,
  hold check 12,8 detik, semua selesai). Sebabnya buffer stdout: begitu stdout
  diarahkan ke berkas atau pipa (cara `serve` menjalankannya) Python memakai
  buffer blok, jadi `print` tanpa `flush` tertahan di memori dan hilang kalau
  prosesnya dibunuh. Log yang bisa kehilangan barisnya sendiri tidak layak jadi
  bukti, jadi ini ditambal sebelum merge.

## Yang tidak dikerjakan di sini

- **Batas waktu tulis respons** (kasus `ClientWrite` 276-356 detik). Obat akarnya
  butuh keputusan tersendiri (batas tulis di server HTTP atau pool yang lebih
  besar dari jumlah thread). Single-flight menutup pemicunya, bukan akibatnya.
  Tetap terbuka di peta jalan.
- **Hitung-sekali untuk jalur berat lain** (event study ~2-3 menit, valuasi,
  screener, hold check). Hanya jalur papan dan konteks rekomendasi yang dijaga
  di sini, karena itu yang terukur menghabiskan pool. Kandidat berikutnya kalau
  ada keluhan: bungkus `_event_study_bundle` dengan `research_cache_once` yang
  sama. Belum dikerjakan supaya perubahan perilakunya tetap sempit.
- **Overflow kartu kandidat di 390 px** (temuan nomor 8, keputusan pemilik:
  paket sendiri).

## Langkah

1. **`api/app.py`**: hapus dekorator + import, tambah `_run_board` dan `_catat`,
   jalankan keenam tugas berurutan di satu thread. Selesai kalau
   `_warm_heavy_caches` adalah fungsi biasa dan menyalakan satu thread bernama
   `warmup`.
2. **`api/analytics.py`**: tambah `research_cache_once` + pindahkan badan
   `get_recommendations` ke `_board_uncached` dan badan `_recommendation_context`
   ke `_recommendation_context_uncached`. Selesai kalau bentuk keluaran kedua
   fungsi tidak berubah (tes lama hijau) dan hitungan dingin hanya sekali untuk
   banyak pemanggil bersamaan.
3. **`api/database.py`**: `atexit.register(close_pool)` di `init_pool`.
   Selesai kalau `python -c` yang memakai API keluar tanpa jejak
   `cannot join thread at interpreter shutdown`.
4. **Tes baru** `tests/test_api_warmup.py`: warm-up menyalakan satu thread
   berurutan dengan papan paling depan, `_catat` selalu flush, helper
   single-flight menghitung sekali untuk banyak pemanggil, `init_pool`
   mendaftarkan penutup, dan papan dingin dilayani bergiliran (pakai fungsi
   palsu, tanpa DB).
5. **Verifikasi E2E tanpa menyentuh `:8000`**: jalankan uvicorn kedua di port
   8010 dengan kode baru, lihat keenam baris `[warmup] ... siap` muncul di
   lognya, dan ukur permintaan pertama `/api/recommendations` apa adanya.

## Pengukuran pemisah biaya dingin (19 Okt 2026, 13:41 WIB, proses terpisah, cache dikosongkan)

```
CTX 43.3s          # _recommendation_context() dingin
BOARD_TOTAL 44.7s  # get_recommendations(limit=20, min_grade='C') dingin
BOARD_REST 1.3s    # sisa papan setelah konteksnya hangat
rows 20 pool 191 cand 46   # sama dengan angka P5 (pool 191, kandidat 46)
```

Artinya 43 dari 45 detik itu konteks rekomendasi, dan itulah yang dipakai bersama
papan, halaman emiten, gerbang, dan job rekaman. Proses yang sama juga keluar
dengan **exit code 1** + `PythonFinalizationError: cannot join thread at
interpreter shutdown` dari `ConnectionPool.__del__`, yaitu repro temuan nomor 8
sebelum tambalan akarnya dipasang.

## Pengukuran dingin per item warm-up (19 Okt 2026, 14:10 WIB, satu proses per item)

| Item | Dingin terukur | Catatan lama (dugaan) |
| --- | --- | --- |
| papan kandidat (`get_recommendations`) | 44 s | 45-67 s |
| broker snapshot (`broker_activity_snapshot`) | 1,3 s | ~15 s |
| valuasi (`get_valuation`) | 14,2 s | ~10 s |
| baris screener (`_screener_rows`) | 20,7 s | ~15-20 s |
| hold check (`get_hold_check`) | 12,8 s | ~7 s |
| event bundle (`_event_study_bundle`) | 184,1 s | ~2 menit |

Total rantai warm-up berurutan kalau semuanya dingin: **~4,6 menit**. Itu tidak
terasa di layar karena warm-up jalan di belakang: permintaan yang datang di
tengah warm-up dihitung oleh permintaan itu sendiri (kecuali papan dan konteks
rekomendasi, yang ikut menunggu hitungan warm-up karena ada hitung-sekali).

## E2E akhir (19 Okt 2026, 14:21-14:27 WIB, uvicorn kedua di port 8010)

Kode branch ini, cache kosong, `:8000` tidak disentuh:

```
SIAP        3.1s
REQ_A       200  43.28s   # datang saat warm-up jalan, dilayani hitungan warm-up
REQ_B       200   0.44s
OVERVIEW    200   0.37s
ROWS        20194 bytes / 20194 bytes   # identik
```

Urutan selesainya warm-up di dalam app (detik sejak proses hidup, dibaca dari log
yang sekarang di-flush - keenam barisnya ada):

| Item | Selesai di detik |
| --- | --- |
| papan kandidat | 47,3 |
| broker snapshot | 47,3 |
| valuasi | 62,4 |
| baris screener | 92,5 |
| hold check | 107,5 |
| event bundle | 336,5 |

Event bundle butuh 229 detik di dalam app (184,1 detik sendirian) karena berbagi
CPU dan DB dengan permintaan yang masuk.

Catatan lama di komentar kode ternyata dugaan semua; yang ini terukur, dan
angkanya sudah menggantikan dugaan itu di komentar kode. Urutan warm-up
(papan, broker, valuasi, screener, hold check, event bundle) jadi masuk akal
berdasar biaya: yang paling murah dan paling sering dipakai lebih dulu, yang
paling mahal terakhir.

Dua koreksi yang datang dari pengukuran ini:

1. **"Valuasi belasan menit" salah.** Sempat tampak begitu karena log hanya
   memuat dua baris dalam 900 detik; ternyata itu masalah buffer (lihat
   keputusan `_catat`). Valuasi seluruh pasar (980 emiten, satu query per
   emiten) hanya 14,2 detik dingin.
2. **Satu query per emiten bisa 30 detik** kalau datang saat pipeline refresh
   memegang lock (`select count(*) ... where code = 'BBRI'` = 30,036 detik,
   sedangkan dengan cache hangat query yang sama 1,2 ms lewat
   `idx_prices_pit_asof`). Bukan penyebab hari ini, tapi ini penjelasan kalau
   halaman Valuasi pernah terasa sangat lambat.

Catatan probe: repro pertama (proses yang memanggil `_warm_heavy_caches` dengan
`faulthandler.dump_traceback_later` menyala) mati dengan `ACCESS_VIOLATION`
(`-1073741819`) ketika event bundle sedang berjalan di dalam pandas. Dugaan
utama: thread penulis jejak faulthandler, bukan event bundle-nya - fungsi yang
sama tanpa faulthandler selesai **184,1 detik dan keluar exit 0**, dan proses API
yang hidup sekarang memuat `[warmup] event bundle siap` lalu tetap melayani
permintaan. Dugaan itu belum dibuktikan dengan repro kedua; kalau muncul lagi
tanpa faulthandler, ini naik jadi temuan tersendiri di peta jalan.


## Bukti yang harus ada sebelum merge

- `pytest` hijau (jumlahnya dilaporkan apa adanya, sebelum dan sesudah) dan
  `ruff check src tests scripts` bersih.
- Log proses uvicorn port 8010 memuat **keenam** baris `[warmup] ... siap`
  (catatan: di kode lama hanya lima, papan tidak ada; log yang di-force-kill
  bisa kehilangan baris karena buffer, jadi `_catat` me-flush).
- Waktu permintaan pertama `/api/recommendations` di port 8010, apa adanya.
- Pengukuran pemisah biaya dingin (konteks vs sisa papan) dari proses terpisah.
