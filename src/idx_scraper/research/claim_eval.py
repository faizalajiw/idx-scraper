"""Hasil klaim mesin keputusan: ukur per klaim, agregat per sesi, lalu verdict.

KENAPA MODUL INI ADA
--------------------
``recommendation_log`` dan ``signal_log`` mengukur **sinyal** — kapan sebuah pola
teknikal muncul. Mesin keputusan menulis **klaim**: kalimat perintah yang mengikat
("beli X, stop S, target T, horizon H"). Klaim seperti itu harus bisa diukur apa
adanya, oleh alat yang tidak bisa dibengkokkan setelah kejadian. Modul ini
alatnya. Ia murni — tanpa DB, tanpa jaringan — supaya bisa diuji, dan supaya tidak
ada angka rekaman yang bergantung pada koneksi saat dibaca.

ATURAN YANG DIPEGANG (spec §6.1, §6.4)
--------------------------------------
1. **Dua dasar harga, dua peran.** Level (stop/target) ditetapkan dan diperiksa
   pada *harga mentah* — itu harga yang benar-benar bisa ditransaksikan. Hasil
   diukur pada *harga terkoreksi* (``abn_k``), supaya dividen dan split tidak
   terbaca sebagai untung atau rugi.
2. **Satu sesi menyentuh stop DAN target -> stop.** Urutan intra-sesi tidak
   diketahui dari data harian; mencatat yang lebih menguntungkan berarti
   membohongi diri sendiri.
3. **Sesi entry tidak memicu apa pun.** Entry di close T+``nlags``, jadi high/low
   sesi itu terjadi sebelum posisinya ada. Jendela risiko mulai sesi berikutnya
   dan panjangnya tepat ``horizon_days`` sesi.
4. **Jendela belum selesai bukan hasil nol.** Klaim yang belum punya cukup sesi
   disebut ``unresolved`` dan abnormalnya ``NaN``, bukan 0 yang ikut terhitung.
5. **Arah selain BUY ditolak** dengan error, bukan dihitung dengan aturan yang
   salah lalu dilaporkan sebagai bukti.
"""

from __future__ import annotations

import pandas as pd

from .signal_log import DEFAULT_HORIZONS, NLAGS_DEFAULT, attach_outcomes

_CLAIM_COLUMNS = ("code", "date", "direction", "stop", "target", "horizon_days")


def _raw_windows(raw: pd.DataFrame) -> dict[str, list[tuple[pd.Timestamp, float, float]]]:
    """Harga mentah -> per emiten: daftar (tanggal, high, low) urut tanggal."""
    df = raw[["code", "date", "high", "low"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["high", "low"]).sort_values(["code", "date"])

    windows: dict[str, list[tuple[pd.Timestamp, float, float]]] = {}
    for code, date, high, low in df.itertuples(index=False, name=None):
        windows.setdefault(str(code), []).append((date, float(high), float(low)))
    return windows


def _resolve(
    window: list[tuple[pd.Timestamp, float, float]],
    stop: float,
    target: float,
    horizon_days: int,
) -> tuple[str, bool, bool, str | None]:
    """Putuskan hasil dari jendela risiko; berhenti pada sentuhan pertama.

    ``hit_stop``/``hit_target`` mencatat apa yang terjadi **sampai klaim selesai** —
    sentuhan setelah klaim ditutup tidak lagi relevan dan tidak dihitung.
    """
    for date, high, low in window:
        in_stop = low <= stop
        in_target = high >= target
        if in_stop:  # stop menang bila sesi yang sama menyentuh keduanya
            return "stop", True, in_target, date.strftime("%Y-%m-%d")
        if in_target:
            return "target", False, True, date.strftime("%Y-%m-%d")
    if len(window) < horizon_days:
        return "unresolved", False, False, None
    return "timeout", False, False, None


def claim_outcomes(
    claims: pd.DataFrame,
    *,
    raw: pd.DataFrame,
    adj: pd.DataFrame,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    nlags: int = NLAGS_DEFAULT,
) -> pd.DataFrame:
    """Ukur hasil tiap klaim: sentuhan level (harga mentah) + abnormal (terkoreksi).

    ``claims`` butuh kolom ``code, date, direction, stop, target, horizon_days``.
    ``raw`` adalah harga mentah (``code, date, high, low, close``), ``adj`` harga
    terkoreksi (``code, date, close``). Mengembalikan seluruh kolom klaim ditambah
    ``abn_{k}`` per horizon, ``hit_stop``, ``hit_target``, ``touch_date``, dan
    ``outcome`` bernilai salah satu dari ``stop`` / ``target`` / ``timeout`` /
    ``unresolved``.
    """
    missing = [name for name in _CLAIM_COLUMNS if name not in claims.columns]
    if missing:
        raise ValueError(f"kolom klaim kurang: {', '.join(missing)}")
    rejected = sorted({str(d) for d in claims["direction"] if str(d) != "BUY"})
    if rejected:
        raise ValueError(f"arah klaim belum didukung: {', '.join(rejected)} (baru BUY)")

    horizons = tuple(horizons)
    abn_cols = [f"abn_{k}" for k in horizons]
    windows = _raw_windows(raw)
    position = {
        code: {date: i for i, (date, _high, _low) in enumerate(rows)}
        for code, rows in windows.items()
    }

    out = claims.copy()
    out["date"] = pd.to_datetime(out["date"])

    outcomes: list[str] = []
    hit_stops: list[bool] = []
    hit_targets: list[bool] = []
    touches: list[str | None] = []
    for row in out.itertuples(index=False):
        code = str(row.code)
        index = position.get(code, {}).get(pd.Timestamp(row.date))
        rows = windows.get(code) or []
        if index is None:
            outcomes.append("unresolved")
            hit_stops.append(False)
            hit_targets.append(False)
            touches.append(None)
            continue
        horizon_days = int(row.horizon_days)
        start = index + nlags + 1
        window = rows[start : start + horizon_days]
        if not window:
            outcomes.append("unresolved")
            hit_stops.append(False)
            hit_targets.append(False)
            touches.append(None)
            continue
        outcome, hit_stop, hit_target, touch = _resolve(
            window, float(row.stop), float(row.target), horizon_days
        )
        outcomes.append(outcome)
        hit_stops.append(hit_stop)
        hit_targets.append(hit_target)
        touches.append(touch)

    measured = attach_outcomes(adj, horizons, nlags)[["code", "date", *abn_cols]]
    out = out.merge(measured, on=["code", "date"], how="left")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["outcome"] = outcomes
    out["hit_stop"] = hit_stops
    out["hit_target"] = hit_targets
    out["touch_date"] = touches

    return out[[*claims.columns, *abn_cols, "hit_stop", "hit_target", "touch_date", "outcome"]]
