"""Rute aliran: flow asing, jejak smart money, dossier, kepemilikan."""

from __future__ import annotations

from fastapi import Query, Response

from .. import alerts, analytics
from ..schemas import (
    ForeignFlow,
    SmartMoneyPatternsBoard,
    SmartMoneyRadar,
    SmartMoneyStock,
    SmartMoneyTrackRecord,
    SmartMoneyWatchList,
    StockForeignFlow,
    StockOwnership,
)
from ._shared import Router, _resolve_codes

route = Router()


@route.get("/api/foreign-flow", response_model=ForeignFlow)
def foreign_flow(days: int = Query(default=20, ge=1, le=120)) -> dict:
    return analytics.get_foreign_flow(days=days)


@route.get("/api/stocks/{code}/foreign-flow", response_model=StockForeignFlow)
def stock_foreign_flow(
    code: str,
    days: int = Query(
        default=30, ge=5, le=120, description="Jendela tren harian emiten (hari bursa)"
    ),
    peer_days: int = Query(default=10, ge=1, le=60, description="Jendela pembanding sektor"),
    peer_limit: int = Query(default=10, ge=3, le=30, description="Maks peer yang ditampilkan"),
) -> dict:
    """Aliran asing satu emiten: tren harian, flip arah, dan peer sektor.

    Net flow dalam rupiah (``foreign_net`` saham * close). Flip = hari tanda
    net berubah vs hari non-nol sebelumnya. Peer sektor dari map kurasi;
    ``comparable`` False menandakan bucket fallback "Lainnya".
    """
    return analytics.get_stock_foreign_flow(
        code.upper(), days=days, peer_days=peer_days, peer_limit=peer_limit
    )


@route.get("/api/smart-money/radar", response_model=SmartMoneyRadar)
def smart_money_radar(
    days: int = Query(default=10, ge=1, le=60, description="Jendela agregasi (hari bursa)"),
) -> dict:
    """Radar jejak smart money: emiten dengan aliran asing terkuat di pasar.

    Dua daftar (akumulasi & distribusi) top-N berdasar |net rupiah| di
    jendela N sesi terakhir, plus lantai likuiditas (nilai transaksi jendela
    >= Rp 500 Jt) supaya emiten yang nyaris tak diperdagangkan tidak masuk
    papan. Logika peringkat di modul pure ``smart_money.build_radar``.
    """
    return analytics.get_smart_money_radar(days=days)


@route.get("/api/stocks/{code}/smart-money", response_model=SmartMoneyStock)
def stock_smart_money(code: str) -> dict:
    """Jejak smart money satu emiten: verdict, pola klasik, level, dan narasi.

    Jawaban plain-language atas "pemain besar lagi masuk/keluar sejak kapan
    dan di level mana". Verdict dari porsi nilai transaksi yang dibelani
    asing (bukan skor kuantitatif); level = rentang konsolidasi 20 sesi;
    konteks sektor = berapa emiten se-sektor yang searah. Cache 30 menit.
    """
    return analytics.get_stock_smart_money(code.upper())


@route.get("/api/stocks/{code}/dossier")
def stock_dossier(code: str) -> dict:
    """Dossier analisis satu emiten: gabungan sumber yang sudah ada, dengan tanggal
    sesi dan penanda stale per bagian. Tidak menghitung skor baru."""
    from . import dossier

    return dossier.build_dossier(code)


@route.get("/api/stocks/{code}/dossier.pdf")
def stock_dossier_pdf(code: str) -> Response:
    """Export dossier satu emiten sebagai PDF (data sama dengan endpoint dossier)."""
    from . import dossier, dossier_pdf

    data = dossier.build_dossier(code)
    pdf = dossier_pdf.render_dossier_pdf(data)
    filename = f"dossier-{data['code']}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@route.get("/api/stocks/{code}/ownership", response_model=StockOwnership)
def stock_ownership(code: str) -> dict:
    """Kepemilikan emiten + aksi pemilik (gratis, keterbukaan IDX).

    Komposisi pemegang saham terkini (termasuk pengendali & free float) dan
    perubahan porsi vs snapshot sebelumnya. Sumber: ``research.ownership``
    (diisi ``idx ownership``). Free float di sini juga bahan untuk menakar
    seberapa besar aliran asing relatif terhadap saham yang benar-benar
    beredar.
    """
    return analytics.get_stock_ownership(code.upper())


@route.get("/api/smart-money/track-record", response_model=SmartMoneyTrackRecord)
def smart_money_track_record() -> dict:
    """Track record pola klasik jejak smart money (120 sesi terakhir).

    Berapa kali tiap pola muncul, dan forward return-nya (entry T+1) plus
    alpha vs pasar — menjawab "pola ini terbukti tidak". Agregasi di modul
    pure ``smart_money``; forward return + alpha dihitung di service
    ``analytics``. Cache 1 jam.
    """
    return analytics.get_smart_money_track_record()


@route.get("/api/smart-money/patterns", response_model=SmartMoneyPatternsBoard)
def smart_money_patterns_board() -> dict:
    """Papan "pola terkuat hari ini": pola yang menyala di sesi terakhir.

    Dikelompokkan per pola (angka historisnya milik pola, bukan emiten) dan
    diurutkan berdasar kekuatan bukti, sehingga `initiation` — pola dengan
    catatan terkuat tapi jarang menyala — terlihat tanpa harus kebetulan
    muncul di emiten yang sedang dibuka. Emiten di dalam tiap kelompok urut
    |net rupiah| dengan lantai likuiditas yang sama seperti radar. Cache 30
    menit.
    """
    return analytics.get_smart_money_patterns_board()


@route.get("/api/smart-money/verdicts", response_model=SmartMoneyWatchList)
def smart_money_verdicts(
    codes: str | None = Query(
        default=None, description="Comma-separated tickers; default = watchlist"
    ),
) -> dict:
    """Verdict jejak smart money per emiten watchlist (kartu halaman Pantau).

    Menampilkan kondisi sekarang (ditimbun / dibuang / seimbang) supaya user
    tahu baseline sebelum alert Telegram menyala saat verdict berubah.
    """
    data = analytics.get_smart_money_watchlist(_resolve_codes(codes))
    return {**data, "telegram_enabled": alerts.telegram_enabled()}
