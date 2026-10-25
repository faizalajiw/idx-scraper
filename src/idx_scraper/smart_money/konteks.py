"""Smart money — konteks pasar, papan pola, narasi awam, dan radar.

Lapisan penyajian: menerjemahkan hasil ``inti`` dan ``pola`` jadi
kalimat, papan, dan baris radar yang siap dipakai UI/alert.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .inti import (
    LARGE_NETVAL_PCT,
    MEANINGFUL_NETVAL_PCT,
    PRICE_WINDOW,
    RADAR_MIN_WINDOW_VALUE,
    VERDICT_ACCUMULATION,
    VERDICT_DISTRIBUTION,
    VERDICT_NEUTRAL,
    _clean_rows,
    _f,
    _window_stats,
    verdict,
)
from .pola import (
    _CONFIDENCE_ORDER,
    PATTERNS,
    detect_patterns,
    pattern_evidence_sentence,
    pattern_history,
)

# ------------------------------------------------------------------ konteks pasar


def market_breadth(
    emitters: Mapping[str, Mapping[str, Any]],
    days: int = PRICE_WINDOW,
) -> dict[str, Any] | None:
    """Sebaran verdict SELURUH pasar di jendela yang sama (pure).

    Tanpa pembanding, verdict "distribusi besar" di hari ketika mayoritas
    pasar dibuang asing terbaca seperti ciri khas emiten — padahal cuma cermin
    pasar. Fungsi ini yang menyediakan penyebutnya.

    Aturan verdict-nya SAMA PERSIS dengan jalur per-emiten (``verdict``,
    fungsi yang sama) dan **tanpa lantai likuiditas** — beda dari
    ``build_radar`` yang memotong emiten tipis untuk papan peringkat. Di sini
    semua emiten ber-data dihitung, supaya penyebutnya jujur.

    Returns ``{date, total, accumulating, distributing, neutral, insufficient,
    accumulating_pct, distributing_pct, distributing_share}`` atau None bila
    tak ada emiten ber-data. ``*_pct`` = porsi terhadap seluruh emiten ber-data;
    ``distributing_share`` = porsi distribusi di antara emiten yang PUNYA arah
    (netral/insufficient keluar dari penyebut) — itu pembanding yang benar
    untuk sebuah verdict, karena "mayoritas pasar" harus dibaca dari emiten
    yang memang bersuara.
    """
    acc = dist = neu = ins = 0
    date: str | None = None
    for info in emitters.values():
        rows = (info or {}).get("rows") or []
        if not rows:
            continue
        v = verdict(rows, days=days)
        d = v.get("date")
        if d:
            date = d if date is None else max(date, str(d))
        if v["insufficient"]:
            ins += 1
        elif v["side"] == VERDICT_ACCUMULATION:
            acc += 1
        elif v["side"] == VERDICT_DISTRIBUTION:
            dist += 1
        else:
            neu += 1
    total = acc + dist + neu + ins
    if total == 0:
        return None
    sided = acc + dist
    return {
        "date": date,
        "total": total,
        "accumulating": acc,
        "distributing": dist,
        "neutral": neu,
        "insufficient": ins,
        "accumulating_pct": round(acc / total * 100.0, 1),
        "distributing_pct": round(dist / total * 100.0, 1),
        "distributing_share": round(dist / sided * 100.0, 1) if sided else None,
        "sided": sided,
    }


def market_context_sentence(
    side: str,
    breadth: Mapping[str, Any] | None,
) -> str | None:
    """Kalimat pembanding pasar untuk verdict satu emiten (plain-language).

    Menjawab "emiten ini istimewa atau ikut arus?". Verdict yang searah
    mayoritas pasar dinyatakan apa adanya — itu bukan sinyal khas emiten, dan
    menyembunyikannya membuat fitur ini terdengar lebih pintar dari kenyataan.
    """
    if not breadth:
        return None
    share = _f(breadth.get("distributing_share"))
    if share is None:
        return None
    dist_pct = share
    acc_pct = 100.0 - dist_pct
    if side == VERDICT_DISTRIBUTION:
        if dist_pct >= 50.0:
            body = (
                f"searah mayoritas pasar — {dist_pct:.0f}% emiten ber-verdict "
                "juga sedang dibuang asing, jadi ini belum tentu ciri khas "
                "emiten ini"
            )
        else:
            body = (
                f"melawan arus pasar — hanya {dist_pct:.0f}% emiten ber-verdict "
                "yang sedang dibuang asing"
            )
    elif side == VERDICT_ACCUMULATION:
        if acc_pct >= 50.0:
            body = (
                f"searah mayoritas pasar — {acc_pct:.0f}% emiten ber-verdict "
                "juga sedang ditimbun asing"
            )
        else:
            body = (
                f"melawan arus pasar — hanya {acc_pct:.0f}% emiten ber-verdict "
                "yang sedang ditimbun asing"
            )
    else:
        return (
            f"Arus asing pasar terbelah: {dist_pct:.0f}% emiten ber-verdict "
            f"dibuang, {acc_pct:.0f}% ditimbun."
        )
    return f"Konteks pasar: {body}."


# ------------------------------------------------------------------ papan pola

#: Berapa emiten yang ditampilkan per pola di papan "pola hari ini" (sisanya
#: diringkas jadi hitungan — 66 emiten dalam satu baris tidak terbaca).
PATTERNS_BOARD_TOP_N = 8


def patterns_board(
    emitters: Mapping[str, Mapping[str, Any]],
    track_record: Iterable[Mapping[str, Any]],
    *,
    window_sessions: int | None = None,
    days: int = PRICE_WINDOW,
    min_window_value: float = RADAR_MIN_WINDOW_VALUE,
    top_n: int = PATTERNS_BOARD_TOP_N,
) -> dict[str, Any]:
    """Papan "pola terkuat hari ini": pola yang menyala di sesi terakhir (pure).

    Dikelompokkan **per pola**, bukan per emiten, karena angka historisnya
    memang milik pola (bukan milik emiten): satu baris per pola dengan buktinya,
    lalu emiten yang memicunya di dalamnya. Daftar rata per emiten akan
    mengulang statistik yang sama puluhan kali dan menyembunyikan pesannya.

    Urutan kelompok mengikuti kekuatan bukti (lihat :func:`rank_patterns`) —
    pola tanpa catatan track record selalu di belakang. Urutan emiten di dalam
    kelompok mengikuti |net rupiah|: di antara nama yang berbeda ukuran,
    Rp 147 M dari Rp 680 M transaksi lebih besar artinya daripada Rp 1 M dari
    Rp 1 M, dan lantai ``min_window_value`` menjaga nama nyaris tak
    diperdagangkan tidak menempel di papan (lantai yang sama dengan radar).

    Returns ``{date, scanned, window_days, min_window_value, groups: [...]}``
    dengan tiap grup ``{pattern, label, direction, confidence, horizon_days,
    aligned_hit_rate, n_resolved_horizon, edge_pct, fired, count, emitters}``.
    ``fired`` = semua emiten yang memicu pola itu (termasuk yang tipis);
    ``count`` = yang lolos lantai likuiditas dan boleh ditampilkan. Tidak
    pernah raise.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for r in track_record:
        pid = r.get("pattern")
        if pid is not None:
            by_id[str(pid)] = r

    entries: list[tuple[str, dict[str, Any]]] = [
        (str(code).upper(), info or {})
        for code, info in emitters.items()
        if (info or {}).get("rows")
    ]
    if not entries:
        return {
            "date": None,
            "scanned": 0,
            "window_days": days,
            "min_window_value": min_window_value,
            "groups": [],
        }

    latest = max(info["rows"][-1]["date"] for _, info in entries)
    usable = [(code, info) for code, info in entries if info["rows"][-1]["date"] == latest]

    buckets: dict[str, list[dict[str, Any]]] = {}
    fired: dict[str, int] = {}
    for code, info in usable:
        rows = info["rows"]
        pats = detect_patterns(rows)
        if not pats:
            continue
        v = verdict(rows, days=days)
        wv = _window_stats(_clean_rows(rows), days)["value_sum"]
        for p in pats:
            pid = str(p.get("id"))
            fired[pid] = fired.get(pid, 0) + 1
            if not wv or wv < min_window_value:
                continue
            buckets.setdefault(pid, []).append(
                {
                    "code": code,
                    "name": info.get("name"),
                    "side": v.get("side"),
                    "net_sum_idr": v.get("net_sum_idr"),
                    "netval_pct": v.get("netval_pct"),
                    "streak": v.get("streak"),
                    "window_value": wv,
                    "date": v.get("date"),
                }
            )

    labels = {
        "silent_accumulation": ("Akumulasi diam-diam", "buy-side"),
        "distribution_on_rally": ("Distribusi saat naik", "sell-side"),
        "initiation": ("Inisiasi volume + asing", "buy-side"),
        "silent_distribution": ("Distribusi diam-diam", "sell-side"),
    }
    # urutan mengikuti PATTERNS supaya stabil, lalu diurutkan berdasar bukti
    order = {pid: i for i, pid in enumerate(PATTERNS)}
    groups: list[tuple[tuple[int, int, float, int], dict[str, Any]]] = []
    for pid in sorted(buckets, key=lambda p: order.get(p, 99)):
        emits = sorted(buckets[pid], key=lambda e: abs(e["net_sum_idr"] or 0.0), reverse=True)
        label, direction = labels.get(pid, (pid, None))
        h = pattern_history(by_id.get(pid), window_sessions=window_sessions) or {}
        conf = str(h.get("confidence")) if h else None
        edge = h.get("edge_pct") if h else None
        if not h:
            key = (1, 9, 0.0, order.get(pid, 99))
        else:
            key = (
                0,
                _CONFIDENCE_ORDER.get(str(conf), 9),
                -float(edge or 0.0),
                order.get(pid, 99),
            )
        groups.append(
            (
                key,
                {
                    "pattern": pid,
                    "label": label,
                    "direction": direction,
                    "confidence": conf,
                    "horizon_days": h.get("horizon_days"),
                    "aligned_hit_rate": h.get("aligned_hit_rate"),
                    "n_resolved_horizon": h.get("n_resolved_horizon"),
                    "edge_pct": edge,
                    "fired": fired.get(pid, len(emits)),
                    "count": len(emits),
                    "emitters": emits[:top_n],
                },
            )
        )

    return {
        "date": latest,
        "scanned": len(usable),
        "window_days": days,
        "min_window_value": min_window_value,
        "groups": [g for _, g in sorted(groups, key=lambda kv: kv[0])],
    }


# ------------------------------------------------------------------ narasi

#: Band label untuk "artinya seberapa" — ambang dari kalibrasi data (lihat
#: docstring modul).
SIZE_BANDS = (
    (LARGE_NETVAL_PCT, "besar"),
    (MEANINGFUL_NETVAL_PCT, "bermakna"),
)


def size_label(netval_pct: float | None) -> str | None:
    """Label ukuran aliran dalam kata: ``besar`` / ``bermakna`` / None."""
    if netval_pct is None:
        return None
    a = abs(netval_pct)
    for thr, label in SIZE_BANDS:
        if a >= thr:
            return label
    return None


def _fmt_rp(v: float | None) -> str:
    """Format rupiah ringkas (Rp ... T / M / Jt) — idem ``analytics._fmt_rp``."""
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e12:
        return f"Rp {v / 1e12:.2f} T"
    if a >= 1e9:
        return f"Rp {v / 1e9:.1f} M"
    return f"Rp {v / 1e6:.1f} Jt"


#: Singkatan bulan Indonesia untuk tanggal di narasi (bukan locale OS — output
#: harus sama di mesin mana pun).
_MONTHS_ID = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
)


def _fmt_date_short(value: Any) -> str | None:
    """``'2026-09-17'`` -> ``'17 Sep 2026'``; None bila tak bisa diurai."""
    s = str(value or "").strip()
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    return f"{day} {_MONTHS_ID[month - 1]} {year}"


def build_narrative(
    code: str,
    verdict: dict[str, Any],
    patterns: list[dict[str, Any]],
    rng: dict[str, Any] | None,
    pattern_history: Mapping[str, Any] | None = None,
    market: Mapping[str, Any] | None = None,
) -> list[str]:
    """Narasi plain-language 2-4 kalimat untuk banner "Jejak Smart Money".

    Prinsip: **verdict dulu, angka belakangan** — orang awam membaca satu
    baris dan sudah tahu arah + durasi; detail menyusul. Kalimat:

    1. Arah + durasi + ukuran:
       "BBCA — Sedang ditimbun asing (10 sesi terakhir): net buy Rp 1,2 T,
       setara 14% dari total nilai transaksi."
    2. Streak + sejak kapan (hanya jika >= 3, biar tidak bising):
       "...; net sell sejak 17 Sep 2026."
    3. Catatan pola (hanya pola pertama — yang paling banyak syaratnya).
    4. Arti historis pola itu (hanya bila track record-nya tersedia):
       seberapa sering pola ini diikuti arah yang diharapkan dan **di
       horizon berapa** — satu pola bercerita 5 hari, yang lain 3 minggu.
    5. Konteks pasar (hanya bila sebaran pasar tersedia): apakah verdict ini
       searah mayoritas pasar atau melawan arus — supaya verdict tidak dibaca
       sebagai sinyal khas emiten padahal cuma cermin pasar.
    6. Level (hanya jika ada range): "Harga 20 sesi terakhir terkurung
       9.200-9.500; tembus di atas 9.500 = konfirmasi, jebol 9.200 = batal."

    Emiten dengan verdict ``insufficient`` -> satu kalimat jujur "belum
    cukup data". Tidak pernah raise.
    """
    sentences: list[str] = []
    side = verdict.get("side")
    net_sum = verdict.get("net_sum_idr")
    netval = verdict.get("netval_pct")
    streak = verdict.get("streak") or 0

    if verdict.get("insufficient"):
        return [
            (f"{code} — Data aliran asing belum cukup untuk verdict (butuh >= 5 sesi bernilai).")
        ]

    if side == VERDICT_ACCUMULATION:
        head = "Sedang ditimbun asing"
        money = f"net buy {_fmt_rp(net_sum)}"
    elif side == VERDICT_DISTRIBUTION:
        head = "Sedang dibuang asing"
        money = f"net sell {_fmt_rp(abs(net_sum) if net_sum is not None else None)}"
    else:
        head = "Arus asing seimbang"
        money = f"net asing {_fmt_rp(net_sum)}"

    size = size_label(netval)
    size_txt = f", aliran {size}" if size else ""
    sentences.append(f"{code} — {head} (10 sesi terakhir): {money} pada nilai transaksi{size_txt}.")

    if streak >= 3 and side != VERDICT_NEUTRAL:
        arah = "net buy" if (side == VERDICT_ACCUMULATION) else "net sell"
        since = _fmt_date_short(verdict.get("streak_start_date"))
        since_txt = f", sejak {since}" if since else ""
        sentences.append(f"Terjadi {streak} sesi {arah} berturut-turut{since_txt}.")

    if patterns:
        sentences.append(patterns[0]["note"])
        # Arti historis pola yang paling syaratnya — klaim yang bisa dicek,
        # bukan cuma label. Hanya kalau track record-nya memang ada.
        if pattern_history:
            head = patterns[0]
            evidence = pattern_evidence_sentence(
                str(head.get("label") or ""),
                pattern_history.get(str(head.get("id")))
                if isinstance(pattern_history, Mapping)
                else None,
                str(head.get("direction")) if head.get("direction") else None,
            )
            if evidence:
                sentences.append(evidence)

    market_line = market_context_sentence(str(side), market)
    if market_line:
        sentences.append(market_line)

    if rng:
        sentences.append(
            f"Harga {rng['lookback']} sesi terakhir bergerak di rentang "
            f"{rng['low']:.0f}-{rng['high']:.0f}; penembusan di atas "
            f"{rng['resistance']:.0f} mengonfirmasi arah, jebol di bawah "
            f"{rng['support']:.0f} membatalkannya. Ini referensi level, "
            "bukan rekomendasi."
        )
    return sentences


# Emoji arah verdict untuk pesan alert (idem gaya SIG_EMOJI di notify.py).
VERDICT_EMOJI = {
    VERDICT_ACCUMULATION: "🟢",
    VERDICT_DISTRIBUTION: "🔴",
    VERDICT_NEUTRAL: "⚪",
}


def build_alert_message(
    code: str,
    name: str | None,
    verdict: dict[str, Any],
    patterns: list[dict[str, Any]],
    rng: dict[str, Any] | None,
    previous_side: str | None = None,
) -> str:
    """Pesan alert plain-language singkat (1-3 baris) untuk Telegram (pure).

    Mirip :func:`build_narrative` tapi dijaga pendek supaya bisa dipindai di
    HP. ``previous_side`` = sisi non-netral sebelumnya (``None`` = belum pernah
    / baru). Ini membentuk kata transisi di headline — yang paling penting bagi
    orang awam:

    - baru + akumulasi      -> "mulai ditimbun asing"
    - baru + distribusi     -> "mulai dibuang asing"
    - balik + akumulasi     -> "berbalik: asing mulai timbun"
    - balik + distribusi    -> "berbalik: asing mulai buang"
    - sudah sama (tanpa transisi) -> "sedang ditimbun/dibuang" (fallback)

    Streak (jika >= 3) dan catatan pola pertama menyusul. Level
    support/resistance sengaja TIDAK masuk (panjang; ada di banner).
    Emiten ``insufficient`` -> kalimat jujur "belum cukup data".
    Tidak pernah raise.
    """
    emoji = VERDICT_EMOJI.get(verdict.get("side", ""), "")
    label = f" {name}" if name else ""
    head = f"{emoji} <b>{code}</b>{label}"

    if verdict.get("insufficient"):
        return f"{head} — data aliran asing belum cukup untuk verdict."

    side = verdict.get("side")
    net_sum = verdict.get("net_sum_idr")
    buy_word = "ditimbun" if side == VERDICT_ACCUMULATION else "dibuang"

    if previous_side is None:
        trans = f"mulai {buy_word}"
    elif previous_side != side:
        trans = f"berbalik: asing mulai {buy_word}"
    else:
        trans = f"sedang {buy_word}"

    money = (
        f"net buy {_fmt_rp(net_sum)}"
        if side == VERDICT_ACCUMULATION
        else f"net sell {_fmt_rp(abs(net_sum) if net_sum is not None else None)}"
    )
    size = size_label(verdict.get("netval_pct"))
    size_txt = f", aliran {size}" if size else ""
    lines = [f"{head} — {trans} ({money}, 10 sesi terakhir{size_txt})."]

    streak = verdict.get("streak") or 0
    if streak >= 3 and side != VERDICT_NEUTRAL:
        arah = "net buy" if side == VERDICT_ACCUMULATION else "net sell"
        lines.append(f"{streak} sesi berturut-turut (arah {arah}).")
    if patterns and patterns[0].get("note"):
        lines.append(patterns[0]["note"])
    return "\n".join(lines)


# ------------------------------------------------------------------ radar


def build_radar(
    emitters: Mapping[str, Mapping[str, Any]],
    days: int = PRICE_WINDOW,
    min_window_value: float = RADAR_MIN_WINDOW_VALUE,
) -> list[dict[str, Any]]:
    """Radar smart money: peringkat emiten dengan jejak aliran terkuat (pure).

    ``emitters``: ``{code: {"name": ..., "rows": [...]}}`` — ``rows`` adalah
    baris harian (sama seperti :func:`verdict`) N sesi terakhir, urut
    menaik. Untuk tiap emiten:

    1. ``verdict(rows, days)`` — harus TIDAK ``insufficient`` dan
       ``side``-nya bukan netral.
    2. Lantai likuiditas: ``window_value`` (jumlah nilai transaksi di
       jendela) >= ``min_window_value`` (default Rp 500 Jt — di atas p10
       data nyata, jadi memotong emiten yang nyaris tak diperdagangkan).

    Returns daftar baris ``{code, name, side, net_sum_idr, netval_pct,
    streak, window_value, date}`` — **akumulasi & distribusi campur**,
    urut menurun berdasar |net_sum_idr| (uang, bukan persentase: emiten
    dengan net 2% dari Rp 10 T lebih besar maknanya dari 20% dari Rp 50 M).
    Endpoint mem-pisah ke dua daftar (in/out) dan memotong top-N.
    """
    out: list[dict[str, Any]] = []
    for code, info in emitters.items():
        rows = info.get("rows") or []
        v = verdict(rows, days=days)
        if v["insufficient"] or v["side"] == VERDICT_NEUTRAL:
            continue
        stats = _window_stats(_clean_rows(rows), days)
        wv = stats["value_sum"]
        if not wv or wv < min_window_value:
            continue
        out.append(
            {
                "code": code,
                "name": info.get("name"),
                "side": v["side"],
                "net_sum_idr": v["net_sum_idr"],
                "netval_pct": v["netval_pct"],
                "streak": v["streak"],
                "window_value": wv,
                "date": v["date"],
            }
        )
    out.sort(key=lambda r: abs(r["net_sum_idr"] or 0.0), reverse=True)
    return out


# --- APPEND-3 ---
