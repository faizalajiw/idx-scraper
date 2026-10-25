"""Service layer — kesehatan data (gap sesi, cakupan kolom, jumlah baris)
dan kesegaran data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...health import (
    count_sessions_between,
    evaluate_field_coverage,
    evaluate_health,
    evaluate_row_counts,
    evaluate_session_gaps,
    expected_trading_date,
    parse_holidays,
)
from ..database import get_cursor
from .inti import WIB


def get_data_health(sessions: int = 30, row_threshold_pct: float = 50.0) -> dict[str, Any]:
    """Ringkasan sehat/tidaknya data EOD untuk kartu "Kesehatan Data".

    Menjawab satu pertanyaan: boleh percaya angka di dashboard? Empat pemeriksa
    read-only yang semua logikanya ada di ``idx_scraper.health`` (murni, sudah
    diuji jaringan-, DB-, dan time bebas di ``tests/test_health.py`` &
    ``tests/test_data_health.py``):

    1. kesegaran — tanggal EOD terbaru vs hari bursa yang diharapkan
    2. cakupan kolom ``foreign_*`` per sesi (jendela ``sessions`` sesi)
    3. jumlah baris per sesi vs median — sesi "separuh" karena interupsi insert
    4. lubang hari bursa di antara sesi pertama & terakhir ``raw_eod``

    Pemeriksa 3 & 4 melewati sesi hari ini (``in_progress``): saat ingest sore
    berjalan, sesi yang belum lengkap itu bukan kerusakan.
    """
    now = datetime.now(WIB)
    holidays = parse_holidays()

    with get_cursor() as cur:
        # 1. Kesegaran — sumber sama dengan `idx health` (prices_pit + close resmi).
        cur.execute("select max(trade_date) as d from research.prices_pit")
        stocks_latest = cur.fetchone()["d"]
        cur.execute(
            "select max((captured_at at time zone 'Asia/Jakarta')::date) as d "
            "from index_quotes where code = 'COMPOSITE' "
            "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16"
        )
        index_latest = cur.fetchone()["d"]

        # 2-3. Satu query untuk cakupan kolom foreign & jumlah baris per sesi.
        cur.execute(
            "select trade_date, count(*) as total, count(foreign_net) as terisi "
            "from research.raw_eod group by 1 order by 1"
        )
        coverage_rows = cur.fetchall()
        tanggal = [r["trade_date"] for r in coverage_rows]

        # Dasar "kalender bursa": union hari IHSG (Yahoo, riwayat penuh) +
        # tanggal close COMPOSITE (IDX, jendela lebih pendek). Kosong = evaluator
        # jatuh ke dasar "hari-kerja".
        cur.execute(
            "select distinct d from ("
            "  select (captured_at at time zone 'Asia/Jakarta')::date d "
            "  from public.index_quotes where code = 'COMPOSITE'"
            "  union select date d from public.index_summary_daily"
            ") x where d is not null and extract(isodow from d) < 6"
        )
        bursa_days = [r["d"] for r in cur.fetchall()]

    expected = expected_trading_date(now, holidays)
    fres = evaluate_health(expected, stocks_latest, index_latest, now)
    # Berapa sesi bursa yang tertinggal per sumber. `overdue` dari
    # `evaluate_health` cuma menyala di hari kerja setelah jam batas, jadi di
    # akhir pekan data yang basi 3 minggu pun akan terbaca "menunggu" tanpa angka
    # ini — inilah yang membedakan "sesi hari ini belum masuk" dari "tertinggal".
    stocks_behind = count_sessions_between(bursa_days, stocks_latest, expected, holidays)
    index_behind = count_sessions_between(bursa_days, index_latest, expected, holidays)
    field_cov = evaluate_field_coverage(
        [(r["trade_date"], r["total"], r["terisi"]) for r in coverage_rows], sessions
    )
    row_cov = evaluate_row_counts(
        [(r["trade_date"], r["total"]) for r in coverage_rows],
        sessions,
        row_threshold_pct,
        in_progress=now.date(),
    )
    # Sesi hari ini (masih mungkin ditulis saat ingest berjalan) juga dilewati di
    # sini, sama seperti di `idx health` & job `health_eod`: kartu ini menjawab
    # "ada yang rusak?", bukan "ingest sedang jalan?".
    gaps = evaluate_session_gaps(tanggal, bursa_days, holidays, in_progress=now.date())

    freshness = {
        "expected": fres.expected.isoformat(),
        "eod_latest": fres.stocks_latest.isoformat() if fres.stocks_latest else None,
        "index_latest": fres.index_latest.isoformat() if fres.index_latest else None,
        "overdue": fres.overdue,
        "missing": fres.missing,
        "stocks_behind": stocks_behind,
        "index_behind": index_behind,
    }
    # `ok` = bersih DAN lengkap (tak ada yang tertunda, tak ada yang rusak) —
    # bukan "tak ada yang perlu ditindak" ala `EodHealth.ok`, yang cuma menyalakan
    # `overdue`. Bedanya tampak tiap hari kerja sebelum ±16:15 WIB: sesi hari ini
    # memang belum ada, `freshness.overdue` masih False, dan kartu menampilkannya
    # sebagai "menunggu", bukan alarm. Yang butuh "perlu tindakan?" baca
    # `freshness.overdue`. `checked > 0` menutup satu-satunya jalan `ok: true`
    # sementara tidak ada yang benar-benar diperiksa (mis. `raw_eod` kosong).
    ok = (
        (not fres.missing)
        and field_cov.checked > 0
        and field_cov.ok
        and row_cov.checked > 0
        and row_cov.ok
        and gaps.ok
    )
    return {
        "checked_at": now,
        "ok": ok,
        "freshness": freshness,
        "field_coverage": {
            "checked": field_cov.checked,
            "worst_pct": field_cov.worst_pct,
            "troublemakers": field_cov.troublemakers,
        },
        "row_counts": {
            "checked": row_cov.checked,
            "worst_pct": row_cov.worst_pct,
            "troublemakers": row_cov.troublemakers,
            "median_rows": row_cov.median_rows,
            "threshold_pct": row_cov.threshold_pct,
        },
        "session_gaps": {
            "first": gaps.first.isoformat() if gaps.first else None,
            "last": gaps.last.isoformat() if gaps.last else None,
            "sessions": gaps.sessions,
            "missing": [d.isoformat() for d in gaps.missing],
            "basis": gaps.basis,
        },
    }


def get_freshness() -> dict[str, Any]:
    """Kesegaran tiga-status untuk badge permukaan (spec D1).

    Sengaja tipis: perhitungannya hidup di `idx_scraper.freshness` (murni,
    teruji tanpa DB), dan badge di tiap halaman cuma butuh dua query
    `max(...)` — bukan empat pemeriksa milik `/api/data-health`.
    """
    from ... import freshness

    with get_cursor() as cur:
        return freshness.current(cur)
