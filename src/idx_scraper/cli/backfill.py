"""Kalender bursa + backfill hari lampau + jaring EOD sore."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import dotenv

from .eod import (
    _clear_api_cache,
    _eod_fresh_for,
    _job_index_eod_close,
    _job_pipeline_refresh,
    _simpan_eod_hari,
)
from .fetch import WIB

dotenv.load_dotenv()


from ..client import (
    IDXClient,
)
from ..logging_setup import say


def _hari_bursa(day: date) -> bool:
    """True kalau ``day`` hari bursa: Sen–Jum dan bukan libur ``IDX_HOLIDAYS``.

    Tanpa cek libur, jaring EOD sore menjalankan pipeline penuh di hari libur
    nasional (data pasar memang tidak ada) — menit terbuang plus alarm palsu.
    """
    if day.weekday() >= 5:
        return False
    from ..health import parse_holidays

    return day not in parse_holidays()


_BACKFILL_HARI_DEFAULT = 21


_BACKFILL_HARI = int(os.getenv("IDX_BACKFILL_DAYS") or _BACKFILL_HARI_DEFAULT)


def _hari_bursa_mundur(mulai: date, jumlah: int) -> list[date]:
    """``jumlah`` hari bursa terakhir yang ``<= mulai``, terbaru dulu (murni).

    Batas langkah sengaja dipasang: cukup melewati akhir pekan + libur panjang,
    tapi loop tidak bisa berputar tanpa akhir kalau ``IDX_HOLIDAYS`` diisi aneh.
    """
    out: list[date] = []
    for i in range(jumlah * 4 + 14):
        d = mulai - timedelta(days=i)
        if _hari_bursa(d):
            out.append(d)
            if len(out) == jumlah:
                break
    return out


def _backfill_hari_lampau(
    client: IDXClient, storage, *, label: str, lapor_lubang_index: bool = False
) -> int:
    """Tarik ulang EOD hari bursa lampau yang bolong. Kembalikan jumlah hari terisi.

    Kenapa perlu: ``_eod_catchup_hari_ini`` hanya melihat HARI INI. Kalau mesin
    tidur atau mati melewati seluruh jendela sore (16:10–22:30), hari itu tidak
    pernah kembali dengan sendirinya — dan ``idx health`` pun tidak menangkapnya,
    karena ia membandingkan tanggal TERBARU, bukan lubang di tengah. Fungsi ini
    menutupnya dari dua kesempatan yang sudah ada: startup ``serve`` dan jaring
    sore ``eod_fallback``.

    Cakupan jujur:

    - **Saham** ditarik di sini (``fetch_stock_summary`` melayani tanggal lampau,
      ``_simpan_eod_hari`` menulis ketiga tujuan tulisnya).
    - **Close resmi indeks** tidak: satu-satunya sumbernya endpoint IDX live
      (``GetIndexList``) yang selalu berarti "hari ini", jadi tanggal lampau
      hanya bisa ditambal manual — lihat catatan di README. ``lapor_lubang_index``
      menyalakan satu baris laporan untuk itu (dipakai jalur startup saja supaya
      log sore tidak berisik).
    - **Menu turunan** (regime, jejak sinyal, rekomendasi, broker) disegarkan
      sekali di akhir kalau ada yang ditarik; jendela langganan mereka 1 bulan,
      jadi tanggal yang baru masuk ikut terhitung.

    Biaya saat tidak ada lubang: satu query DB per tanggal (≈ nol). Tanggal yang
    IDX balas 0 baris tidak diingat-ingat — cek berikutnya boleh mencoba lagi,
    karena 0 baris bisa berarti libur tak terdaftar atau IDX sedang menolak.

    Jendelanya default 21 hari bursa (``IDX_BACKFILL_DAYS``). Lubang lebih tua
    dari itu tidak ditarik di sini — pakai skrip manual
    ``python -m scripts.check_eod_gaps`` (lihat runbook outage di README).
    """
    if _BACKFILL_HARI <= 0:
        return 0
    sekarang = datetime.now(WIB)
    kandidat = [
        d
        for d in _hari_bursa_mundur(sekarang.date(), _BACKFILL_HARI)
        if d != sekarang.date()  # hari ini sudah ditangani `_eod_catchup_hari_ini`
    ]
    if not kandidat:
        return 0

    kosong: list[date] = []
    tanpa_index: list[date] = []
    for day in kandidat:
        ada_saham, ada_index = _eod_fresh_for(day)
        if not ada_saham:
            kosong.append(day)
        if not ada_index:
            tanpa_index.append(day)

    if lapor_lubang_index and tanpa_index:
        say(
            f"[catchup] {label}: {len(kandidat)} hari bursa lampau dicek, "
            f"{len(tanpa_index)} di antaranya tanpa close indeks resmi "
            f"({', '.join(str(d) for d in tanpa_index)}) — close indeks hanya "
            "tersedia dari endpoint IDX live, jadi tanggal lampau tidak bisa "
            "ditarik ulang otomatis"
        )
    if not kosong:
        return 0

    terisi = 0
    for day in kosong:
        try:
            baris = _simpan_eod_hari(client, storage, day)
        except Exception as e:
            say(f"[err] backfill {day}: {e}", err=True)
            continue
        if baris:
            terisi += 1
            say(f"[catchup] {label}: EOD {day} ditarik ulang — {baris} emiten")
        else:
            say(
                f"[warn] {label}: IDX membalas 0 baris untuk {day} — "
                "boleh dicoba lagi di cek berikutnya",
                err=True,
            )
    if terisi:
        # Tanpa EOD: tidak ada sesi pasar baru, yang perlu jalan hanya recompute
        # menu turunan. Lock lintas-job tetap dipakai — kalau rantai 16:10 masih
        # berjalan, recompute rantai itu sudah mencakup pekerjaan ini.
        _job_pipeline_refresh(client, storage, label=f"backfill {label}", do_eod=False)
    return terisi


def _eod_catchup(
    client: IDXClient, storage, *, label: str, lapor_lubang_index: bool = False
) -> str:
    """Pastikan EOD lengkap; kerjakan yang kurang. Satu definisi.

    Dipakai dua pemanggil — job ``eod_fallback`` (tiap jam 16:30–20:30, di dalam
    ``serve``) dan startup catchup ``serve`` — supaya keduanya memakai definisi
    "sudah lengkap" yang sama (``_eod_fresh_for``) dan tidak saling meniarakan
    pekerjaan.

    Dua fase: hari ini dulu, lalu hari bursa lampau yang bolong
    (``_backfill_hari_lampau``) — lihat alasannya di docstring fungsi itu.

    Kembalikan hasil fase HARI INI: ``bukan-hari-bursa`` | ``pipeline`` |
    ``indeks`` | ``lengkap``.
    """
    hasil = _eod_catchup_hari_ini(client, storage, label=label)
    _backfill_hari_lampau(client, storage, label=label, lapor_lubang_index=lapor_lubang_index)
    return hasil


def _eod_catchup_hari_ini(client: IDXClient, storage, *, label: str) -> str:
    """Fase "hari ini" dari ``_eod_catchup`` (dipisah supaya fase lampau selalu ikut).

    Kembalikan ``bukan-hari-bursa`` | ``pipeline`` | ``indeks`` | ``lengkap``.
    """
    now_wib = datetime.now(WIB)
    if not _hari_bursa(now_wib.date()) or now_wib.hour < 16:
        say(f"[catchup] {label}: dilewati (pasar belum tutup atau bukan hari bursa)")
        return "bukan-hari-bursa"
    ada_saham, ada_indeks = _eod_fresh_for(now_wib.date())
    if not ada_saham:
        say(
            f"[catchup] {label}: EOD saham {now_wib:%Y-%m-%d} belum ada — "
            "menjalankan pipeline penuh..."
        )
        _job_pipeline_refresh(client, storage, label=f"catchup {label}", do_eod=True)
        return "pipeline"
    if not ada_indeks:
        say(
            f"[catchup] {label}: close resmi COMPOSITE {now_wib:%Y-%m-%d} belum ada — "
            "menjalankan index EOD close..."
        )
        _job_index_eod_close()
        _clear_api_cache()
        return "indeks"
    say(f"[catchup] {label}: EOD hari ini sudah lengkap (saham OK, index OK)")
    return "lengkap"


def _job_eod_fallback(client: IDXClient, storage) -> None:
    """Jaring kedua EOD — Sen–Jum, tiap jam 16:30–20:30 WIB, DI DALAM ``serve``.

    Menggantikan task Windows ``IDX-EOD`` (dihapus 15 Okt 2026): penjadwalan
    hanya di satu tempat, jadi tidak ada lagi dua pipeline yang bisa bertabrakan
    di sore hari. Kalau rantai 16:10 berhasil, job ini keluar dalam ~1 detik
    (satu query DB, tanpa request IDX). Kalau gagal, ia menjalankan pipeline
    penuh — menutup jendela penolakan IDX yang lebih panjang daripada retry
    60/180 detik di dalam rantai.

    Diulang tiap jam karena mesin bisa tidur melewati slot 16:10: proses ``serve``
    ikut beku (bukan mati), dan ``misfire_grace_time`` 2 jam membuat cek terakhir
    menyala begitu mesin bangun — sepanjang masih sebelum ~22:30.
    """
    _eod_catchup(client, storage, label="ulang EOD sore")
