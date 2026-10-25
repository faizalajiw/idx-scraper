"""Peringkat, persentil, grade, dan ukuran porsi tier."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .param import (
    BAND_SHARES,
    COMPONENT_WEIGHTS,
    DEFAULT_HORIZON,
    NEUTRAL_SCORE,
    _f,
    validated_horizons,
)


def candidate_rank_key(row: Mapping[str, Any]) -> tuple[float, float, float, str]:
    """Kunci urutan kandidat (menurun): skor -> R/R -> likuiditas -> kode (pure).

    Tie-break-nya **dibukukan** karena komposisi skor sekarang beresolusi kasar
    (banyak kandidat persis di ``NEUTRAL_SCORE``). Tanpa tie-break yang berarti,
    peringkat akan ditentukan urutan kedatangan data. Likuiditas dipakai sebagai
    penyela supaya di antara nama ber-skor sama yang tampil lebih dulu adalah
    yang benar-benar diperdagangkan.
    """
    metrics = row.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    return (
        float(_f(row.get("score")) or 0.0),
        float(_f(row.get("rr")) or 0.0),
        float(_f(metrics.get("value")) or 0.0),
        str(row.get("code") or ""),
    )


#: Urutan pemutus yang BENAR-BENAR dipakai ``candidate_rank_key``, ditulis apa
#: adanya supaya pembaca papan tahu kenapa satu nama tampil di atas nama lain
#: saat skornya sama (spec D3: urutan tidak boleh jadi rahasia).
RANK_BASIS: tuple[str, ...] = ("skor", "R/R", "nilai transaksi", "kode")


def rank_basis(row: Mapping[str, Any]) -> dict[str, Any]:
    """Isi urutan pemutus untuk ditampilkan, sejalan dengan ``candidate_rank_key``."""
    metrics = row.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    return {
        "order": list(RANK_BASIS),
        "values": [
            _f(row.get("score")),
            _f(row.get("rr")),
            _f(metrics.get("value")),
            str(row.get("code") or ""),
        ],
    }


def pool_percentile(scores: Iterable[float]) -> list[float]:
    """Peringkat persentil 0-100 di dalam satu kolam; seri dapat nilai sama.

    Rumusnya ``(rank - 1) / (n - 1) * 100`` dengan seri memakai peringkat
    rata-rata; ``n == 1`` -> ``[100.0]``. Inilah yang menggantikan bacaan lama
    "skor 0-100 = keyakinan" (spec D3): 100 hanya berarti "tertinggi di kolam
    hari itu", bukan "peluang besar".
    """
    values = [float(_f(v) or 0.0) for v in scores]
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [100.0]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        average_rank = (i + j) / 2.0 + 1.0  # peringkat rata-rata, 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return [round((r - 1.0) / (n - 1) * 100.0, 1) for r in ranks]


def confidence_label(score: float, horizon: int) -> str:
    """Label keyakinan: "meyakinkan" hanya bila skor bergerak NAIK DAN horizon teruji.

    Dua aturan apa adanya (spec D3) - tidak ada ambang angka baru di sini: skor
    yang persis ``NEUTRAL_SCORE`` berarti tidak ada komponen yang bergerak, dan
    horizon tanpa komponen berbobot tidak punya bukti walk-forward sama sekali.
    Skor yang jatuh **di bawah** netral juga bukan keyakinan: barisnya tetap
    tayang sebagai kandidat, tapi labelnya tidak boleh menjanjikan apa pun.
    """
    value = _f(score)
    if value is not None and value > float(NEUTRAL_SCORE) and int(horizon) in validated_horizons():
        return "meyakinkan"
    return "belum terbukti"


def own_history_percentile(metrics: Any, code: str, horizon: int) -> float | None:
    """Peringkat skor terakhir emiten DI RIWAYATNYA SENDIRI (spec D3).

    Batas yang jujur: yang dihitung skor **dasar** - tanpa lapisan faktor IC dan
    aliran broker, karena keduanya cuma ada untuk sesi berjalan dan tidak bisa
    dihitung ulang per sesi lampau. ``None`` bila riwayatnya < 20 baris: angka
    yang lahir dari 3 sesi bukan peringkat, itu kebisingan.
    """
    try:
        sub = metrics[metrics["code"].astype(str).str.upper() == str(code).upper()]
    except (AttributeError, KeyError, TypeError):
        return None
    if sub is None or len(sub) < 20:
        return None
    ordered = sub.sort_values("date")
    from .skor import candidate_features

    scores = [
        score_components(candidate_features(row), int(horizon)) for _, row in ordered.iterrows()
    ]
    if not scores:
        return None
    return pool_percentile(scores)[-1]


def tier_sizes(n: int, shares: tuple[float, float, float] = BAND_SHARES) -> tuple[int, int, int]:
    """Jumlah nama per tier (A, B, C) untuk pool ``n`` — minimal 1 nama per tier.

    Batas minimum 1 menjaga pool kecil tetap terwakili (satu kandidat tetap
    dapat grade A, bukan hilang tanpa penjelasan), dan tiap tier selalu lebih
    lebar dari tier di atasnya.
    """
    total = max(0, int(n))
    a = max(1, round(total * (1.0 - float(shares[2]))))
    b = max(a + 1, round(total * (1.0 - float(shares[1]))))
    c = max(b + 1, round(total * (1.0 - float(shares[0]))))
    return (a, b, c)


def assign_grades(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Beri ``grade`` lewat peringkat relatif pool hari itu (pure).

    Urutan: ``candidate_rank_key`` menurun. Potongan tier dari ``BAND_SHARES``
    (C 25% · B 10% · A 2% teratas). Kandidat di luar 25% teratas **dibuang** —
    itu definisi "masuk papan", bukan filter skor absolut.

    Dipakai jalur papan maupun jalur satu-emiten supaya grade sebuah emiten di
    halaman detail persis sama dengan grade-nya di papan.
    """
    ordered = sorted((dict(c) for c in candidates), key=candidate_rank_key, reverse=True)
    n_a, n_b, n_c = tier_sizes(len(ordered))
    scores = [float(_f(c.get("score")) or 0.0) for c in ordered]
    # Peringkat di antara SEMUA kandidat yang lolos penyaringan hari itu, bukan
    # hanya yang lolos potongan tier: angkanya harus bisa dibandingkan antar hari
    # dengan definisi yang tetap. Ini BUKAN ukuran kolam likuiditas
    # (`pool_size` / `research.pool.PoolIndex`) - "kolam" di sini berarti
    # himpunan kandidat yang sudah dinilai.
    percentiles = pool_percentile(scores)
    out: list[dict[str, Any]] = []
    for i, c in enumerate(ordered):
        if i < n_a:
            c["grade"] = "A"
        elif i < n_b:
            c["grade"] = "B"
        elif i < n_c:
            c["grade"] = "C"
        else:
            break
        c["rank"] = i + 1
        c["pool"] = len(ordered)  # kandidat yang dinilai, bukan besar kolam likuiditas
        c["percentile"] = percentiles[i]
        c["rank_basis"] = rank_basis(c)
        # N1 (review): fallback yang SAMA dengan jalur papan/emiten. `or 0` di
        # sini berarti "horizon nol" - bukan horizon default - dan melabeli
        # kandidat tanpa field itu "belum terbukti" tanpa sebab.
        c["score_confidence"] = confidence_label(
            scores[i], int(c.get("horizon_days") or DEFAULT_HORIZON)
        )
        out.append(c)
    return out


def score_components(
    features: Mapping[str, float],
    horizon: int,
    *,
    extra_points: float = 0.0,
) -> float:
    """Skor satu horizon: ``NEUTRAL + Σ bobot x bendera + poin lapis terukur lain``.

    Bobot berasal dari ``COMPONENT_WEIGHTS`` (walk-forward, lihat docstring-nya).
    Tidak ada poin yang ditulis tangan di luar tabel itu selain ``extra_points``
    (lapis faktor IC / aliran broker — keduanya sudah punya gate IC sendiri dan
    hanya dikirim pemanggil bila benar-benar tervalidasi).
    """
    weights = COMPONENT_WEIGHTS.get(int(horizon), {})
    total = NEUTRAL_SCORE + float(extra_points)
    for name, w in weights.items():
        if w:
            total += float(w) * float(features.get(name, 0.0) or 0.0)
    return round(max(0.0, min(100.0, total)), 1)


def safe_horizon(horizon: int | None) -> int:
    """Horizon yang benar-benar punya komponen berbobot, else horizon default."""
    h = int(horizon) if horizon is not None else int(DEFAULT_HORIZON)
    return h if h in validated_horizons() else int(DEFAULT_HORIZON)
