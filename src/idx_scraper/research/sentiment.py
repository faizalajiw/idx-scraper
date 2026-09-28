"""Sentimen posisi & aliran — dihitung dari data yang SUDAH dimiliki.

Tidak ada sumber baru, tidak ada scraping, tidak ada API berbayar. "Sentimen"
di sini adalah **jejak perilaku uang di data**, bukan opini dari berita:

1. **Arus asing** — ``foreign_net / value`` hari terakhir, diubah ke **persentil
   cross-sectional** pasar. Rasio mentahnya sangat kecil (p50 ~ -0.008%,
   p90 |0.1%|) dan skalanya berbeda antar emiten, jadi memakai ambang absolut
   akan salah kalibrasi; peringkat pasar justru sebanding dan bebas konstanta
   ajaib — idiom yang sama dengan ``research.composite``.
2. **Ketimpangan buku** — ``ob_imbalance`` dari snapshot intraday
   (``research.orderbook``): bid lebih tebal = ada yang mengakumulasi diam-diam.
3. **Absorption** — ``ob_absorption``: buku melawan arah harga (offer tebal tapi
   harga tetap naik = pembeli menyerap penawaran).

Gauge pasar adalah skor ``0..100`` dari tiga besaran tak bersatuan: breadth
harga (berapa emiten naik), pergerakan IHSG, dan breadth arus asing (berapa
emiten dibeli asing) — semuanya ``0..1`` atau persen, jadi tidak butuh skala
rupiah.

Batasan yang dipegang:
- Snapshot intraday hari ``T`` memang informasi yang sudah tertutup pada close
  ``T`` — tidak ada look-ahead.
- Emiten tanpa data buku / flow dilewati, **bukan** diisi nol: nol berarti
  "netral", padahal artinya "tidak tahu".
- Hari EOD yang masih parsial (``value`` NULL) dilewati, karena rasio arus
  asing tidak bisa dihitung dari hari seperti itu.
- Skor ini penyesuai konteks, bukan sinyal beli/jual. Rule teknikal tetap
  dominan (lihat ``research.composite``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

WIB = timezone(timedelta(hours=7))

# --- bobot komponen emiten (jumlah 1; dinormalisasi ulang bila ada yang kosong)
WEIGHTS = {"foreign": 0.40, "imbalance": 0.35, "absorption": 0.25}

# Ambang label emiten.
LABEL_STRONG = 0.40
LABEL_MILD = 0.15

# Ambang gauge pasar (50 = netral).
MARKET_RISK_ON = 60.0
MARKET_RISK_OFF = 40.0

# Bobot komponen pasar.
MARKET_WEIGHTS = {"breadth": 0.45, "index": 0.30, "foreign": 0.25}

# Gerak IHSG harian 1.5% dianggap ekstrem (komponen pasar ±1).
INDEX_EXTREME_PCT = 1.5

# Persentil arus asing yang dianggap ekstrem (atas/bawah).
FOREIGN_RANK_HIGH = 0.80
FOREIGN_RANK_LOW = 0.20

# Default: buang emiten dengan nilai transaksi di bawah ini dari daftar sorotan
# (Rp 1 miliar) supaya daftar tidak didominasi saham tidur.
MIN_VALUE_DEFAULT = 1_000_000_000.0

# Hari dianggap layak dipakai kalau minimal separuh barisnya punya nilai
# transaksi. EOD yang baru masuk sering parsial (value/foreign_net masih NULL)
# — memakai max(trade_date) mentah akan menghasilkan sentimen kosong.
MIN_COMPLETE_SHARE = 0.5


# --------------------------------------------------------------------------- #
# Pure: komponen & skor
# --------------------------------------------------------------------------- #


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Persentil cross-sectional ``0..1`` (share nilai <= x) untuk tiap key.

    Sama idiomnya dengan ``composite.ranks_from_rows``: (rank+1)/n supaya
    peringkat terendah > 0 dan tertinggi = 1. Pure & testable.
    """
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: kv[1])
    n = len(ordered)
    return {k: (i + 1) / n for i, (k, _) in enumerate(ordered)}


def foreign_component(rank: float | None) -> float | None:
    """Persentil arus asing ``0..1`` -> ``[-1, 1]`` (p50 = 0)."""
    if rank is None:
        return None
    return _clamp(2.0 * float(rank) - 1.0)


def orderbook_component(ob_imbalance: float | None) -> float | None:
    """Ketimpangan buku sudah ``[-1, 1]`` — lewatkan apa adanya."""
    if ob_imbalance is None:
        return None
    return _clamp(float(ob_imbalance))


def absorption_component(ob_absorption: float | None) -> float | None:
    """Absorption sudah ``[-1, 1]`` — lewatkan apa adanya."""
    if ob_absorption is None:
        return None
    return _clamp(float(ob_absorption))


def label_for(score: float) -> str:
    """Label band skor emiten — satu sumber kebenaran untuk UI."""
    if score >= LABEL_STRONG:
        return "AKUMULASI KUAT"
    if score >= LABEL_MILD:
        return "AKUMULASI"
    if score <= -LABEL_STRONG:
        return "DISTRIBUSI KUAT"
    if score <= -LABEL_MILD:
        return "DISTRIBUSI"
    return "NETRAL"


@dataclass
class EmitenSentiment:
    score: float
    label: str
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def emiten_sentiment(
    foreign_rank: float | None = None,
    ob_imbalance: float | None = None,
    ob_absorption: float | None = None,
) -> EmitenSentiment:
    """Gabungkan komponen aliran/buku jadi skor sentimen satu emiten.

    Bobot dinormalisasi ulang atas komponen yang tersedia; tanpa satu pun
    komponen -> skor 0 (NETRAL) dengan ``components`` kosong, supaya pemanggil
    bisa membedakan "netral" dari "tidak ada data".
    """
    raw = {
        "foreign": foreign_component(foreign_rank),
        "imbalance": orderbook_component(ob_imbalance),
        "absorption": absorption_component(ob_absorption),
    }
    available = {k: v for k, v in raw.items() if v is not None}
    if not available:
        return EmitenSentiment(0.0, "NETRAL", {}, [])

    total_w = sum(WEIGHTS[k] for k in available)
    score = sum(WEIGHTS[k] * v for k, v in available.items()) / total_w

    reasons: list[str] = []
    if foreign_rank is not None:
        if foreign_rank >= FOREIGN_RANK_HIGH:
            reasons.append(
                f"Arus asing di persentil {foreign_rank:.0%} pasar — paling masuk hari ini"
            )
        elif foreign_rank <= FOREIGN_RANK_LOW:
            reasons.append(
                f"Arus asing di persentil {foreign_rank:.0%} pasar — paling keluar hari ini"
            )
    if ob_imbalance is not None:
        if ob_imbalance >= 0.25:
            reasons.append(
                f"Bid jauh lebih tebal (ketimpangan {ob_imbalance:+.2f}) — akumulasi diam-diam"
            )
        elif ob_imbalance <= -0.25:
            reasons.append(
                f"Offer jauh lebih tebal (ketimpangan {ob_imbalance:+.2f}) — distribusi diam-diam"
            )
    if ob_absorption is not None:
        if ob_absorption <= -0.2:
            reasons.append(
                "Buku melawan arah harga — pembeli menyerap penawaran (absorption)"
            )
        elif ob_absorption >= 0.2:
            reasons.append("Buku ikut arah harga — konfirmasi pergerakan")

    return EmitenSentiment(round(score, 4), label_for(score), raw, reasons)


def market_gauge(
    breadth_up: float | None,
    index_pct: float | None,
    foreign_breadth: float | None,
) -> dict[str, Any]:
    """Gauge sentimen pasar ``0..100`` dari breadth harga, IHSG, breadth asing.

    Semua komponen tak bersatuan: ``breadth_up``/``foreign_breadth`` adalah share
    (0..1), ``index_pct`` persen. Bobot dinormalisasi ulang atas yang tersedia.
    50 = netral. Pure & testable.
    """
    comps: dict[str, float] = {}
    if breadth_up is not None:
        comps["breadth"] = _clamp(2.0 * float(breadth_up) - 1.0)
    if index_pct is not None:
        comps["index"] = _clamp(float(index_pct) / INDEX_EXTREME_PCT)
    if foreign_breadth is not None:
        comps["foreign"] = _clamp(2.0 * float(foreign_breadth) - 1.0)

    if not comps:
        return {"score": 50.0, "label": "NETRAL", "components": {}}

    total_w = sum(MARKET_WEIGHTS[k] for k in comps)
    blended = sum(MARKET_WEIGHTS[k] * v for k, v in comps.items()) / total_w
    score = round(50.0 + 50.0 * blended, 1)
    if score >= MARKET_RISK_ON:
        label = "RISK-ON"
    elif score <= MARKET_RISK_OFF:
        label = "RISK-OFF"
    else:
        label = "NETRAL"
    return {"score": score, "label": label, "components": comps}


# --------------------------------------------------------------------------- #
# Agregasi
# --------------------------------------------------------------------------- #


def build(
    rows: list[dict[str, Any]],
    ob_by_code: dict[str, dict[str, float | None]] | None = None,
    index_pct: float | None = None,
    limit: int = 15,
    min_value: float = MIN_VALUE_DEFAULT,
) -> dict[str, Any]:
    """Susun respons sentimen dari snapshot terakhir + faktor order-book.

    Pure & testable: ``rows`` = snapshot terakhir per emiten (dict), ``ob_by_code``
    = faktor buku terakhir per emiten (``None`` bila capture tidak tersedia).

    Arus asing diubah ke persentil **lintas emiten yang punya data hari itu**,
    jadi peringkatnya selalu bermakna walau rasio mentahnya kecil.
    """
    ob_by_code = ob_by_code or {}
    up = down = flat = 0
    total_value = 0.0
    total_foreign = 0.0
    foreign_pos = 0
    foreign_n = 0
    ratios: dict[str, float] = {}
    prepared: list[dict[str, Any]] = []

    for r in rows:
        code = str(r.get("code", "")).upper()
        value = r.get("value")
        foreign_net = r.get("foreign_net")
        percent = r.get("percent")
        value_f = float(value) if value else None
        foreign_f = float(foreign_net) if foreign_net is not None else None

        if value_f:
            total_value += value_f
        if percent is not None:
            if percent > 0:
                up += 1
            elif percent < 0:
                down += 1
            else:
                flat += 1
        if foreign_f is not None:
            foreign_n += 1
            total_foreign += foreign_f
            if foreign_f > 0:
                foreign_pos += 1

        ratio = (
            foreign_f / value_f
            if foreign_f is not None and value_f and value_f > 0
            else None
        )
        if ratio is not None:
            ratios[code] = ratio
        prepared.append(
            {
                "code": code,
                "name": r.get("name"),
                "close": _num(r.get("close")),
                "percent": _num(percent),
                "value": _num(value_f),
                "foreign_net": _num(foreign_f),
                "foreign_net_pct": None if ratio is None else round(ratio, 6),
                "ob_imbalance": None,
                "ob_absorption": None,
                "_ratio": ratio,
            }
        )

    ranks = percentile_ranks(ratios)

    items: list[dict[str, Any]] = []
    for item in prepared:
        ob = ob_by_code.get(item["code"], {})
        sent = emiten_sentiment(
            foreign_rank=ranks.get(item["code"]),
            ob_imbalance=ob.get("ob_imbalance"),
            ob_absorption=ob.get("ob_absorption"),
        )
        if not sent.components:
            continue  # tidak ada dasar apa pun; jangan klaim netral
        item["ob_imbalance"] = ob.get("ob_imbalance")
        item["ob_absorption"] = ob.get("ob_absorption")
        item["score"] = sent.score
        item["label"] = sent.label
        item["reasons"] = sent.reasons
        item["foreign_rank"] = None if item["code"] not in ranks else round(ranks[item["code"]], 4)
        item.pop("_ratio", None)
        items.append(item)

    breadth_denom = up + down + flat
    breadth_up = (up / breadth_denom) if breadth_denom else None
    foreign_breadth = (foreign_pos / foreign_n) if foreign_n else None
    foreign_to_value = (total_foreign / total_value) if total_value else None
    market = market_gauge(breadth_up, index_pct, foreign_breadth)
    market.update(
        {
            "breadth_up": None if breadth_up is None else round(breadth_up, 4),
            "foreign_breadth": None if foreign_breadth is None else round(foreign_breadth, 4),
            "up": up,
            "down": down,
            "flat": flat,
            "index_percent": _num(index_pct),
            "total_value": _num(total_value),
            "total_foreign_net": _num(total_foreign),
            "foreign_to_value": None if foreign_to_value is None else round(foreign_to_value, 6),
        }
    )

    ranked = sorted(
        (i for i in items if (i["value"] or 0) >= min_value),
        key=lambda i: abs(i["score"]),
        reverse=True,
    )
    return {
        "market": market,
        "accumulation": [i for i in ranked if i["score"] >= LABEL_MILD][:limit],
        "distribution": [i for i in ranked if i["score"] <= -LABEL_MILD][:limit],
        "stats": {
            "analyzed": len(items),
            "accumulation": sum(1 for i in items if i["score"] >= LABEL_MILD),
            "distribution": sum(1 for i in items if i["score"] <= -LABEL_MILD),
            "neutral": sum(1 for i in items if -LABEL_MILD < i["score"] < LABEL_MILD),
        },
        "weights": dict(WEIGHTS),
    }


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 4)


# --------------------------------------------------------------------------- #
# DB orchestration
# --------------------------------------------------------------------------- #

_LATEST_SQL = """
with day_quality as (
    select trade_date,
           count(*) as n,
           count(*) filter (where value is not null and value > 0) as n_value
    from research.latest_pit
    group by trade_date
),
chosen as (
    select trade_date
    from day_quality
    where n > 0 and n_value >= %s * n
    order by trade_date desc
    limit 1
)
select l.code, l.name, l.trade_date, l.close, l.percent, l.volume,
       coalesce(l.value, l.close * l.volume) as value,
       l.foreign_net
from research.latest_pit l
join chosen c on c.trade_date = l.trade_date
where l.close is not null and l.close > 0
"""


def load_latest(dsn: str) -> list[dict[str, Any]]:
    """Snapshot hari EOD terakhir yang cukup lengkap (dict rows).

    Bukan sekadar ``max(trade_date)``: EOD yang baru masuk sering parsial, dan
    rasio arus asing mustahil dihitung dari hari seperti itu.
    """
    import psycopg
    from psycopg.rows import dict_row

    with (
        psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        cur.execute(_LATEST_SQL, (MIN_COMPLETE_SHARE,))
        return [dict(r) for r in cur.fetchall()]


def compute(
    dsn: str,
    index_pct: float | None = None,
    ob_by_code: dict[str, dict[str, float | None]] | None = None,
    limit: int = 15,
    min_value: float = MIN_VALUE_DEFAULT,
) -> dict[str, Any]:
    """Baca snapshot terakhir + order-book, susun respons sentimen lengkap."""
    rows = load_latest(dsn)
    out = build(rows, ob_by_code=ob_by_code, index_pct=index_pct, limit=limit, min_value=min_value)
    as_of = max((r["trade_date"] for r in rows if r.get("trade_date")), default=None)
    out["as_of"] = str(as_of) if as_of is not None else None
    out["generated_at"] = datetime.now(WIB).isoformat(timespec="seconds")
    out["orderbook_available"] = bool(ob_by_code)
    out["disclaimer"] = (
        "Sentimen ini dihitung dari jejak transaksi yang sudah tersimpan "
        "(arus asing peringkat pasar, ketimpangan buku intraday, breadth) — "
        "bukan berita atau opini. Dipakai sebagai konteks, bukan sinyal beli/jual."
    )
    return out
