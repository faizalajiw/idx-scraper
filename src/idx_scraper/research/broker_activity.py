"""Skor Aktivitas Broker — proksi smart money per emiten, terkalibrasi IC.

Kenapa proksi (dan bukan angka broker asli)
-------------------------------------------
IDX **tidak** mempublikasikan breakdown broker per emiten di endpoint gratis
yang kita polling (lihat komentar di ``sql/research_schema.sql``). Yang benar-
benar tersedia:

- ``research.broker_daily`` — agregat PER BROKER FIRMA untuk SELURUH pasar
  (EOD, ``GetBrokerSummary``). Berguna untuk konsentrasi pasar, bukan per saham.
- ``foreign_buy/sell/net`` — PER EMITEN, tapi agregat "asing" (bukan per firma).
- snapshot order book intraday — PER EMITEN (bid/offer) -> ``research.orderbook``.

Modul ini mengubah sinyal yang tersedia itu menjadi SATU skor akumulasi per
emiten. Aturan yang dipegang:

1. **Skor hanya dari faktor yang LOLOS UJI.** Ambangnya identik dengan gate
   composite repo (``|mean IC| >= 0.05`` dan ``|ICIR| >= 0.5``). Faktor di luar
   ambang diberi bobot 0 dan TIDAK boleh menggerakkan skor.
2. **Arah ditentukan data, bukan asumsi.** Tanda kontribusi mengikuti tanda
   mean IC faktor itu; tidak ada asumsi "net beli -> pasti naik".
3. **Bobot proporsional |IC|**, dinormalisasi jumlah 1.
4. **Nilai faktor di-percentile lintas pasar** (bukan cuma watchlist) supaya
   ranking antar emiten bermakna, lalu dipetakan ke rentang skor 0..100.
5. **No look-ahead.** Nilai faktor di ``(code, T)`` hanya dari data ``<= T``
   (``research.factors``), dan IC-nya dibaca dari run terbaru yang sudah
   tersimpan — bukan dihitung dari data masa depan.
6. **Tidak ada faktor lolos -> tidak ada ranking.** Halaman menampilkan status
   "belum tervalidasi" alih-alih skor rekaan.

Konsumen skor ini: halaman Aktivitas Broker (ranking), filter Screener
(`min_broker_score`), lapisan verdict Hold Check (`apply_broker_layer`), kartu
detail emiten (riwayat + pembanding sektor), dan rotasi sektor
(:func:`sector_score_history`). Semuanya membaca SATU snapshot yang sama supaya
angkanya tidak pernah berbeda antar halaman — lihat
``api.analytics.broker_activity_snapshot``.

Modul ini sengaja bebas dari akses DB untuk logika intinya (hanya menerima
DataFrame), supaya bisa dites tanpa Postgres: pola yang sama dengan
``research.orderbook``.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .factors import FACTOR_DEFINITIONS
from .ic_history import ELIGIBLE_ABS_IC, ELIGIBLE_ABS_ICIR, WEIGHT_HORIZON

# Faktor yang membentuk skor Aktivitas Broker: keluarga flow bernotasi rupiah
# (sumber: foreign_net per emiten) + faktor order book (sumber: snapshot
# intraday). Faktor generik (momentum, volatilitas, likuiditas) sengaja TIDAK
# diikutkan — menu ini soal jejak aliran dana, bukan screening teknikal.
BROKER_FACTORS: tuple[str, ...] = (
    "flow_net_5d",
    "flow_net_21d",
    "flow_accel",
    "flow_consistency_21d",
    "flow_activity_21d",
    "foreign_net_pct",
    "foreign_streak",
    "ob_imbalance",
    "ob_absorption",
)

# Label pendek untuk UI (deskripsi panjang tetap dari FACTOR_DEFINITIONS).
FACTOR_LABELS: dict[str, str] = {
    "flow_net_5d": "Arus asing 5 hari",
    "flow_net_21d": "Arus asing 21 hari",
    "flow_accel": "Percepatan arus",
    "flow_consistency_21d": "Konsistensi arah",
    "flow_activity_21d": "Intensitas aktivitas",
    "foreign_net_pct": "Foreign net % value",
    "foreign_streak": "Streak net asing",
    "ob_imbalance": "Ketimpangan buku",
    "ob_absorption": "Absorption buku",
}

# Emiten yang faktornya tersedia kurang dari porsi ini dibuang dari ranking:
# skornya dibentuk dari sebagian kecil faktor saja, jadi tidak sebanding.
MIN_FACTOR_COVERAGE = 0.5

# Berapa driver (kontributor terbesar) yang dikirim ke UI per emiten.
DRIVER_COUNT = 3

# Ambang "emiten sedang diakumulasi" untuk breadth sektor. 60 dipilih karena
# skor 50 = median pasar, jadi 60 berarti jelas di atas rata-rata — bukan angka
# yang dikalibrasi ulang dari data.
BREADTH_THRESHOLD = 60.0

# Pemisah kuadran rotasi: level di 50 (median pasar) dan delta di 0.
ROTATION_LEVEL_SPLIT = 50.0
# Perubahan median skor <= ini dianggap tidak bergerak (poin skor). Tanpa
# toleransi, delta tepat 0 akan dilabeli "MEMUDAR"/"TERPURUK" — mengklaim ada
# arah padahal perubahannya nol.
ROTATION_DELTA_EPS = 0.5
ROTATION_PHASES: tuple[str, ...] = (
    "AKUMULASI",
    "MEMUDAR",
    "MEMBAIK",
    "TERPURUK",
    "STABIL",
)


def _finite(v: Any) -> float | None:
    """None/NaN/inf -> None; angka lain -> float. Menjaga output JSON bersih."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# --------------------------------------------------------------------------- #
# Gate IC -> faktor + bobot + arah
# --------------------------------------------------------------------------- #


def select_factors(
    ic_rows: Iterable[dict[str, Any]],
    horizon: int = WEIGHT_HORIZON,
    min_abs_ic: float = ELIGIBLE_ABS_IC,
    min_abs_icir: float = ELIGIBLE_ABS_ICIR,
) -> list[dict[str, Any]]:
    """Saring baris IC -> faktor keluarga broker, lengkap bobot & arah (pure).

    ``ic_rows``: baris ``research.factor_ic_history`` (dict dengan key
    ``factor, horizon, mean_ic, icir, t_stat, hit_rate, n_days``).

    Bobot > 0 HANYA untuk faktor yang lolos gate. Faktor tidak lolos tetap
    dikembalikan (bobot 0) supaya UI bisa menampilkan apa yang diuji dan gagal
    — transparansi, bukan menyembunyikan yang tidak lulus.
    """
    picked: list[dict[str, Any]] = []
    for r in ic_rows:
        factor = str(r.get("factor") or "")
        if factor not in BROKER_FACTORS:
            continue
        raw_horizon = r.get("horizon")
        if raw_horizon is not None and int(raw_horizon) != horizon:
            continue

        ic = _finite(r.get("mean_ic"))
        icir = _finite(r.get("icir"))
        eligible = (
            ic is not None
            and icir is not None
            and abs(ic) >= min_abs_ic
            and abs(icir) >= min_abs_icir
        )
        picked.append(
            {
                "factor": factor,
                "label": FACTOR_LABELS.get(factor, factor),
                "description": FACTOR_DEFINITIONS.get(factor),
                "mean_ic": ic,
                "icir": icir,
                "t_stat": _finite(r.get("t_stat")),
                "hit_rate": _finite(r.get("hit_rate")),
                "n_days": int(r["n_days"]) if r.get("n_days") is not None else None,
                "eligible": eligible,
                # Arah = tanda mean IC. Dibiarkan 0 saat tidak lolos supaya
                # tidak ada kontribusi yang diam-diam masuk lewat bobot 0.
                "direction": (1.0 if (ic or 0.0) > 0 else -1.0) if eligible else 0.0,
                "weight": 0.0,
            }
        )

    eligible_rows = [p for p in picked if p["eligible"]]
    total = sum(abs(p["mean_ic"] or 0.0) for p in eligible_rows)
    if total > 0:
        for p in eligible_rows:
            p["weight"] = abs(p["mean_ic"] or 0.0) / total

    # Faktor lolos dulu, lalu |IC| desc: yang paling berpengaruh tampil di atas.
    picked.sort(key=lambda p: (not p["eligible"], -abs(p["mean_ic"] or 0.0)))
    return picked


def eligible_factors(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hanya faktor yang lolos gate DAN berbobot > 0 (yang benar-benar dipakai)."""
    return [s for s in selected if s["eligible"] and s["weight"] > 0]


# --------------------------------------------------------------------------- #
# Nilai faktor terbaru -> skor komposit
# --------------------------------------------------------------------------- #


def percentile_ranks(values: pd.Series) -> pd.Series:
    """Percentile cross-sectional (0..1]; NaN tetap NaN (bukan diisi 0)."""
    return values.rank(pct=True, method="average")


def latest_factor_rows(
    panel: pd.DataFrame,
    factors: Iterable[str] | None = None,
) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Baris tanggal terakhir per emiten dari panel faktor.

    Returns ``(snapshot, as_of)``. Tanggal terakhir diambil dari seluruh panel
    (bukan per emiten) supaya semua emiten di-ranking pada hari bursa yang sama
    — kalau per emiten, emiten yang datanya telat akan "menang" karena barisnya
    lebih lama.
    """
    if panel.empty:
        return pd.DataFrame(columns=["code", "close"]), None

    as_of = pd.to_datetime(panel["date"]).max()
    snap = panel[pd.to_datetime(panel["date"]) == as_of].copy()

    cols = ["code", "close", *(factors or [])]
    keep = [c for c in dict.fromkeys(cols) if c in snap.columns]
    return snap[keep].reset_index(drop=True), as_of


def _score_matrix(
    snapshot: pd.DataFrame,
    active: list[dict[str, Any]],
) -> tuple[pd.Series, pd.Series, pd.DataFrame, pd.DataFrame] | None:
    """Skor, coverage, kontribusi, dan percentile untuk SATU tanggal (pure).

    Percentile dihitung lintas emiten DI TANGGAL ITU — itu inti skornya, jadi
    dipisah ke sini supaya pemanggil per-tanggal (riwayat) memakai matematika
    yang persis sama dengan pemanggil satu-tanggal (ranking pasar).

    Returns ``None`` kalau tidak ada yang bisa dihitung (frame kosong / tidak ada
    faktor aktif). Gate coverage TIDAK diterapkan di sini — pemanggil yang
    memutuskan cara menanganinya.
    """
    if snapshot.empty or not active:
        return None

    pct = pd.DataFrame(index=snapshot.index)
    for s in active:
        f = s["factor"]
        pct[f] = (
            percentile_ranks(pd.to_numeric(snapshot[f], errors="coerce"))
            if f in snapshot.columns
            else np.nan
        )

    factor_cols = [s["factor"] for s in active]
    coverage = pct[factor_cols].notna().sum(axis=1) / float(len(factor_cols))

    # Kontribusi bertanda per faktor, lalu dijumlah berbobot.
    contrib = pd.DataFrame(index=snapshot.index)
    for s in active:
        f = s["factor"]
        # fillna(0.5) -> (2*0.5-1) = 0: faktor tanpa nilai tidak menggeser skor.
        centred = 2.0 * pct[f].fillna(0.5) - 1.0
        contrib[f] = s["weight"] * s["direction"] * centred

    return 50.0 + 50.0 * contrib.sum(axis=1), coverage, contrib, pct


def _drivers_for(
    contrib_row: pd.Series,
    pct_row: pd.Series,
    count: int,
) -> list[dict[str, Any]]:
    """Kontributor terbesar satu emiten, berikut percentile mentahnya.

    Dipakai untuk menjelaskan ke UI "kenapa skornya begitu": besar dorongan
    (``contribution``) plus posisi emiten di pasar (``percentile``).
    """
    row = contrib_row.dropna()
    if row.empty:
        return []
    row = row.reindex(row.abs().sort_values(ascending=False).index)[:count]
    return [
        {
            "factor": f,
            "label": FACTOR_LABELS.get(f, f),
            "contribution": float(v),
            "percentile": _finite(pct_row.get(f)),
        }
        for f, v in row.items()
    ]


def composite_scores(
    latest: pd.DataFrame,
    selected: list[dict[str, Any]],
    min_coverage: float = MIN_FACTOR_COVERAGE,
    driver_count: int = DRIVER_COUNT,
) -> pd.DataFrame:
    """Skor akumulasi 0..100 per emiten dari faktor yang lolos gate (pure).

    Skor = 50 + 50 * Σ over faktor aktif: ``w_f · sign(IC_f) · (2·pct_f − 1)``,
    dengan ``pct_f`` = percentile cross-sectional nilai faktor (0..1). Karena
    tanda mengikuti IC, skor tinggi SELALU berarti "kombinasi yang historis
    bergerak searah return positif" — bukan asumsi arah manual.

    Faktor yang nilainya kosong di emiten tertentu diperlakukan NETRAL
    (kontribusi 0), dan ``coverage`` melaporkan berapa porsi faktor yang
    benar-benar tersedia. Emiten dengan coverage < ``min_coverage`` DIBUANG,
    bukan diberi skor dari sebagian kecil faktor.

    Tanpa faktor aktif -> DataFrame kosong: pemanggil harus menampilkan status
    "belum tervalidasi", bukan angka.
    """
    empty = pd.DataFrame(columns=["code", "close", "score", "coverage", "drivers"])
    matrix = _score_matrix(latest, eligible_factors(selected))
    if matrix is None:
        return empty
    score, coverage, contrib, pct = matrix

    keep = coverage >= min_coverage
    if not keep.any():
        return empty

    out = pd.DataFrame(
        {
            "code": latest["code"].astype(str).str.upper(),
            "close": pd.to_numeric(latest["close"], errors="coerce")
            if "close" in latest.columns
            else np.nan,
            "score": score,
            "coverage": coverage,
        },
        index=latest.index,
    ).loc[keep]

    out = out.copy()
    out["drivers"] = [
        _drivers_for(contrib.loc[i], pct.loc[i], driver_count) for i in out.index
    ]
    return out.reset_index(drop=True)


def composite_score_history(
    panel: pd.DataFrame,
    selected: list[dict[str, Any]],
    code: str,
    lookback: int = 60,
    min_coverage: float = MIN_FACTOR_COVERAGE,
    driver_count: int = DRIVER_COUNT,
) -> pd.DataFrame:
    """Riwayat skor harian untuk SATU emiten — percentile dihitung PER TANGGAL.

    Setiap tanggal di-score terhadap pasar HARI ITU. Ini bukan detail teknis:
    skor sebulan lalu yang diukur dengan percentile pasar hari ini tidak bisa
    dibandingkan dengan skor hari ini. Tanggal tempat emiten tidak lolos gate
    coverage DILEWATI (riwayatnya bolong), bukan diisi skor rekaan.

    ``panel``: panel multi-tanggal (kolom ``code``, ``date`` + kolom faktor).
    Returns DataFrame ``date, score, coverage, drivers`` urut tanggal menaik.
    """
    cols = ["date", "score", "coverage", "drivers"]
    active = eligible_factors(selected)
    if panel.empty or not active or lookback <= 0:
        return pd.DataFrame(columns=cols)
    if "date" not in panel.columns or "code" not in panel.columns:
        return pd.DataFrame(columns=cols)

    target_code = code.strip().upper()
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["code"] = df["code"].astype(str).str.upper()
    if not (df["code"] == target_code).any():
        return pd.DataFrame(columns=cols)

    rows: list[dict[str, Any]] = []
    for day in sorted(df["date"].unique())[-lookback:]:
        frame = df[df["date"] == day]
        pos = frame.index[frame["code"] == target_code]
        if len(pos) == 0:
            continue
        matrix = _score_matrix(frame, active)
        if matrix is None:
            continue
        score, coverage, contrib, pct = matrix
        i = pos[0]
        if float(coverage.loc[i]) < min_coverage:
            continue
        rows.append(
            {
                "date": day,
                "score": float(score.loc[i]),
                "coverage": float(coverage.loc[i]),
                "drivers": _drivers_for(contrib.loc[i], pct.loc[i], driver_count),
            }
        )

    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Agregasi sektor & rotasi
# --------------------------------------------------------------------------- #


def sector_score_history(
    panel: pd.DataFrame,
    selected: list[dict[str, Any]],
    sector_of: Mapping[str, str],
    lookback: int = 60,
    min_coverage: float = MIN_FACTOR_COVERAGE,
    min_names: int = 3,
    exclude_sectors: Collection[str] = (),
) -> pd.DataFrame:
    """Skor broker teragregasi per sektor per tanggal, plus breadth (pure).

    Untuk tiap tanggal skor seluruh emiten dihitung cross-sectional (percentile
    pasar HARI ITU — lihat ``_score_matrix``), lalu diagregasi per sektor:

    - ``median_score``: median skor. Median, bukan mean, karena satu emiten
      ekstrem tidak boleh mewakili seluruh sektor.
    - ``breadth``: porsi emiten dengan skor >= ``BREADTH_THRESHOLD`` — sektor
      yang isinya cuma 1-2 nama bagus bukan sektor yang diakumulasi.

    Sektor dengan emiten berskor < ``min_names`` DIBUANG: median dari satu-dua
    nama bukan gambaran sektor dan justru paling berisik saat diurutkan.

    ``sector_of`` dipetakan PEMANGGIL (bukan lookup di sini) supaya modul riset
    tetap bebas dari peta sektor spesifik aplikasi. Emiten tanpa pemetaan -> di-skip.

    Returns DataFrame ``date, sector, n_names, median_score, breadth``.
    """
    cols = ["date", "sector", "n_names", "median_score", "breadth"]
    active = eligible_factors(selected)
    if panel.empty or not active or lookback <= 0:
        return pd.DataFrame(columns=cols)
    if "date" not in panel.columns or "code" not in panel.columns:
        return pd.DataFrame(columns=cols)

    excluded = set(exclude_sectors)
    df = panel.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["_sector"] = df["code"].astype(str).str.upper().map(lambda c: sector_of.get(c))
    df = df[df["_sector"].notna() & ~df["_sector"].isin(excluded)]
    if df.empty:
        return pd.DataFrame(columns=cols)

    rows: list[dict[str, Any]] = []
    for day in sorted(df["date"].unique())[-lookback:]:
        frame = df[df["date"] == day]
        matrix = _score_matrix(frame, active)
        if matrix is None:
            continue
        score, coverage, _contrib, _pct = matrix
        keep = coverage >= min_coverage
        if not keep.any():
            continue

        sub = pd.DataFrame(
            {
                "sector": frame["_sector"],
                "score": score,
                "accum": score >= BREADTH_THRESHOLD,
            }
        )[keep]
        if sub.empty:
            continue

        grouped = sub.groupby("sector")
        agg = pd.DataFrame(
            {
                "n_names": grouped.size().astype(int),
                "median_score": grouped["score"].median(),
                "breadth": grouped["accum"].mean(),
            }
        )
        agg = agg[agg["n_names"] >= min_names]
        for sector, r in agg.iterrows():
            rows.append(
                {
                    "date": day,
                    "sector": str(sector),
                    "n_names": int(r["n_names"]),
                    "median_score": float(r["median_score"]),
                    "breadth": float(r["breadth"]),
                }
            )

    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows)


def rotation_phase(
    level: float | None,
    delta: float | None,
    eps: float = ROTATION_DELTA_EPS,
) -> str | None:
    """Kuadran rotasi dari LEVEL skor dan PERUBAHANNYA (pure).

    - ``AKUMULASI``: level tinggi & naik  -> dana masuk, sektor menguat
    - ``MEMUDAR``  : level tinggi & turun -> masih kuat tapi mulai ditinggalkan
    - ``MEMBAIK``  : level rendah & naik  -> mulai dipungut
    - ``TERPURUK`` : level rendah & turun -> ditinggalkan
    - ``STABIL``   : perubahannya di dalam ``eps`` — arahnya TIDAK diketahui,
      jadi sengaja tidak diklaim sebagai naik maupun turun

    Rotasi diukur dari PERUBAHAN, bukan level: sektor berlevel tinggi yang
    sedang menurun berbeda artinya dari yang sedang naik. Pemisahnya 50 (median
    pasar) dan 0 — titik netral skala, bukan angka hasil kalibrasi.
    """
    if level is None or delta is None:
        return None
    if abs(delta) <= eps:
        return "STABIL"

    high = level >= ROTATION_LEVEL_SPLIT
    rising = delta > 0.0
    if high and rising:
        return "AKUMULASI"
    if high:
        return "MEMUDAR"
    return "MEMBAIK" if rising else "TERPURUK"


# --------------------------------------------------------------------------- #
# Lapisan Hold Check
# --------------------------------------------------------------------------- #

# Anggaran penyesuaian skor hold-check dari lapisan aktivitas broker. Sengaja
# LEBIH KECIL dari lapisan faktor IC (±10 poin) dan jauh di bawah sinyal
# teknikal (±25): proksi aliran menggeser verdict, tidak menentukannya.
BROKER_MAX_ADJUSTMENT = 8.0

# Di luar ambang ini barulah alasan ditulis ke UI — skor yang praktis netral
# tidak perlu menghasilkan bullet yang mengaburkan alasan sebenarnya.
BROKER_REASON_HIGH = 65.0
BROKER_REASON_LOW = 35.0


def verdict_adjustment(
    broker_score: float | None,
    validated: bool,
    max_adjustment: float = BROKER_MAX_ADJUSTMENT,
) -> tuple[float, str | None]:
    """Penyesuaian skor hold-check dari skor aktivitas broker (pure).

    Skor broker adalah percentile cross-sectional (50 = median pasar), jadi
    dipetakan linier: 50 -> 0, 100 -> +max, 0 -> -max.

    Arah & peringkat relatif datang dari faktor yang lulus uji IC. Magnitudenya
    (``max_adjustment``) adalah anggaran rancangan yang ditetapkan di muka dan
    di-cap — bukan angka yang diklaim berasal dari data.

    **Hanya aktif saat ``validated``.** Kalau belum ada faktor aliran yang lulus
    gate IC, penyesuaiannya 0 dan tanpa alasan: hold-check berperilaku persis
    seperti sebelum fitur ini ada, bukan diberi angka tanpa dasar statistik.

    Returns ``(adjustment, reason | None)``.
    """
    if not validated or broker_score is None:
        return 0.0, None

    score = float(broker_score)
    adj = max(-max_adjustment, min(max_adjustment, max_adjustment * (score - 50.0) / 50.0))
    if adj == 0.0:
        return 0.0, None

    if score >= BROKER_REASON_HIGH:
        reason = (
            f"Aktivitas broker mendukung: skor aliran {score:.0f}/100 "
            f"(persentil pasar) — {adj:+.1f} poin"
        )
    elif score <= BROKER_REASON_LOW:
        reason = (
            f"Aktivitas broker melemah: skor aliran {score:.0f}/100 "
            f"(persentil pasar) — {adj:+.1f} poin"
        )
    else:
        reason = None
    return adj, reason


def apply_broker_layer(
    score: float,
    verdict: str,
    reasons: list[str],
    broker_score: float | None,
    validated: bool,
    max_adjustment: float = BROKER_MAX_ADJUSTMENT,
) -> tuple[float, str, list[str], float]:
    """Terapkan lapisan aktivitas broker ke satu emiten (pure, testable).

    Bentuknya mengikuti ``composite.apply_factor_layer`` supaya bisa dirangkai:
    Returns ``(new_score, new_verdict, new_reasons, adj)``. Tanpa skor broker,
    belum tervalidasi, atau penyesuaian nol -> masukan dikembalikan apa adanya.
    """
    from .composite import verdict_for  # satu sumber kebenaran band verdict

    adj, reason = verdict_adjustment(broker_score, validated, max_adjustment)
    if adj == 0.0:
        return score, verdict, reasons, 0.0

    new_score = max(0.0, min(100.0, score + adj))
    new_reasons = [*reasons, reason] if reason else list(reasons)
    return new_score, verdict_for(new_score), new_reasons, adj


# --------------------------------------------------------------------------- #
# Konsentrasi broker pasar (dari research.broker_daily)
# --------------------------------------------------------------------------- #


def concentration(rows: list[dict[str, Any]], top_n: int = 10) -> dict[str, Any]:
    """Konsentrasi transaksi per broker firma: CR1/CR3/CR5 + HHI (pure).

    Dihitung dari nilai transaksi broker untuk SATU sesi. Tinggi = porsi besar
    pasar dipegang sedikit firma (jejak pemain besar / institusi dominan);
    rendah = tersebar (ciri partisipasi retail).

    HHI di skala 0..1 (kalikan 10000 untuk skala antitrust klasik).
    """
    clean: list[dict[str, Any]] = []
    for r in rows:
        value = _finite(r.get("value")) or 0.0
        if value <= 0:
            continue
        clean.append(
            {
                "broker_code": str(r.get("broker_code") or "?"),
                "broker_name": r.get("broker_name"),
                "value": value,
                "volume": _finite(r.get("volume")),
                "frequency": _finite(r.get("frequency")),
            }
        )
    clean.sort(key=lambda c: c["value"], reverse=True)

    total = sum(c["value"] for c in clean)
    base = {
        "n_brokers": len(clean),
        "total_value": None,
        "cr1": None,
        "cr3": None,
        "cr5": None,
        "hhi": None,
        "top": [],
    }
    if total <= 0:
        return base

    shares = [c["value"] / total for c in clean]
    top: list[dict[str, Any]] = []
    for c, s in zip(clean[:top_n], shares[:top_n]):
        top.append({**c, "share": s})

    return {
        "n_brokers": len(clean),
        "total_value": total,
        "cr1": shares[0],
        "cr3": sum(shares[:3]),
        "cr5": sum(shares[:5]),
        "hhi": sum(s * s for s in shares),
        "top": top,
    }
