"""smart_money: pecahan api/analytics.py (FAI-19)."""

from __future__ import annotations

from typing import Any

from ..database import get_cursor
from ..sector_map import FALLBACK_SECTOR, sector_for
from ._common import _f, _research_cache_get, _research_cache_put, logger
from .decision import _market_return

#: Sejarah untuk track record pola: cukup untuk 10-sesi jendela deteksi
#: + 21-sesi horizon forward terlama + margin, dengan cukup kejadian supaya
#: hit rate & alpha-nya bermakna. 120 sesi ≈ 6 bulan bursa.
_SMART_MONEY_TRACK_HISTORY = 120

_RADAR_TOP_N = 15

#: Berapa sesi bursa ditarik untuk konteks streak (lebih panjang dari jendela
#: agregasi, supaya streak bisa menghitung mundur melewati jendela).
_SMART_MONEY_HISTORY = 30


def _smart_money_panel(history_days: int) -> dict[str, dict[str, Any]]:
    """Panel baris harian per emiten untuk N sesi terakhir (satu query).

    Returns ``{code: {"name": ..., "rows": [...]}}`` — baris sudah dalam
    bentuk yang dibutuhkan ``smart_money`` (``date, net_idr, value, close,
    high, low, volume``), urut tanggal menaik per emiten. Baris dengan close
    NULL dilewati (tidak ada harga yang bisa dikalikan).
    """
    from ...smart_money import _f as _fnum

    with get_cursor() as cur:
        cur.execute(
            """select code, name, trade_date as date,
                      foreign_net * close as net_idr,
                      value, close, high, low, volume
               from research.latest_pit
               where trade_date in (
                   select distinct trade_date from research.latest_pit
                   order by 1 desc limit %s
               )
                 and close is not null
               order by code, trade_date""",
            (history_days,),
        )
        panel: dict[str, list[dict[str, Any]]] = {}
        names: dict[str, str] = {}
        for r in cur.fetchall():
            code = str(r["code"]).upper()
            names.setdefault(code, r["name"])
            panel.setdefault(code, []).append(
                {
                    "date": str(r["date"]),
                    "net_idr": _fnum(r["net_idr"]),
                    "value": _fnum(r["value"]),
                    "close": _fnum(r["close"]),
                    "high": _fnum(r["high"]),
                    "low": _fnum(r["low"]),
                    "volume": _fnum(r["volume"]),
                }
            )
    return {c: {"name": names.get(c), "rows": rows} for c, rows in panel.items()}


def get_smart_money_radar(days: int = 10) -> dict[str, Any]:
    """Radar smart money: emiten dengan jejak aliran terkuat di seluruh pasar.

    Sumber ``research.latest_pit``; logika peringkat di modul pure
    ``idx_scraper.smart_money.build_radar`` (dites tanpa DB). Output dibagi
    dua daftar — ``accumulation`` (net buy) dan ``distribution`` (net sell) —
    masing-masing top-``limit`` berdasar |net rupiah|. Cache 30 menit (data
    harian, tidak berubah intraday).
    """
    from ...smart_money import build_radar

    days = max(1, min(days, 60))
    cache_key = f"smart_money_radar:{days}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_HISTORY)
    ranked = build_radar(panel, days=days)
    accumulation = [r for r in ranked if r["side"] == "akumulasi"][:_RADAR_TOP_N]
    distribution = [r for r in ranked if r["side"] == "distribusi"][:_RADAR_TOP_N]

    out: dict[str, Any] = {
        "days": days,
        "date": ranked[0]["date"] if ranked else None,
        "accumulation": accumulation,
        "distribution": distribution,
        "scanned": len(panel),
    }
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def get_smart_money_breadth() -> dict[str, Any] | None:
    """Sebaran verdict SELURUH pasar (jendela 10 sesi) — pembanding verdict.

    Dipakai jalur per-emiten untuk menjawab "emiten ini istimewa atau ikut
    arus?". Memakai panel pasar yang sama dengan radar (satu query), tanpa
    lantai likuiditas, dan aturan verdict yang sama dengan per-emiten. Cache
    30 menit — harian, dan query-nya pasar-lebar, jadi jalur per-emiten tidak
    boleh membayarnya berulang.
    """
    from ...smart_money import market_breadth

    cache_key = "smart_money_breadth"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_HISTORY)
    out = market_breadth(panel)
    if out is not None:
        _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def get_smart_money_patterns_board() -> dict[str, Any]:
    """Papan "pola terkuat hari ini": pola yang menyala di sesi terakhir.

    Menggabungkan deteksi pola se-pasar (panel yang sama dengan radar) dengan
    track record pola (cache 1 jam) supaya tiap kelompok membawa buktinya —
    keyakinan, horizon, dan berapa persen arahnya benar. Cache 30 menit.

    Ini yang membuat `initiation` (pola dengan bukti terkuat, tapi hanya muncul
    saat volume melonjak) kelihatan tanpa harus menunggu ia kebetulan menyala
    di emiten yang sedang dibuka.
    """
    from ...smart_money import patterns_board

    cache_key = "smart_money_patterns_board"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_HISTORY)
    tr = get_smart_money_track_record()
    out = patterns_board(
        panel,
        tr.get("patterns") or [],
        window_sessions=tr.get("history_sessions"),
    )
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def get_stock_smart_money(code: str) -> dict[str, Any]:
    """Jejak smart money satu emiten: verdict + pola + level + narasi.

    Gabungan dari modul pure ``idx_scraper.smart_money``:
    - ``verdict`` (akumulasi/distribusi/netral + streak + ukuran)
    - ``detect_patterns`` (pola klasik yang terdeteksi)
    - ``consolidation_range`` (level pembatalan)
    - ``build_narrative`` (kalimat plain-language untuk banner)
    - ``sector`` konteks: berapa emiten se-sektor yang akumulasi vs distribusi

    Semua dari ``research.latest_pit`` (60 sesi terakhir). Cache 30 menit.
    Emiten tanpa data -> struktur kosong yang aman di-render (``has_data``
    False) supaya UI menampilkan empty state, bukan error.
    """
    from ...smart_money import (
        build_narrative,
        consolidation_range,
        detect_patterns,
        market_context_sentence,
        patterns_history,
        rank_patterns,
    )
    from ...smart_money import (
        verdict as _verdict,
    )

    code = code.strip().upper()
    cache_key = f"smart_money_stock:{code}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    empty: dict[str, Any] = {
        "code": code,
        "has_data": False,
        "verdict": None,
        "patterns": [],
        "range": None,
        "narrative": [],
        "sector": None,
        "date": None,
    }

    with get_cursor() as cur:
        cur.execute(
            """select code, name, trade_date as date,
                      foreign_net * close as net_idr,
                      value, close, high, low, volume
               from (
                   select * from research.latest_pit
                   where code = %s and close is not null
                   order by trade_date desc limit 60
               ) t order by trade_date""",
            (code,),
        )
        rows = cur.fetchall()

    if not rows:
        return empty

    panel_rows = [
        {
            "date": str(r["date"]),
            "net_idr": _f(r["net_idr"]),
            "value": _f(r["value"]),
            "close": _f(r["close"]),
            "high": _f(r["high"]),
            "low": _f(r["low"]),
            "volume": _f(r["volume"]),
        }
        for r in rows
    ]

    v = _verdict(panel_rows)
    pats = detect_patterns(panel_rows)
    rng = consolidation_range(panel_rows)

    # Arti historis: pola yang muncul HARI INI ditempeli track record-nya,
    # supaya label ("Distribusi diam-diam") jadi klaim yang bisa dicek, dan
    # horizon-nya ikut terbawa — pola akumulasi bercerita ~1 bulan, pola
    # distribusi ~1 minggu. Track record di-cache 1 jam (hitungan mahal).
    # Kegagalan di sini tidak boleh mematikan banner: pola tetap tampil,
    # hanya tanpa klaim historis.
    hist: dict[str, Any] = {}
    try:
        tr = get_smart_money_track_record()
        hist = patterns_history(
            pats,
            tr.get("patterns") or [],
            window_sessions=tr.get("history_sessions"),
        )
        for p in pats:
            h = hist.get(str(p.get("id")))
            if h is not None:
                p["history"] = h
    except Exception:  # graceful degradation — banner tetap hidup
        hist = {}

    # Urutan tampil ikut kekuatan bukti: pola dengan catatan paling bisa
    # dipercaya tampil lebih dulu, bukan pola yang kebetulan diperiksa dulu.
    pats = rank_patterns(pats, hist)

    # Konteks pasar: pembanding supaya verdict tidak dibaca sebagai ciri khas
    # emiten padahal cuma cermin pasar (hari ini mayoritas pasar bisa net sell).
    breadth: dict[str, Any] | None = None
    try:
        breadth = get_smart_money_breadth()
    except Exception as e:  # graceful degradation — banner tetap hidup
        logger.warning(f"[warn] konteks pasar dilewati di smart-money: {e}")
        breadth = None

    narrative = build_narrative(code, v, pats, rng, pattern_history=hist, market=breadth)

    # Konteks sektor: verifikasi apakah emiten ini "janggal" dibanding
    # se-sektor (berapa yang akumulasi vs distribusi di jendela sama).
    sector_ctx: dict[str, Any] | None = None
    sector = sector_for(code)
    try:
        if sector and sector != FALLBACK_SECTOR:
            sector_ctx = _sector_flow_context(code, sector)
    except Exception as e:
        logger.warning(f"[warn] konteks sektor dilewati di smart-money: {e}")
        sector_ctx = None

    out: dict[str, Any] = {
        "code": code,
        "name": rows[-1].get("name"),
        "has_data": not v["insufficient"],
        "verdict": v,
        "patterns": pats,
        "range": rng,
        "narrative": narrative,
        "sector": {
            "sector": sector,
            "comparable": sector != FALLBACK_SECTOR,
            **sector_ctx,
        }
        if sector_ctx is not None
        else None,
        "market": (
            {**breadth, "position": market_context_sentence(v["side"], breadth)}
            if breadth
            else None
        ),
        "date": v["date"],
    }
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def _sector_flow_context(code: str, sector: str) -> dict[str, Any] | None:
    """Berapa emiten se-sektor yang akumulasi vs distribusi (jendela 10).

    Kode anggota sektor dari ``sector_map`` (kurasi + generated map); panel
    30 sesi terakhir hanya untuk kode itu (filter di SQL, bukan tarik semua
    pasar). Returns ``{sector_total, accumulating, distributing, position}``
    — ``position`` kalimat plain-language, mis. "1 dari 8 emiten perbankan
    yang akumulasi". None jika anggota ber-data < 3.
    """
    from ...smart_money import (
        VERDICT_ACCUMULATION,
        VERDICT_DISTRIBUTION,
        VERDICT_NEUTRAL,
        verdict,
    )
    from .sector_map import SECTOR_MAP, generated_map

    sector_codes = sorted(
        {c.upper() for c, s in SECTOR_MAP.items() if s == sector}
        | {c.upper() for c, s in generated_map().items() if s == sector}
    )
    if code.upper() not in sector_codes:
        # map tidak memetakan emiten ini ke sektor tsb (override manual bisa
        # terjadi di UI); sertakan supaya konteks tidak kehilangan subjeknya.
        sector_codes.append(code.upper())
    if not sector_codes:
        return None

    with get_cursor() as cur:
        cur.execute(
            """select code, trade_date as date,
                      net_idr,
                      value, close, high, low, volume
               from (
                   select code, trade_date, close, value, high, low,
                          volume, foreign_net * close as net_idr,
                          row_number() over (
                              partition by code order by trade_date desc
                          ) as rn
                     from research.latest_pit
                    where close is not null and code = any(%s)
               ) t
               where t.rn <= 30
               order by code, trade_date""",
            (sector_codes,),
        )
        all_rows = cur.fetchall()

    by_code: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        c = str(r["code"]).upper()
        by_code.setdefault(c, []).append(
            {
                "date": str(r["date"]),
                "net_idr": _f(r["net_idr"]),
                "value": _f(r["value"]),
                "close": _f(r["close"]),
                "high": _f(r["high"]),
                "low": _f(r["low"]),
                "volume": _f(r["volume"]),
            }
        )

    acc = dist = 0
    for rows in by_code.values():
        if not rows:
            continue
        v = verdict(rows)
        if v["insufficient"]:
            continue
        if v["side"] == VERDICT_ACCUMULATION:
            acc += 1
        elif v["side"] == VERDICT_DISTRIBUTION:
            dist += 1

    total = len(by_code)
    if total < 3:
        return None

    self_v = verdict(by_code.get(code.upper(), []))
    if self_v["insufficient"] or self_v["side"] == VERDICT_NEUTRAL:
        position = (
            f"Di sektor {sector}, {acc} emiten akumulasi & {dist} distribusi "
            f"(dari {total} ber-data)."
        )
    elif self_v["side"] == VERDICT_ACCUMULATION:
        position = (
            f"Satu dari {acc} emiten di sektor {sector} yang sedang akumulasi "
            f"(dari {total} ber-data)."
        )
    else:
        position = (
            f"Satu dari {dist} emiten di sektor {sector} yang sedang distribusi "
            f"(dari {total} ber-data)."
        )

    return {
        "sector_total": total,
        "accumulating": acc,
        "distributing": dist,
        "position": position,
    }


def get_smart_money_track_record() -> dict[str, Any]:
    """Track record pola klasik jejak smart money — "pola ini terbukti?"

    Mengumpulkan kejadian pola dari 120 sesi terakhir seluruh pasar
    (:func:`collect_pattern_episodes`), memberi forward return T+1
    (disiplin ``research.signal_log``) + alpha vs pasar equal-weight, lalu
    agregasi via :func:`pattern_track_record`. Ini layer "apakah pola
    ini bisa dipercaya" — melengkapi banner/radar yang hanya memberi
    kondisi hari ini.

    Cache 1 jam (hitungan mahal & harian; tak berubah intraday).
    """
    from ...smart_money import collect_pattern_episodes, pattern_track_record

    cache_key = "smart_money_track_record"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_TRACK_HISTORY)

    # Indeks (code, date) -> index baris, supaya lookup T (hari kejadian)
    # dan forward return O(1) per episode, bukan scan ulang per emiten.
    # Forward return tetap dihitung di hari *bursa* (urut baris per emiten),
    # bukan hari kalender.
    idx: dict[tuple[str, str], int] = {}
    for code, entry in panel.items():
        rows = entry["rows"]
        for j, r in enumerate(rows):
            idx[(code, r["date"])] = j

    closes: dict[str, list[dict[str, Any]]] = {code: entry["rows"] for code, entry in panel.items()}

    episodes = collect_pattern_episodes(panel)

    # Return pasar equal-weight per (tanggal kejadian, horizon) — dihitung
    # sekali lalu dipakai semua episode di tanggal/horizon yang sama.
    market: dict[tuple[str, int], float | None] = {}

    for ep in episodes:
        code, date = ep["code"], ep["date"]
        i = idx.get((code, date))
        rows = closes.get(code)
        if i is None or not rows:
            continue
        # entry = close di T+1 (hari bursa berikutnya).
        if i + 1 >= len(rows):
            continue
        entry = rows[i + 1].get("close")
        if entry is None or entry <= 0:
            continue
        for h in (1, 5, 10, 21):
            j = i + 1 + h
            exit_ = rows[j].get("close") if j < len(rows) else None
            if exit_ is None:
                ep[f"fwd{h}"] = None
                ep[f"abn{h}"] = None
                continue
            fwd = (exit_ / entry - 1.0) * 100.0
            ep[f"fwd{h}"] = fwd
            mkey = (date, h)
            if mkey not in market:
                market[mkey] = _market_return(closes, idx, date, h)
            mkt = market[mkey]
            ep[f"abn{h}"] = (fwd - mkt) if mkt is not None else None

    out = pattern_track_record(episodes)
    result = {
        "patterns": out,
        "n_episodes": len(episodes),
        "history_sessions": _SMART_MONEY_TRACK_HISTORY,
        "horizon_note": "Forward return entry T+1 (hari bursa), exit T+1+horizon.",
    }
    _research_cache_put(cache_key, result, ttl=3600.0)
    return result


def get_smart_money_verdicts(codes: list[str]) -> list[dict[str, Any]]:
    """Verdict jejak smart money untuk daftar kode (satu query, read-only).

    Dipakai worker alert: di tiap refresh EOD, verdict per emitan di-watchlist
    dihitung dari panel 30 sesi lalu diproses lewat :class:`SmartMoneyState`
    untuk mem-filter transisi bermakna. Logika pure di ``smart_money``; fungsi
    ini hanya query + orkestrasi. Cache 30 menit (sama dengan radar).

    Returns daftar ``{code, name, verdict, patterns, range}`` untuk kode yang
    punya data (kode tanpa data dilewati). Urutan mengikuti input.
    """
    from ...smart_money import (
        consolidation_range,
        detect_patterns,
    )
    from ...smart_money import (
        verdict as _verdict,
    )

    if not codes:
        return []
    codes = [c.upper() for c in codes]

    cache_key = f"smart_money_verdicts:{','.join(sorted(codes))}"
    cached = _research_cache_get(cache_key)
    if cached is not None:
        return cached

    panel = _smart_money_panel(_SMART_MONEY_HISTORY)

    out: list[dict[str, Any]] = []
    for code in codes:
        entry = panel.get(code)
        if not entry or not entry["rows"]:
            continue
        rows = entry["rows"]
        v = _verdict(rows)
        patterns = detect_patterns(rows)
        rng = consolidation_range(rows)
        out.append(
            {
                "code": code,
                "name": entry.get("name"),
                "verdict": v,
                "patterns": patterns,
                "range": rng,
            }
        )
    _research_cache_put(cache_key, out, ttl=1800.0)
    return out


def get_smart_money_watchlist(codes: list[str]) -> dict[str, Any]:
    """Ringkasan verdict jejak smart money per emiten watchlist (halaman Pantau).

    Bentuk baris yang siap dirender: ``code, name, side, insufficient,
    net_sum_idr, netval_pct, streak, date, patterns`` (label pola). Tidak ada
    hitungan baru — hanya memangkas ``get_smart_money_verdicts`` supaya kartu
    UI tidak menerima struktur berat (narasi, range).
    """
    items = get_smart_money_verdicts(codes)
    rows: list[dict[str, Any]] = []
    for d in items:
        v = d.get("verdict") or {}
        rows.append(
            {
                "code": d["code"],
                "name": d.get("name"),
                "side": v.get("side"),
                "insufficient": bool(v.get("insufficient")),
                "net_sum_idr": v.get("net_sum_idr"),
                "netval_pct": v.get("netval_pct"),
                "streak": v.get("streak"),
                "date": v.get("date"),
                "patterns": [p.get("label") for p in (d.get("patterns") or [])],
            }
        )
    return {"n": len(rows), "rows": rows}
