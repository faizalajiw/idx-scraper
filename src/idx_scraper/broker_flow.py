"""Kategori broker (asing / lokal / BUMN) & komposisi nilai transaksi pasar.

Kenapa klasifikasi dikurasi di backend
--------------------------------------
Kategori bandarmologi perlu SATU sumber kebenaran untuk semua halaman. Sumbernya
map kurasi **kode** broker (bukan nama): kode adalah kunci yang tersimpan di
``research.broker_daily``, stabil lintas sesi, dan pendek (2 huruf) sehingga
tidak mungkin salah match seperti pencarian kata kunci nama ("Mandiri" bisa
muncul di nama firma mana pun).

Map di bawah diverifikasi dari pasangan (kode, nama) asli yang tersimpan di
``research.broker_daily`` (dump 2026-09-29, 88 firma). Endpoint
``/api/broker-flow`` mengembalikan kategori tiap broker sesi terakhir
(``classification``) supaya klasifikasi bisa diaudit dari UI, bukan ditebak.

Batasan data yang dipegang jujur:

- ``research.broker_daily`` adalah TOTAL transaksi per firma — IDX tidak
  mempublikasikan split beli/jual per firma di endpoint publik. Yang bisa
  dihitung adalah **komposisi nilai transaksi** (turnover share), BUKAN net
  buy/sell per kategori. Jangan mengklaim arah aliran dari angka ini.
- Kode broker bisa berpindah pemilik (contoh nyata: ``YP`` pernah tercatat
  "Valbury", sekarang "Mirae Asset"). Map dikurasi per 2026 dan harus
  diperiksa ulang kalau nama firma berubah.

Modul ini sengaja bebas dari akses DB (hanya menerima iterable baris) supaya
bisa dites tanpa Postgres — pola yang sama dengan ``research.broker_activity``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

# Urutan kanonik kategori — dipakai UI & skema respons.
CATEGORIES: tuple[str, ...] = ("asing", "lokal", "bumn")

# Firma asing (kantor pusat di luar Indonesia) yang aktif di DB saat ini.
# Pasangan kode -> nama diverifikasi dari research.broker_daily 2026-09-29.
FOREIGN_BROKER_CODES: frozenset[str] = frozenset(
    {
        "AK",  # UBS Sekuritas Indonesia (Swiss)
        "YU",  # CGS International Sekuritas Indonesia (Tiongkok)
        "BK",  # J.P. Morgan Sekuritas Indonesia (AS)
        "KZ",  # CLSA Sekuritas Indonesia (Hong Kong)
        "CP",  # KB Valbury Sekuritas (Korea)
        "ZP",  # Maybank Sekuritas Indonesia (Malaysia)
        "YP",  # Mirae Asset Sekuritas Indonesia (Korea)
        "RX",  # Macquarie Sekuritas Indonesia (Australia)
        "BQ",  # Korea Investment and Sekuritas Indonesia (Korea)
        "HD",  # KGI Sekuritas Indonesia (Taiwan)
        "TP",  # OCBC Sekuritas Indonesia (Singapura)
        "XA",  # NH Korindo Sekuritas Indonesia (Korea)
        "DR",  # RHB Sekuritas Indonesia (Malaysia)
        "AG",  # Kiwoom Sekuritas Indonesia (Korea)
        "AI",  # Kay Hian Sekuritas (Singapura / UOB)
        "FS",  # Yuanta Sekuritas Indonesia (Taiwan)
        "DP",  # DBS Vickers Sekuritas Indonesia (Singapura)
        "AH",  # Shinhan Sekuritas Indonesia (Korea)
        "GI",  # Webull Sekuritas Indonesia (Tiongkok/Singapura)
    }
)

# Catatan: kode asing historis (mis. CS = Credit Suisse, sudah merger dengan
# UBS) sengaja TIDAK dimasukkan tanpa verifikasi — kode yang tidak ada di map
# aman jatuh ke "lokal" hanya salah kategori ringan, sedangkan kode yang
# salah masuk kategori asing mengklaim pemilik asing yang tidak ada.

# Sekuritas milik BUMN Indonesia. Empat kode pertama aktif di DB saat ini;
# tiga terakhir kode historis yang pernah muncul di payload IDX (fixture lama,
# saudara kandungnya masih berdagang) — dipertahankan agar riwayat tetap benar.
BUMN_BROKER_CODES: frozenset[str] = frozenset(
    {
        "CC",  # Mandiri Sekuritas
        "NI",  # BNI Sekuritas
        "OD",  # BRI Danareksa Sekuritas
        "DX",  # Bahana Sekuritas
        "MM",  # Mandiri Sekuritas (kode historis)
        "BV",  # BNI Sekuritas (kode historis)
        "DA",  # Danareksa Sekuritas (kode historis)
    }
)

def classify_broker(code: str | None) -> str:
    """Kode broker -> ``"asing"`` | ``"bumn"`` | ``"lokal"`` (pure).

    Kode dinormalisasi (trim + uppercase). Kode yang tidak ada di map dianggap
    lokal — default yang aman karena mayoritas firma memang lokal, dan kode
    baru tak dikenal tidak boleh ikut kategori khusus tanpa bukti.
    """
    if not code:
        return "lokal"
    c = str(code).strip().upper()
    if c in FOREIGN_BROKER_CODES:
        return "asing"
    if c in BUMN_BROKER_CODES:
        return "bumn"
    return "lokal"


def _value(r: Mapping[str, Any]) -> float:
    v = r.get("value")
    if v is None:
        return 0.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def composition(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Komposisi nilai transaksi per kategori untuk SATU sesi (pure).

    ``rows``: baris ``research.broker_daily`` satu tanggal (dict berisi minimal
    ``broker_code`` dan ``value``). Baris tanpa kode dilewati; nilai kosong
    dihitung 0 tapi tetap masuk hitungan firma.

    Returns dict ``n_brokers, total_value, categories`` — kategori berisi
    ``value, share, n_brokers``. ``share`` None kalau total tidak positif
    (tidak ada data vs memang nol dibedakan lewat ``total_value`` None).
    """
    total = 0.0
    any_value = False
    by_cat: dict[str, dict[str, Any]] = {
        c: {"value": 0.0, "share": None, "n_brokers": 0} for c in CATEGORIES
    }
    n = 0
    for r in rows:
        code = r.get("broker_code")
        if not code:
            continue
        cat = classify_broker(str(code))
        v = _value(r)
        if v != 0.0:
            any_value = True
        by_cat[cat]["value"] += v
        by_cat[cat]["n_brokers"] += 1
        total += v
        n += 1

    out: dict[str, Any] = {
        "n_brokers": n,
        "total_value": total if any_value else None,
        "categories": by_cat,
    }
    if total > 0:
        for c in CATEGORIES:
            by_cat[c]["share"] = by_cat[c]["value"] / total
    return out


def top_brokers_by_category(
    rows: Iterable[Mapping[str, Any]],
    top_n: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """Firma terbesar per kategori dalam satu sesi (pure).

    ``share`` dihitung terhadap TOTAL pasar (konsisten dengan
    :func:`composition`), bukan terhadap kategorinya sendiri, supaya angka yang
    sama punya arti yang sama di semua kartu UI.
    """
    total = sum(_value(r) for r in rows if r.get("broker_code"))
    buckets: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
    for r in rows:
        code = r.get("broker_code")
        if not code:
            continue
        cat = classify_broker(str(code))
        v = _value(r)
        buckets[cat].append(
            {
                "broker_code": str(code),
                "broker_name": r.get("broker_name"),
                "value": v,
                "share": (v / total) if total > 0 else None,
            }
        )
    for c in CATEGORIES:
        buckets[c].sort(key=lambda x: x["value"], reverse=True)
        buckets[c] = buckets[c][: max(0, top_n)]
    return buckets


def composition_series(
    rows_by_date: Mapping[str, Iterable[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Tren komposisi per tanggal, urut tanggal menaik (pure).

    ``rows_by_date``: tanggal ISO -> baris broker tanggal itu. Returns daftar
    ``date, total_value, {cat}_value, {cat}_share`` — bentuk pipih agar tinggal
    ditempel ke chart recharts.
    """
    out: list[dict[str, Any]] = []
    for date in sorted(rows_by_date):
        comp = composition(rows_by_date[date])
        row: dict[str, Any] = {
            "date": date,
            "total_value": comp["total_value"],
        }
        for c in CATEGORIES:
            row[f"{c}_value"] = comp["categories"][c]["value"]
            row[f"{c}_share"] = comp["categories"][c]["share"]
        out.append(row)
    return out
