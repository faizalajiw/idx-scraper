"""Sumber berita Market Labs — satu-satunya: Stockbit Snips.

Semua feed RSS lain (CNN Indonesia, Tempo, Bloomberg Technoz, Republika, dst.)
SUDAH DIHAPUS atas permintaan pemilik produk. Jangan tambahkan kembali tanpa
persetujuan eksplisit.

`pluang.com/akademi/berita-analisis` sudah dievaluasi dan DITOLAK: seluruh request
scraper (termasuk robots.txt) dijawab 403 oleh edge Akamai-nya, hanya lolos kalau
header browser dipalsukan. Jangan tambahkan.

Catatan akses:
  * `snips.stockbit.com` menyajikan daftar snips langsung di HTML (server-side),
    jadi tidak butuh API atau JS. robots.txt hanya melarang jalur `/api/`,
    `/search`, `/config`, `/static/`, dan `?format=json`.
  * `stockbit.com/StockbitNews` TIDAK bisa di-scrape: isinya di-render client-side
    lewat `api.stockbit.com` yang menolak klien non-app (InvalidParameter), dan
    tidak ada RSS maupun sitemap publik. Karena itu kartu "MarLabs News" memakai
    snips terbaru dari sumber yang sama, diurutkan per tanggal.

Scraping tetap wilayah abu-abu: frekuensi rendah (default 15 menit), UA normal,
dan setiap item tautan balik ke halaman asalnya.
"""

SOURCE = "Stockbit Snips"
BASE_URL = "https://snips.stockbit.com"

# Semua blok (Snips Terbaru, Stockbit Research, Investasi, dst.) diambil; urutan
# "terbaru" ditentukan oleh tanggal, bukan posisi blok.
