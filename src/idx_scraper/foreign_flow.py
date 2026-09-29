"""Aliran asing per emiten: tren harian, flip, dan pembanding sektor (pure).

Kenapa modul ini pure
---------------------
Pola yang sama dengan ``idx_scraper.broker_flow``: semua perhitungan menerima
iterable baris dan tanpa akses DB supaya bisa dites tanpa Postgres, sementara
query-nya tinggal menempel di endpoint (``api.analytics``).

Sumber data & batasannya
------------------------
``research.latest_pit`` menyimpan ``foreign_buy/sell/net`` per emiten per hari —
agregat "asing" (semua firma asing digabung IDX), BUKAN per firma. Kolomnya
dalam **SAHAM** (lihat catatan ``research.factors``): untuk rupiah, kalikan
``close`` hari itu. Di modul ini semua nilai rupiah dihitung pemanggil/query
lewat baris ``{"date", "net_idr", ...}`` supaya fungsi di sini tidak tahu cara
konversinya.

Flip (pergantian arah net)
--------------------------
Definisi kecil tapi harus tegas: flip = net hari ini tandanya BEDA dengan net
sebelumnya yang BUKAN nol (hari net 0 atau tanpa data tidak menTrigger flip —
asing yang diam bukan pergantian arah). Streak menghitung hari net positif
(+) atau negatif (−) berturut-turut, hari nol mereset ke 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def _f(r: Mapping[str, Any], key: str = "net_idr") -> float | None:
    """Ambil nilai float; None/NaN/inf/invalid -> None (bukan 0)."""
    v = r.get(key)
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def daily_flow(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Tren harian net flow asing satu emiten, urut tanggal menaik (pure).

    ``rows``: baris per hari dengan minimal ``date`` dan ``net_idr`` (rupiah,
    sudah dikali close oleh query). ``buy_idr``/``sell_idr`` opsional.

    Returns daftar ``{date, net, buy, sell, streak, flip}``:
    - ``streak``: jumlah hari berturut-turut net bertanda sama (hari nol -> 0).
    - ``flip``: True hanya di hari tanda net berubah vs hari non-nol sebelumnya.
      Hari pertama tidak pernah flip (belum ada pembanding).
    """
    out: list[dict[str, Any]] = []
    last_nonzero_sign = 0
    streak = 0
    for r in sorted(rows, key=lambda x: str(x.get("date") or "")):
        net = _f(r)
        buy = _f(r, "buy_idr")
        sell = _f(r, "sell_idr")
        s = _sign(net) if net is not None else 0
        flip = bool(last_nonzero_sign != 0 and s != 0 and s != last_nonzero_sign)
        if s == 0:
            streak = 0
        elif s == last_nonzero_sign:
            streak += 1
        else:
            streak = 1
        if s != 0:
            last_nonzero_sign = s
        out.append(
            {
                "date": str(r.get("date")),
                "net": net,
                "buy": buy,
                "sell": sell,
                "streak": streak if net is not None else None,
                "flip": flip if net is not None else None,
            }
        )
    return out


def flip_summary(flow: list[dict[str, Any]]) -> dict[str, Any]:
    """Ringkasan flip + streak dari keluaran :func:`daily_flow` (pure).

    Returns ``last_flip`` (dict ``date, to`` dengan ``to`` = "net_buy"/"net_sell",
    None kalau belum pernah ada net non-nol), ``days_since_flip``,
    ``current_streak``, ``current_side`` ("net_buy"/"net_sell"/"flat").
    """
    last_flip: dict[str, Any] | None = None
    last_flow: dict[str, Any] | None = None
    for point in flow:
        if point["net"] is not None:
            last_flow = point
        if point.get("flip"):
            last_flip = {"date": point["date"], "to": "net_buy" if point["net"] > 0 else "net_sell"}

    if last_flow is None or last_flow["net"] is None:
        return {
            "last_flip": None,
            "days_since_flip": None,
            "current_streak": None,
            "current_side": "flat",
        }

    if last_flip is not None and last_flip["date"] == last_flow["date"]:
        days_since = 0
    elif last_flip is not None:
        dates = [p["date"] for p in flow if p["net"] is not None]
        days_since = len(dates) - 1 - dates.index(last_flip["date"])
    else:
        days_since = None

    streak = last_flow["streak"]
    net = last_flow["net"]
    return {
        "last_flip": last_flip,
        "days_since_flip": days_since,
        "current_streak": streak,
        "current_side": "net_buy" if net > 0 else ("net_sell" if net < 0 else "flat"),
    }


def sector_peer_flow(
    self_rows: Iterable[Mapping[str, Any]],
    peer_rows_by_code: Mapping[str, Iterable[Mapping[str, Any]]],
    days: int = 10,
) -> list[dict[str, Any]]:
    """Pembanding net flow asing per emiten se-sektor (pure).

    ``self_rows``/``peer_rows_by_code``: baris harian sama seperti
    :func:`daily_flow` (``date`` + ``net_idr`` rupiah). Nilai pembanding adalah
    **jumlah net N hari terakhir** per emiten — bukan rata-rata harian — supaya
    emiten yang data-nya bolong tidak diuntungkan/disalahkan; emiten dengan
    seluruh nilai None di jendela tidak masuk daftar (tanpa data ≠ nol).

    Returns daftar urut menurun berdasar ``net_sum``: kode, nama opsional,
    ``net_sum``, ``net_mean``, ``n_days`` (hari bernilai di jendela), dan
    ``is_self``. Emiten sendiri SELALU disertakan meski di luar top-N.
    """
    window_self = daily_flow(self_rows)[-days:] if days > 0 else []
    self_sum = sum(p["net"] for p in window_self if p["net"] is not None)
    self_n = sum(1 for p in window_self if p["net"] is not None)

    out: list[dict[str, Any]] = []
    for code, rows in peer_rows_by_code.items():
        window = daily_flow(rows)[-days:] if days > 0 else []
        vals = [p["net"] for p in window if p["net"] is not None]
        if not vals:
            continue
        out.append(
            {
                "code": code,
                "net_sum": sum(vals),
                "net_mean": sum(vals) / len(vals),
                "n_days": len(vals),
                "is_self": False,
            }
        )
    if self_n > 0:
        out.append(
            {
                "code": "__self__",
                "net_sum": self_sum,
                "net_mean": self_sum / self_n,
                "n_days": self_n,
                "is_self": True,
            }
        )

    out.sort(key=lambda x: x["net_sum"], reverse=True)
    return out


def rank_in_peers(peers: list[dict[str, Any]]) -> dict[str, Any]:
    """Posisi emiten sendiri di antara peer (pure, pasangan :func:`sector_peer_flow`).

    Returns ``rank`` (1 = akumulasi asing terbesar di jendela), ``count``,
    ``median_sum`` (median net_sum peer — baseline "typical" sektor).
    """
    if not peers:
        return {"rank": None, "count": 0, "median_sum": None}
    sums = sorted(p["net_sum"] for p in peers)
    count = len(sums)
    mid = count // 2
    median = (
        float(sums[mid])
        if count % 2 == 1
        else float((sums[mid - 1] + sums[mid]) / 2)
    )
    pos = [i for i, p in enumerate(peers) if p.get("is_self")]
    return {
        "rank": (pos[0] + 1) if pos else None,
        "count": count,
        "median_sum": median,
    }
