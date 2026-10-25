"""Smart money — pola klasik, track record, dan arti historisnya.

Bagian ini menjawab "pola apa yang sedang terjadi dan seberapa layak
pola itu dipercaya" berdasarkan episode historis, bukan feeling.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .inti import (
    FLAT_RANGE_MAX_PCT,
    MEANINGFUL_NETVAL_PCT,
    MIN_WINDOW_DAYS,
    PRICE_WINDOW,
    RISING_MIN_PCT,
    VOL_SPIKE_RATIO,
    VOLUME_BASELINE_WINDOW,
    _clean_rows,
    _f,
    _window_stats,
)

# ------------------------------------------------------------------ pola klasik

#: Empat pola yang dikenali orang awam dari buku/podcast — tiap pola bisa
#: dijelaskan satu kalimat. Semua kondisi terdokumentasi di docstring masing-
#: masing; tidak ada parameter tersembunyi.
PATTERNS: tuple[str, ...] = (
    "silent_accumulation",
    "distribution_on_rally",
    "initiation",
    "silent_distribution",
)


def _pattern_conditions(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Blok-blok boolean yang dipakai semua pola (dihitung sekali).

    Jendela: harga/range/perubahan 10 sesi terakhir, volume hari ini vs
    rata-rata 20, net asing hari ini + jendela 10. Returns None jika history
    terlalu pendek untuk dipercaya (butuh >= 11 baris & 5 hari ber-net).
    """
    if len(rows) < 11:
        return None
    w10 = rows[-PRICE_WINDOW:]
    closes = [r["close"] for r in w10 if r["close"] is not None]
    highs = [r["high"] for r in w10 if r["high"] is not None]
    lows = [r["low"] for r in w10 if r["low"] is not None]
    if len(closes) < MIN_WINDOW_DAYS or len(highs) < MIN_WINDOW_DAYS or len(lows) < MIN_WINDOW_DAYS:
        return None

    last = rows[-1]
    vol_base = [r["volume"] for r in rows[-VOLUME_BASELINE_WINDOW:] if r["volume"]]
    vol_ratio = (
        (last["volume"] / (sum(vol_base) / len(vol_base)))
        if last["volume"] and len(vol_base) >= 10
        else None
    )
    net_today = last["net_idr"]
    value_today = last["value"]
    net_today_pct = (
        (net_today / value_today * 100.0)
        if net_today is not None and value_today and value_today > 0
        else None
    )
    stats10 = _window_stats(rows, PRICE_WINDOW)

    return {
        "range_pct": (max(highs) - min(lows)) / closes[-1] * 100.0,
        "chg10_pct": (closes[-1] / closes[0] - 1.0) * 100.0,
        "vol_ratio": vol_ratio,
        "net_today_pct": net_today_pct,
        "netval10_pct": stats10["netval_pct"],
        "net_sum_idr": stats10["net_sum_idr"],
        "n_net_days": stats10["n_net_days"],
    }


def pattern_flags(
    range_pct: float | None,
    chg10_pct: float | None,
    vol_ratio: float | None,
    netval10_pct: float | None,
) -> dict[str, bool]:
    """Boolean 4 pola dari metrik yang sudah dihitung (pure, tanpa baris).

    Ini satu-satunya sumber kebenaran syarat pola: dipakai jalur per-emiten
    (:func:`detect_patterns`) MAUPUN jalur panel (track record — metriknya
    dihitung vectorized, syaratnya tetap fungsi ini) supaya keduanya
    tidak pernah berbeda definisi. Input None -> kondisi terkait False (tanpa data
    = tanpa sinyal, bukan asumsi).
    """
    flat = range_pct is not None and netval10_pct is not None and range_pct < FLAT_RANGE_MAX_PCT
    up = chg10_pct is not None and chg10_pct > RISING_MIN_PCT
    down_hard = chg10_pct is not None and chg10_pct < -3.0
    spike = vol_ratio is not None and vol_ratio >= VOL_SPIKE_RATIO
    fb = netval10_pct is not None and netval10_pct >= MEANINGFUL_NETVAL_PCT
    fs = netval10_pct is not None and netval10_pct <= -MEANINGFUL_NETVAL_PCT

    return {
        "silent_accumulation": flat and fb,
        "distribution_on_rally": up and fs,
        "initiation": bool(spike and fb and not down_hard),
        "silent_distribution": flat and fs,
    }


def detect_patterns(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deteksi pola klasik jejak smart money (pure).

    Empat pola, prioritas dari terkuat (yang mensyaratkan paling banyak
    kondisi), dan **setengah-harga datar vs naik** adalah pembeda utamanya:

    - ``silent_accumulation`` (akumulasi diam-diam): harga 10 sesi terakhir
      TENANG (range < 10%), asing net buy bermakna (netval10 >= +5%).
      "Institusi mengumpulkan tanpa menggerakkan harga."
    - ``distribution_on_rally`` (distribusi saat naik): harga 10 sesi
      TERNAIK (> +8%), tapi asing net sell bermakna (netval10 <= -5%).
      "Yang jual lebih besar dari yang beli — hati-hati euforia."
    - ``initiation`` (inisiasi): lonjakan volume hari ini (>= 2x rata20) +
      asing net buy bermakna di jendela 10 (+ harga tidak turun > 3% — ini
      volume yang mendorong, bukan panic dump).
      "Masuknya baru mulai, perhatikan momentumnya."
    - ``silent_distribution`` (distribusi diam-diam): harga TENANG (range
      < 10%), asing net sell bermakna (netval10 <= -5%).
      "Pelan-pelan keluar tanpa menekan harga."

    Returns daftar (bisa 0-2; inisiasi bisa berjalan berdampingan dengan
    akumulasi) ``{id, label, direction, note}`` — ``direction`` sudah
    berorientasi ke investor: ``"buy-side"`` / ``"sell-side"``. Tidak pernah
    raise; history pendek -> ``[]``.
    """
    cleaned = _clean_rows(rows)
    c = _pattern_conditions(cleaned)
    if c is None or c["netval10_pct"] is None:
        return []

    flags = pattern_flags(c["range_pct"], c["chg10_pct"], c["vol_ratio"], c["netval10_pct"])

    out: list[dict[str, Any]] = []
    if flags["silent_distribution"]:
        out.append(
            {
                "id": "silent_distribution",
                "label": "Distribusi diam-diam",
                "direction": "sell-side",
                "note": (
                    f"Harga 10 sesi terakhir tenang (range {c['range_pct']:.0f}%), "
                    "tapi asing tercatat net sell bermakna — dana keluar pelan "
                    "tanpa menekan harga."
                ),
            }
        )
    if flags["distribution_on_rally"]:
        out.append(
            {
                "id": "distribution_on_rally",
                "label": "Distribusi saat naik",
                "direction": "sell-side",
                "note": (
                    f"Harga naik {c['chg10_pct']:.0f}% dalam 10 sesi, tapi asing "
                    "net sell bermakna — yang jual lebih besar dari yang beli; "
                    "hati-hati euforia."
                ),
            }
        )
    if flags["silent_accumulation"]:
        out.append(
            {
                "id": "silent_accumulation",
                "label": "Akumulasi diam-diam",
                "direction": "buy-side",
                "note": (
                    f"Harga 10 sesi terakhir tenang (range {c['range_pct']:.0f}%), "
                    "sementara asing tercatat net buy bermakna — pola khas "
                    "pengumpulan sebelum harga bergerak."
                ),
            }
        )
    if flags["initiation"]:
        out.append(
            {
                "id": "initiation",
                "label": "Inisiasi volume + asing",
                "direction": "buy-side",
                "note": (
                    f"Volume hari ini {c['vol_ratio']:.1f}x rata-rata 20 sesi "
                    "dengan asing net buy bermakna — tanda masuk yang baru "
                    "mulai bergerak."
                ),
            }
        )
    return out


#: Jarak minimum antar kejadian pola (sesi) agar satu episode tidak
#: terhitung berulang kali — jendela pola selebar 10 sesi, jadi episode yang
#: tumpang-tindih (hari-hari berdekatan) dianggap satu episode.
EPISODE_GAP = 10


def collect_pattern_episodes(panel: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Kumpulkan kejadian pola klasik dari panel per emiten (pure, tanpa DB).

    Panel bentuk ``{code: {name, rows}}`` (baris harian urut tanggal menaik,
    kunci ``date/close/high/low/volume/net_idr/value`` — bentuk yang sama
    dengan jalur per-emiten). Untuk tiap emiten, tiap baris T yang punya
    history >= 11 sesi di belakangnya diperiksa dengan ``_pattern_conditions``
    + ``pattern_flags`` — **fungsi yang sama** dipakai ``detect_patterns``,
    sehingga definisi pola di sini tak pernah berbeda dari jalur per-emiten.

    Dedup episode: jika dua kejadian T dan T+1 (jarak < :data:`EPISODE_GAP`)
    untuk pola yang sama, hanya yang lebih lama yang dihitung — jendela 10
    sesi yang tumpang-tindih bukan kejadian baru.

    Returns daftar ``{code, date, pattern}`` (khususnya metrik yang dibutuhkan
    :func:`pattern_track_record`; forward return dihitung pemanggil karena
    butuh baris *setelah* T). Tidak pernah raise; panel kosong -> ``[]``.
    """
    episodes: list[dict[str, Any]] = []
    for code, entry in panel.items():
        rows = entry.get("rows") if isinstance(entry, Mapping) else entry
        if not rows:
            continue
        cleaned = _clean_rows(rows)
        last_idx = len(cleaned) - 1
        last_seen: dict[str, int] = {}
        # i mulai dari PRICE_WINDOW (10) supaya prefix selalu punya >= 11 baris
        # — syarat ``_pattern_conditions``. Tiap baris T = "hari kejadian".
        for i in range(PRICE_WINDOW, last_idx + 1):
            prefix = cleaned[: i + 1]
            c = _pattern_conditions(prefix)
            if c is None or c["netval10_pct"] is None:
                continue
            flags = pattern_flags(c["range_pct"], c["chg10_pct"], c["vol_ratio"], c["netval10_pct"])
            today = cleaned[i]["date"]
            for pid, hit in flags.items():
                if not hit:
                    continue
                prev = last_seen.get(pid)
                if prev is not None and (i - prev) < EPISODE_GAP:
                    continue  # masih satu episode yang sama
                last_seen[pid] = i
                episodes.append({"code": code, "date": today, "pattern": pid})
    return episodes


# ------------------------------------------------------------------ track record


def _mean(v: list[float]) -> float | None:
    return sum(v) / len(v) if v else None


def _median(v: list[float]) -> float | None:
    s = sorted(v)
    if not s:
        return None
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def _tstat(v: list[float]) -> float | None:
    """t-stat mean-vs-nol (tanpa Newey-West: kejadian antar emiten hampir
    independen cross-sectional — beda dengan deret waktu se-emasiten)."""
    if len(v) < 5:
        return None
    m = _mean(v)
    var = sum((x - m) ** 2 for x in v) / (len(v) - 1)
    if var is None or var <= 0:
        return None
    return m / (var**0.5 / len(v) ** 0.5)


def pattern_track_record(
    occurrences: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Agregasi track record pola klasik dari kejadian yang sudah diberi
    forward return (pure).

    ``occurrences``: satu baris per kejadian pola ``{code, date, pattern,
    fwd5, fwd10, fwd21, [abn5, abn10, abn21]}`` — forward return dalam
    **persen**, entry T+1 (disiplin yang sama dengan ``research.signal_log``);
    horizon yang belum terealisasi = None. ``abn{h}`` (opsional) = forward
    return dikurangi return pasar equal-weight window yang sama (alpha vs
    pasar; konvensi ``abn_k`` di ``signal_log``).

    Returns daftar per pola (urutan :data:`PATTERNS`) ``{pattern, label,
    direction, n, n_resolved, per horizon: {n, hit_rate, aligned_hit_rate,
    mean, median, tstat, alpha, effective_alpha}}``. ``alpha`` = mean
    ``abn{h}`` (None bila tak ada abn). Hit rate = porsi kejadian
    terealisasi dengan forward return > 0. ``aligned_hit_rate`` &
    ``effective_alpha`` = interpretasi searah pola: untuk pola sell-side
    (yang "berfungsi" bila harga turun), aligned = 1 − hit_rate dan
    effective_alpha dibalik tanda — jadi dua field ini selalu bisa dibaca
    "seberapa sering/besar polanya bekerja sesuai ekspektasi". Pola tanpa
    kejadian -> tidak masuk daftar (UI menampilkan "belum ada kejadian").
    Tidak pernah raise.
    """
    labels = {
        "silent_accumulation": ("Akumulasi diam-diam", "buy-side"),
        "distribution_on_rally": ("Distribusi saat naik", "sell-side"),
        "initiation": ("Inisiasi volume + asing", "buy-side"),
        "silent_distribution": ("Distribusi diam-diam", "sell-side"),
    }
    per_pattern: dict[str, list[dict[str, Any]]] = {p: [] for p in PATTERNS}
    for o in occurrences:
        pid = str(o.get("pattern"))
        if pid in per_pattern:
            per_pattern[pid].append(o)

    out: list[dict[str, Any]] = []
    for pid in PATTERNS:
        rows = per_pattern[pid]
        if not rows:
            continue
        n = len(rows)
        label, direction = labels.get(pid, (pid, None))
        horizons: dict[str, Any] = {}
        n_resolved = 0
        for h in (1, 5, 10, 21):
            vals = [_f(r.get(f"fwd{h}")) for r in rows]
            vals = [v for v in vals if v is not None]
            abn = [_f(r.get(f"abn{h}")) for r in rows]
            abn = [v for v in abn if v is not None]
            n_resolved = max(n_resolved, len(vals))
            hit = (sum(1 for v in vals if v > 0) / len(vals)) if vals else None
            # Interpretasi mengikuti arah pola: pola sell-side "berfungsi"
            # bila harga TURUN, jadi hit rate-nya = porsi fwd < 0, dan
            # alpha efektif dibalik tanda. Keduanya = "seberapa sering
            # pola ini bekerja sesuai ekspektasi", satu sumbu baca.
            aligned = (hit if direction == "buy-side" else 1.0 - hit) if hit is not None else None
            eff_alpha = None
            if abn:
                eff_alpha = _mean(abn) if direction == "buy-side" else -_mean(abn)
            horizons[f"fwd{h}"] = {
                "n": len(vals),
                "hit_rate": hit,
                "aligned_hit_rate": aligned,
                "mean": _mean(vals),
                "median": _median(vals),
                "tstat": _tstat(vals),
                "alpha": _mean(abn),
                "effective_alpha": eff_alpha,
            }
        out.append(
            {
                "pattern": pid,
                "label": label,
                "direction": direction,
                "n": n,
                "n_resolved": n_resolved,
                "horizons": horizons,
            }
        )
    return out


# ------------------------------------------------------------------ arti historis

#: Minimum episode TEREFALISASI sebelum satu persentase layak diklaim ke user.
#: 30 = ambang sampel konvensional untuk sebuah rate (bukan tebakan): di bawah
#: ini, 60% dan 60%-nya lagi bisa datang dari 10 kejadian dan tak berarti apa-
#: apa. Horizon di bawah ambang ini tetap dikirim, tapi ditandai tidak reliable.
MIN_HISTORY_N = 30

#: Ambang |t-stat| -> kata keyakinan. 2 ≈ batas konvensional "bukan kebetulan",
#: 1 = indikasi lemah. Menjawab "seberapa yakin ini bukan noise" — pertanyaan
#: berbeda dari "seberapa sering arahnya benar" (aligned_hit_rate).
CONFIDENCE_BANDS = ((2.0, "tinggi"), (1.0, "sedang"))

#: Urutan kata keyakinan untuk pengurutan (kecil = lebih dulu).
_CONFIDENCE_ORDER = {"tinggi": 0, "sedang": 1, "lemah": 2}


def _confidence_word(tstat: Any) -> str:
    """``|t| >= 2`` -> ``tinggi``, ``>= 1`` -> ``sedang``, selain itu ``lemah``."""
    t = _f(tstat)
    if t is None:
        return "lemah"
    for threshold, word in CONFIDENCE_BANDS:
        if abs(t) >= threshold:
            return word
    return "lemah"


#: Nama horizon (kunci dari ``pattern_track_record``) -> jumlah hari bursa.
HORIZON_DAYS = {"fwd1": 1, "fwd5": 5, "fwd10": 10, "fwd21": 21}


def pattern_history(
    row: Mapping[str, Any] | None,
    *,
    window_sessions: int | None = None,
) -> dict[str, Any] | None:
    """Ringkas track record SATU pola jadi bentuk siap-tampil (pure).

    ``row`` = satu entri keluaran :func:`pattern_track_record` (atau hasil
    :func:`patterns_history`). Memilih **horizon terbaik** = ``aligned_hit_rate``
    tertinggi di antara horizon yang sampelnya >= :data:`MIN_HISTORY_N`
    (seri diputus oleh n terbesar); bila tak ada yang memenuhi, dipakai
    horizon dengan n terbesar dan hasilnya ditandai ``reliable=False``.

    Kenapa horizon dipilih, bukan dirata-rata: kekuatan pola berbeda tajam
    antar horizon (pola akumulasi nyaris tak berarti di 5 hari, baru terbaca
    di 21 hari; pola distribusi justru sebaliknya). Satu angka gabungan akan
    menyembunyikan fakta itu — tepat kesalahan yang mau dihindari.

    Returns ``{n, n_resolved, window_sessions, horizon, horizon_days,
    aligned_hit_rate, n_resolved_horizon, tstat, effective_alpha, reliable,
    horizons}`` atau None bila tak ada horizon. Tidak pernah raise.
    """
    if not row:
        return None
    horizons = row.get("horizons") or {}
    if not isinstance(horizons, Mapping) or not horizons:
        return None

    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for key, stats in horizons.items():
        if not isinstance(stats, Mapping):
            continue
        if stats.get("aligned_hit_rate") is None:
            continue
        candidates.append((str(key), stats))
    if not candidates:
        return None

    def _n(key_stats: tuple[str, Mapping[str, Any]]) -> int:
        return int(_f(key_stats[1].get("n")) or 0)

    def _aligned(key_stats: tuple[str, Mapping[str, Any]]) -> float:
        return float(_f(key_stats[1].get("aligned_hit_rate")) or 0.0)

    reliable_pool = [c for c in candidates if _n(c) >= MIN_HISTORY_N]
    if reliable_pool:
        best = max(reliable_pool, key=lambda c: (_aligned(c), _n(c)))
        reliable = True
    else:
        best = max(candidates, key=_n)
        reliable = False

    key, stats = best
    clean: dict[str, Any] = {}
    for hkey, hstats in horizons.items():
        if isinstance(hstats, Mapping) and hstats.get("aligned_hit_rate") is not None:
            clean[str(hkey)] = {
                "n": int(_f(hstats.get("n")) or 0),
                "aligned_hit_rate": hstats.get("aligned_hit_rate"),
                "tstat": hstats.get("tstat"),
                "effective_alpha": hstats.get("effective_alpha"),
                "horizon_days": HORIZON_DAYS.get(str(hkey)),
            }

    # Keyakinan memakai |t| TERBAIK di antara horizon yang sampelnya layak —
    # menjawab "seberapa yakin ini bukan kebetulan", pertanyaan berbeda dari
    # "seberapa sering arahnya benar" (yang dijawab horizon terpilih di atas).
    # Bisa jadi horizon dengan hit rate lebih rendah justru lebih meyakinkan
    # (menang sering sedikit, tapi besar) — dan itu memang informasi berguna.
    best_t: float | None = None
    for hstats in horizons.values():
        if not isinstance(hstats, Mapping):
            continue
        if int(_f(hstats.get("n")) or 0) < MIN_HISTORY_N:
            continue
        t = _f(hstats.get("tstat"))
        if t is None:
            continue
        if best_t is None or abs(t) > abs(best_t):
            best_t = t
    if best_t is None:
        fallback_t = _f(stats.get("tstat"))
        best_t = float(fallback_t) if fallback_t is not None else None

    edge = _f(stats.get("effective_alpha"))

    return {
        "n": row.get("n"),
        "n_resolved": row.get("n_resolved"),
        "window_sessions": window_sessions,
        "horizon": key,
        "horizon_days": HORIZON_DAYS.get(key),
        "aligned_hit_rate": stats.get("aligned_hit_rate"),
        "n_resolved_horizon": int(_f(stats.get("n")) or 0),
        "tstat": stats.get("tstat"),
        "effective_alpha": stats.get("effective_alpha"),
        "reliable": reliable,
        "confidence": _confidence_word(best_t),
        "best_tstat": best_t,
        "edge_pct": abs(edge) if edge is not None else None,
        "horizons": clean,
    }


def patterns_history(
    patterns: Iterable[Mapping[str, Any]],
    track_record: Iterable[Mapping[str, Any]],
    *,
    window_sessions: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Peta ``pattern_id -> pattern_history`` untuk pola yang terdeteksi.

    Dipakai jalur per-emiten: pola yang benar-benar muncul hari ini saja yang
    perlu dibawakan angka historisnya. ``track_record`` = daftar keluaran
    :func:`pattern_track_record`; pola yang tak ada di sana dilewati (UI
    menampilkan pola tanpa klaim historis, bukan angka nol palsu).
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for r in track_record:
        pid = r.get("pattern")
        if pid is not None:
            by_id[str(pid)] = r
    out: dict[str, dict[str, Any]] = {}
    for p in patterns:
        pid = str(p.get("id"))
        h = pattern_history(by_id.get(pid), window_sessions=window_sessions)
        if h is not None:
            out[pid] = h
    return out


def pattern_evidence_sentence(
    label: str,
    history: Mapping[str, Any] | None,
    direction: str | None = None,
) -> str | None:
    """Kalimat bukti plain-language untuk satu pola + track record-nya.

    Contoh: "Dalam 120 sesi terakhir, 64% dari 484 kejadian serupa diikuti
    harga turun dalam 5 hari bursa." Horizon dan arah selalu disebut, karena
    itulah isi klaimnya — pola akumulasi bercerita 3 minggu, pola distribusi
    bercerita 5 hari. Sampel kecil ditandai terus terang.

    ``direction`` **wajib** untuk menyebut arah harga dengan benar:
    ``aligned_hit_rate`` sudah dihitung searah pola (untuk pola jual, "sesuai
    ekspektasi" berarti harga TURUN), jadi arah tidak boleh disimpulkan dari
    besar-kecilnya angka — pola jual dengan 64% akan terbaca "harga naik" dan
    itu justru membalik artinya. Tanpa ``direction``, kalimat memakai frasa
    netral "sesuai arah polanya" daripada menebak.

    Angka rate tetap apa adanya meski di bawah 50%: itu artinya pola ini
    justru sering meleset, dan user berhak melihatnya.
    """
    if not history:
        return None
    rate = _f(history.get("aligned_hit_rate"))
    days = history.get("horizon_days")
    n = history.get("n_resolved_horizon") or 0
    if rate is None or not days or not n:
        return None
    window = history.get("window_sessions")
    head = f"Dalam {window} sesi terakhir" if window else "Secara historis"
    tail = "" if history.get("reliable", True) else " (sampel masih kecil — anggap indikatif)"
    if direction == "buy-side":
        hasil = "diikuti harga naik"
    elif direction == "sell-side":
        hasil = "diikuti harga turun"
    else:
        hasil = "berakhir sesuai arah polanya"
    return (
        f"{head}, {rate * 100:.0f}% dari {n} kejadian {label.lower()} "
        f"{hasil} dalam {days} hari bursa{tail}."
    )


def rank_patterns(
    patterns: Iterable[Mapping[str, Any]],
    histories: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Urutkan pola terdeteksi menurut KEKUATAN BUKTI, bukan urutan deteksi (pure).

    Satu emiten bisa memicu dua pola sekaligus. Yang tampil lebih dulu
    sebaiknya yang catatannya paling bisa dipercaya, bukan yang kebetulan
    diperiksa lebih dulu. Kunci berurutan:

    1. punya catatan track record atau tidak — pola tanpa catatan SELALU di
       belakang, karena "belum ada bukti" bukan "terbukti lemah";
    2. kata keyakinan (|t| terbaik lintas horizon layak);
    3. besar efek (|effective_alpha| di horizon terpilih).

    Stabil: pola dengan kunci sama tetap pada urutan deteksinya. Tidak pernah
    raise; daftar kosong -> ``[]``.
    """
    items = [dict(p) for p in patterns]
    hists = histories or {}

    def _key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, float, int]:
        i, p = pair
        h = hists.get(str(p.get("id"))) or {}
        if not h:
            return (1, 9, 0.0, i)
        conf = _CONFIDENCE_ORDER.get(str(h.get("confidence")), 9)
        edge = _f(h.get("edge_pct")) or 0.0
        return (0, conf, -edge, i)

    return [p for _, p in sorted(enumerate(items), key=_key)]
