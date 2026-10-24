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

DUA UKURAN, DUA JAWABAN
-----------------------
``outcome`` (level kena atau tidak) dan ``abn_k`` (hasil relatif pasar) adalah dua
hal berbeda dan bisa berbeda ketersediaannya: klaim bisa sudah kena target dari
harga mentah sementara panel terkoreksinya belum punya cukup sesi, sehingga
``abn_k``-nya ``NaN``. Karena itu pembaca agregat harus memakai
``session_aggregate["measured_claims"]`` — jumlah klaim yang **punya angka** —
bukan ``claims`` (semua baris), kalau yang mau dihitung adalah bukti.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .signal_log import DEFAULT_HORIZONS, NLAGS_DEFAULT, attach_outcomes

_CLAIM_COLUMNS = ("code", "date", "direction", "stop", "target", "horizon_days")


def _raw_bars(raw: pd.DataFrame) -> dict[str, dict[pd.Timestamp, tuple[float, float]]]:
    """Harga mentah -> per emiten: ``{tanggal: (high, low)}``.

    Baris tanpa high/low dibuang: tanpa keduanya, sentuhan level tidak bisa
    diperiksa, dan menganggapnya "tidak kena" berarti mengarang hasil.
    """
    df = raw[["code", "date", "high", "low"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["high", "low"])

    bars: dict[str, dict[pd.Timestamp, tuple[float, float]]] = {}
    for code, date, high, low in df.itertuples(index=False, name=None):
        bars.setdefault(str(code), {})[date] = (float(high), float(low))
    return bars


def _resolve(
    observed: list[tuple[pd.Timestamp, float, float]],
    stop: float,
    target: float,
) -> tuple[str, bool, bool, str | None] | None:
    """Hasil bila sebuah level tersentuh di jendela; ``None`` bila tidak ada.

    Berhenti di sentuhan pertama, dan ``hit_stop``/``hit_target`` mencatat apa yang
    terjadi **sampai klaim selesai** — sentuhan setelah klaim ditutup tidak lagi
    relevan dan tidak dihitung.
    """
    for date, high, low in observed:
        if low <= stop:  # stop menang bila sesi yang sama menyentuh keduanya
            return "stop", True, high >= target, date.strftime("%Y-%m-%d")
        if high >= target:
            return "target", False, True, date.strftime("%Y-%m-%d")
    return None


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

    Kalender yang dipakai adalah **sesi milik ``adj``** — panel yang sama yang
    menghasilkan ``abn_k`` — supaya jendela sentuhan dan jendela hasil tidak
    bergeser diam-diam. Sesi yang hilang dari ``raw`` di dalam jendela membuat
    klaim ``unresolved``: hari yang tidak teramati tidak boleh dianggap sudah lewat.
    Sesi yang hilang **setelah** klaim selesai tidak berpengaruh.
    """
    missing = [name for name in _CLAIM_COLUMNS if name not in claims.columns]
    if missing:
        raise ValueError(f"kolom klaim kurang: {', '.join(missing)}")
    rejected = sorted({str(d) for d in claims["direction"] if str(d) != "BUY"})
    if rejected:
        raise ValueError(f"arah klaim belum didukung: {', '.join(rejected)} (baru BUY)")

    horizons = tuple(horizons)
    abn_cols = [f"abn_{k}" for k in horizons]
    bars_by_code = _raw_bars(raw)

    measured = attach_outcomes(adj, horizons, nlags)
    sessions_by_code: dict[str, list[pd.Timestamp]] = {}
    for code, group in measured.groupby("code", sort=False):
        sessions_by_code[str(code)] = list(group["date"])

    out = claims.copy()
    out["date"] = pd.to_datetime(out["date"])

    outcomes: list[str] = []
    hit_stops: list[bool] = []
    hit_targets: list[bool] = []
    touches: list[str | None] = []
    for row in out.itertuples(index=False):
        code = str(row.code)
        sessions = sessions_by_code.get(code) or []
        index = (
            sessions.index(pd.Timestamp(row.date)) if pd.Timestamp(row.date) in sessions else None
        )

        horizon_days = int(row.horizon_days)
        expected: list[pd.Timestamp] = []
        if index is not None:
            expected = sessions[index + nlags + 1 : index + nlags + 1 + horizon_days]

        bars = bars_by_code.get(code) or {}
        observed: list[tuple[pd.Timestamp, float, float]] = []
        for date in expected:
            bar = bars.get(date)
            if bar is None:
                break  # bar hilang: hari ini tidak teramati, jangan lompati
            observed.append((date, bar[0], bar[1]))

        resolved = _resolve(observed, float(row.stop), float(row.target)) if observed else None
        if resolved is None:
            complete = len(expected) == horizon_days and len(observed) == len(expected)
            resolved = (
                ("timeout", False, False, None) if complete else ("unresolved", False, False, None)
            )

        outcomes.append(resolved[0])
        hit_stops.append(resolved[1])
        hit_targets.append(resolved[2])
        touches.append(resolved[3])

    out = out.merge(measured[["code", "date", *abn_cols]], on=["code", "date"], how="left")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["outcome"] = outcomes
    out["hit_stop"] = hit_stops
    out["hit_target"] = hit_targets
    out["touch_date"] = touches

    return out[[*claims.columns, *abn_cols, "hit_stop", "hit_target", "touch_date", "outcome"]]


def session_aggregate(outcomes: pd.DataFrame, *, horizon: int) -> dict[str, Any]:
    """Agregat hasil klaim **per sesi**, bukan per klaim (spec §6.1, R7).

    Rata-rata dihitung dua kali: lebih dulu lintas klaim di dalam satu tanggal,
    lalu lintas tanggal. Tiga sampai lima klaim di hari yang sama adalah SATU
    pengamatan — dihitung per baris, hari yang ramai otomatis lebih berpengaruh
    daripada hari yang sepi, dan yang ingin dijawab bukan itu.

    ``claims`` menghitung semua klaim yang pernah ditulis; ``measured_claims``
    hanya yang punya angka (``abn_{horizon}`` berhingga). ``sessions`` menghitung
    sesi yang menyumbang angka terukur. Klaim ``unresolved`` tetap dihitung di
    ``unresolved`` tapi tidak menambah sesi dan abnormalnya tidak dianggap nol.
    """
    column = f"abn_{horizon}"
    missing = [name for name in ("date", column, "outcome") if name not in outcomes.columns]
    if missing:
        raise ValueError(f"kolom hasil kurang: {', '.join(missing)}")

    counts = outcomes["outcome"].value_counts().to_dict() if not outcomes.empty else {}
    summary: dict[str, Any] = {
        "claims": len(outcomes),
        "measured_claims": 0,
        "sessions": 0,
        "mean_abnormal": None,
        "median_session_abnormal": None,
        "session_dispersion": None,
        "hit_target": int(counts.get("target", 0)),
        "hit_stop": int(counts.get("stop", 0)),
        "timeout": int(counts.get("timeout", 0)),
        "unresolved": int(counts.get("unresolved", 0)),
        "per_session": [],
    }
    if outcomes.empty:
        return summary

    frame = pd.DataFrame(
        {
            "date": outcomes["date"].astype(str).str.slice(0, 10),
            "abn": pd.to_numeric(outcomes[column], errors="coerce"),
        }
    ).dropna(subset=["abn"])
    if frame.empty:
        return summary

    grouped = frame.groupby("date")["abn"].agg(["size", "mean"]).sort_index()
    session_means = grouped["mean"]
    summary["measured_claims"] = len(frame)
    summary["sessions"] = len(session_means)
    summary["mean_abnormal"] = round(float(session_means.mean()), 6)
    summary["median_session_abnormal"] = round(float(session_means.median()), 6)
    if len(session_means) >= 2:
        summary["session_dispersion"] = round(float(session_means.std(ddof=1)), 6)
    summary["per_session"] = [
        {
            "date": str(date),
            "n": int(entry["size"]),
            "mean_abnormal": round(float(entry["mean"]), 6),
        }
        for date, entry in grouped.iterrows()
    ]
    return summary


# --------------------------------------------------------------------------- #
# Verdict & rekaman (spec §6.1 "cara gagal", keputusan §8 Q4)
# --------------------------------------------------------------------------- #

MIN_CLAIMS = 30  # keputusan pemilik: 30 klaim TERUKUR, ...
MIN_SESSIONS = 30  # ... atau 6 minggu bursa (±30 sesi), mana yang lebih dulu
VERDICT_HORIZON = 21  # satu-satunya horizon dengan bukti OOS (IC h21 +0,038 / t 1,95)


def claim_verdict(
    agg: dict[str, Any],
    *,
    min_claims: int = MIN_CLAIMS,
    min_sessions: int = MIN_SESSIONS,
) -> dict[str, Any]:
    """Terbukti / tidak terbukti / belum cukup — dari agregat per sesi.

    Ambang klaim menghitung **klaim yang punya angka** (``measured_claims``), bukan
    baris yang tercatat: klaim baru matang setelah horizonnya berjalan, jadi
    menghitung baris membuat 30 klaim "tercapai" sebelum satu pun bisa dinilai —
    dan verdict akan berdiri di atas satu sesi. Kalau ``measured_claims`` tidak
    tersedia, dipakai ``claims - unresolved`` sebagai perkiraan terdekat.

    "Belum cukup" bukan kegagalan; itu jawaban jujur selama buktinya belum
    terkumpul, dan ia menyebut berapa yang sudah ada. Yang dilarang adalah
    memutuskan dari sampel yang belum layak.
    """
    claims = int(agg.get("claims") or 0)
    sessions = int(agg.get("sessions") or 0)
    mean = agg.get("mean_abnormal")

    measured = agg.get("measured_claims")
    if measured is None:
        measured = claims - int(agg.get("unresolved") or 0)
    measured = max(int(measured or 0), 0)

    if mean is None or sessions < 1:
        return {
            "status": "belum cukup",
            "reason": "belum ada hasil terukur",
            "claims": claims,
            "measured_claims": measured,
            "sessions": sessions,
            "mean_abnormal": None,
        }

    enough_claims = measured >= min_claims
    enough_sessions = sessions >= min_sessions
    if not enough_claims and not enough_sessions:
        return {
            "status": "belum cukup",
            "reason": (
                f"baru {measured} klaim terukur dari {min_claims} "
                f"dan {sessions} sesi dari {min_sessions}"
            ),
            "claims": claims,
            "measured_claims": measured,
            "sessions": sessions,
            "mean_abnormal": mean,
        }

    if enough_claims and enough_sessions:
        reason = (
            f"{measured} klaim terukur dan {sessions} sesi tercapai "
            f"(ambang {min_claims}/{min_sessions})"
        )
    elif enough_claims:
        reason = f"{measured} klaim terukur tercapai (ambang {min_claims})"
    else:
        reason = f"{sessions} sesi tercapai (ambang {min_sessions})"

    return {
        "status": "terbukti" if float(mean) > 0 else "tidak terbukti",
        "reason": reason,
        "claims": claims,
        "measured_claims": measured,
        "sessions": sessions,
        "mean_abnormal": mean,
    }


def _persen(value: float) -> str:
    """``0.014`` -> ``+1,4%`` (koma desimal, tanda plus untuk yang positif)."""
    return f"{float(value) * 100:+.1f}%".replace(".", ",")


def format_record_line(agg: dict[str, Any]) -> str:
    """Kalimat rekaman yang dibaca pengguna — satu baris, apa adanya.

    Inilah yang mengikat mesin pada klaimnya sendiri: disusun dari agregat hasil,
    bukan dari niat. Yang belum terukur ditulis "belum terukur", bukan 0,0% yang
    akan terbaca seolah tidak untung.
    """
    claims = int(agg.get("claims") or 0)
    if claims == 0:
        return "Aturan ini: belum punya rekaman"

    head = (
        f"Aturan ini: {claims} klaim · {int(agg.get('hit_target') or 0)} kena target"
        f" · {int(agg.get('hit_stop') or 0)} kena batas rugi"
    )
    mean = agg.get("mean_abnormal")
    if mean is None:
        return f"{head} · rata-rata belum terukur"
    return f"{head} · rata-rata {_persen(mean)} vs pasar"
