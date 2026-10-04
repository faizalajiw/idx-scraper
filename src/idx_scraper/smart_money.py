"""Jejak smart money — verdict, pola klasik, dan narasi plain-language per emiten.

Menjawab tiga pertanyaan orang awam untuk satu emiten:

1. "Pemain besar lagi masuk atau keluar?"  -> :func:`verdict`
   (akumulasi / distribusi / netral — satu kata, bukan skor kuantitatif)
2. "Sejak kapan dan seberapa besar?"       -> streak (hari berturut) + ukuran
   (rupiah + porsi dari total nilai transaksi)
3. "Patokannya di mana kalau mau ikut?"    -> :func:`consolidation_range`

Kenapa modul ini pure
---------------------
Pola yang sama dengan ``idx_scraper.foreign_flow`` dan
``idx_scraper.broker_flow``: semua perhitungan menerima iterable baris dan
TANPA akses DB supaya bisa dites tanpa Postgres. Query-nya tinggal menempel di
endpoint (``api.analytics``) yang menyiapkan baris ``{date, net_idr, value,
close, high, low, volume}`` urut tanggal menaik.

Batasannya yang harus selalu diingat UI
---------------------------------------
"Smart money" di sini = agregat **investor asing** (IDX menggabungkan semua
firma asing; di data gratis tidak ada breakdown per firma per saham) + pola
perilaku harga/volume. Labelnya apa adanya: ini jejak, bukan rekomendasi.

Ambang kalibrasi (dilakukan terhadap data nyata 244 sesi bursa, 967 emiten,
24 Sep 2025 - 30 Sep 2026 — bukan angka feeling; idiom yang sama dengan
koreksi desain ``research.sentiment``):

- range harga 10 sesi: p25 ≈ 9,9%  -> "flat" = range < 10%
- perubahan 10 sesi:  > +8% = naik jelas (≈17% dari semua emiten-sesi)
- volume hari ini / rata20: p90 ≈ 2,1 -> "lonjakan" = ≥ 2x
- net asing 10 sesi / nilai transaksi 10 sesi:
  p50|.| ≈ 2%, p75|.| ≈ 6,2%, p90|.| ≈ 16,3%
  -> "artinya" (directional) ≥ 5%, "besar" ≥ 16%
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

# ------------------------------------------------------------------ konstanta

# Ambang dari kalibrasi data (lihat docstring modul).
FLAT_RANGE_MAX_PCT = 10.0      # range 10 sesi di bawah ini = harga "tenang"
RISING_MIN_PCT = 8.0           # perubahan 10 sesi di atas ini = "naik jelas"
VOL_SPIKE_RATIO = 2.0          # volume hari ini >= 2x rata20 = lonjakan
MEANINGFUL_NETVAL_PCT = 5.0    # net asing jml / nilai jml >= 5% = bermakna
LARGE_NETVAL_PCT = 16.0        # >= 16% = aliran besar (p90)
INITIATION_NET_PCT = 2.0       # inisiasi: net hari ini >= 2% nilai hari itu

PRICE_WINDOW = 10              # jendela "terakhir" utk verdict & pola (sesi)
VOLUME_BASELINE_WINDOW = 20    # baseline volume utk rasio lonjakan
RANGE_WINDOW = 10              # jendela range utk deteksi "flat"
MIN_WINDOW_DAYS = 5            # min. hari ber-net di jendela agar boleh verdict

VERDICT_ACCUMULATION = "akumulasi"
VERDICT_DISTRIBUTION = "distribusi"
VERDICT_NEUTRAL = "netral"

# Lantai likuiditas radar: jumlah nilai transaksi di jendela harus >= ini
# (Rp 500 Jt — di atas p10 nilai-5-sesi ≈ Rp 180 Jt, jadi memotong emiten
# yang paling sulit diperdagangkan tanpa membuang emiten kecil yang likuid).
RADAR_MIN_WINDOW_VALUE = 5.0e8


# ------------------------------------------------------------------ helper


def _f(value: Any) -> float | None:
    """Ambil nilai float; None/NaN/inf/invalid -> None (bukan 0)."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clean_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalisasi baris + urut tanggal menaik (defensif; endpoint sudah urut).

    ``date`` boleh str ISO / ``date`` / ``Timestamp`` — dinormalkan ke str ISO
    supaya sort lexicographic selalu benar (YYYY-MM-DD).
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r.get("date")
        if d is None:
            continue
        dstr = str(d)
        if len(dstr) >= 10:
            dstr = dstr[:10]
        out.append(
            {
                "date": dstr,
                "net_idr": _f(r.get("net_idr")),
                "value": _f(r.get("value")),
                "close": _f(r.get("close")),
                "high": _f(r.get("high")),
                "low": _f(r.get("low")),
                "volume": _f(r.get("volume")),
            }
        )
    out.sort(key=lambda x: x["date"])
    return out


def _streak(rows: list[dict[str, Any]]) -> tuple[int, str, str | None]:
    """Streak arah net asing terakhir (hari berturut-turut, sampai hari paling
    baru). Returns ``(streak, side, start_date)``; side =
    ``"net_buy"``/``"net_sell"``/``"flat"``; ``start_date`` = tanggal sesi
    PERTAMA streak yang sedang berjalan (None bila streak 0) — ini yang
    menjawab "sejak kapan" tanpa harus menghitung sendiri di UI. Hari net 0
    atau tanpa data memutus streak — konsisten dengan
    ``foreign_flow.daily_flow``.
    """
    streak = 0
    side = "flat"
    start_date: str | None = None
    for r in reversed(rows):
        net = r["net_idr"]
        if net is None:
            break
        s = 1 if net > 0 else (-1 if net < 0 else 0)
        if s == 0:
            break
        if side == "flat":
            side = "net_buy" if s > 0 else "net_sell"
        elif (s > 0 and side == "net_sell") or (s < 0 and side == "net_buy"):
            break
        streak += 1
        # baris ditelusuri mundur, jadi nilai terakhir = hari paling awal
        start_date = r["date"]
    return streak, side, start_date


def _window_stats(rows: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Agregat N sesi terakhir (sesi bursa, bukan hari kalender).

    ``netval_pct`` = jumlah net asing / jumlah nilai transaksi * 100 — porsi
    nilai transaksi yang benar-benar dibeli/dijual asing di jendela. None jika
    tidak ada nilai (tak bisa bagi). Hari tanpa net tidak dianggap net 0.
    """
    window = rows[-days:]
    net_sum = sum(r["net_idr"] for r in window if r["net_idr"] is not None)
    value_sum = sum(r["value"] for r in window if r["value"] is not None)
    n_net = sum(1 for r in window if r["net_idr"] is not None)
    netval_pct = (net_sum / value_sum * 100.0) if value_sum and value_sum > 0 else None
    return {
        "net_sum_idr": net_sum,
        "value_sum": value_sum if value_sum > 0 else None,
        "netval_pct": netval_pct,
        "n_net_days": n_net,
    }


# ------------------------------------------------------------------ verdict


def verdict(rows: Iterable[Mapping[str, Any]], days: int = PRICE_WINDOW) -> dict[str, Any]:
    """Verdict jejak smart money satu emiten — satu jawaban plain-language.

    Rules (terdokumentasi, tanpa angka magic):

    - ``side``: ``netval_pct`` >= +5 -> ``akumulasi``; <= -5 -> ``distribusi``;
      selain itu ``netral``. (5% = p75 |netval10| data nyata — hanya 1 dari 4
      emiten-sesi yang sampai di situ.)
    - ``strength``: |netval_pct| >= 16 -> ``besar`` (p90); >= 5 -> ``menengah``;
      netral -> None.
    - ``insufficient``: True jika < :data:`MIN_WINDOW_DAYS` hari ber-net di
      jendela — UI wajib menampilkan "data belum cukup", bukan netral palsu.

    Returns ``side, strength, streak, streak_side, streak_start_date,
    net_sum_idr, netval_pct, n_net_days, date, insufficient``. Tidak pernah
    raise — emiten tanpa data menghasilkan struktur kosong yang aman
    di-render.

    ``days``: lebar jendela agregasi (default 10). Requirement "hari
    bernilai" berskala dengan jendela (``min(5, days)``) supaya jendela pendek
    (mis. radar 2 hari) tidak selamanya ``insufficient``.
    """
    cleaned = _clean_rows(rows)
    streak, streak_side, streak_start = _streak(cleaned)
    stats = _window_stats(cleaned, days)
    netval = stats["netval_pct"]
    min_days = min(MIN_WINDOW_DAYS, days)
    insufficient = stats["n_net_days"] < min_days or netval is None

    if insufficient:
        side = VERDICT_NEUTRAL
        strength = None
    elif netval >= MEANINGFUL_NETVAL_PCT:
        side = VERDICT_ACCUMULATION
        strength = "besar" if netval >= LARGE_NETVAL_PCT else "menengah"
    elif netval <= -MEANINGFUL_NETVAL_PCT:
        side = VERDICT_DISTRIBUTION
        strength = "besar" if netval <= -LARGE_NETVAL_PCT else "menengah"
    else:
        side = VERDICT_NEUTRAL
        strength = None

    return {
        "side": side,
        "strength": strength,
        "streak": streak,
        "streak_side": streak_side,
        "streak_start_date": streak_start if streak > 0 else None,
        "net_sum_idr": stats["net_sum_idr"] or None,
        "netval_pct": round(netval, 2) if netval is not None else None,
        "n_net_days": stats["n_net_days"],
        "date": cleaned[-1]["date"] if cleaned else None,
        "insufficient": insufficient,
    }


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
    flat = (
        range_pct is not None
        and netval10_pct is not None
        and range_pct < FLAT_RANGE_MAX_PCT
    )
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

    flags = pattern_flags(
        c["range_pct"], c["chg10_pct"], c["vol_ratio"], c["netval10_pct"]
    )

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
            flags = pattern_flags(
                c["range_pct"], c["chg10_pct"], c["vol_ratio"], c["netval10_pct"]
            )
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


# ------------------------------------------------------------------ level

RANGE_LOOKBACK = 20  # jendela konsolidasi untuk level pembatalan


def consolidation_range(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Rentang konsolidasi N sesi terakhir — level pembatalan untuk ikut.

    Membalas pertanyaan "kalau mau ikut, patokannya di mana?":

    - ``low``/``high``: low & high N sesi terakhir (window default 20).
    - ``support``/``resistance``: sama dengan low/high (label yang bisa
      dipegang user awam).
    - ``breakout_above``: ``high`` — penembusan di atasnya = konfirmasi.
    - ``invalidation_below``: ``low`` — jebol di bawahnya = pembatalan.
    - ``range_pct``: lebar rentang dalam % harga.

    Returns None jika < 5 sesi ber-harga (histori terlalu pendek — UI tidak
    boleh menampilkan level yang tidak bisa dihitung).
    """
    cleaned = _clean_rows(rows)
    window = [
        r
        for r in cleaned[-RANGE_LOOKBACK:]
        if r["close"] is not None and r["low"] is not None and r["high"] is not None
    ]
    if len(window) < 5:
        return None
    low = min(r["low"] for r in window)
    high = max(r["high"] for r in window)
    last_close = window[-1]["close"]
    if last_close is None or last_close <= 0:
        return None
    return {
        "lookback": len(window),
        "low": round(low, 2),
        "high": round(high, 2),
        "support": round(low, 2),
        "resistance": round(high, 2),
        "range_pct": round((high - low) / last_close * 100.0, 2),
    }


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
        for h in (5, 10, 21):
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
            if hit is not None:
                aligned = hit if direction == "buy-side" else 1.0 - hit
            else:
                aligned = None
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
HORIZON_DAYS = {"fwd5": 5, "fwd10": 10, "fwd21": 21}


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
    head = (
        f"Dalam {window} sesi terakhir" if window else "Secara historis"
    )
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
        return {"date": None, "scanned": 0, "window_days": days,
                "min_window_value": min_window_value, "groups": []}

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
    "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
    "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
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
            (
                f"{code} — Data aliran asing belum cukup untuk verdict "
                "(butuh >= 5 sesi bernilai)."
            )
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
    sentences.append(
        f"{code} — {head} (10 sesi terakhir): {money} pada nilai transaksi"
        f"{size_txt}."
    )

    if streak >= 3 and side != VERDICT_NEUTRAL:
        arah = "net buy" if (side == VERDICT_ACCUMULATION) else "net sell"
        since = _fmt_date_short(verdict.get("streak_start_date"))
        since_txt = f", sejak {since}" if since else ""
        sentences.append(
            f"Terjadi {streak} sesi {arah} berturut-turut{since_txt}."
        )

    if patterns:
        sentences.append(patterns[0]["note"])
        # Arti historis pola yang paling syaratnya — klaim yang bisa dicek,
        # bukan cuma label. Hanya kalau track record-nya memang ada.
        if pattern_history:
            head = patterns[0]
            evidence = pattern_evidence_sentence(
                str(head.get("label") or ""),
                pattern_history.get(str(head.get("id"))) if isinstance(pattern_history, Mapping) else None,
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
