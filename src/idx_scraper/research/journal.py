"""Buku klaim (lapis 4): aturan murni boleh-tidaknya klaim dan bentuk barisnya.

Fungsi murni: tanpa DB dan tanpa cache. Dipakai penulis klaim terjadwal
(``research.journal_store``), perintah ``idx journal``, dan lapisan API, supaya
gerbang klaim dan bentuk klaim tidak pernah punya dua definisi.

Sumber kebenaran aturan: ``docs/superpowers/specs/2026-10-19-spec-lapis-4-pengikat-design.md``,
dengan keputusan lapis 4 di ``docs/superpowers/specs/2026-10-18-spec-mesin-keputusan-design.md``
(D5 tulis sekali, 6.1 cara gagal, 6.2 pre-commit, Q1 daftar pendek).
"""

from __future__ import annotations

import math
from typing import Any

from ..freshness import SEGAR
from .claim_eval import VERDICT_HORIZON

#: Aturan yang melahirkan klaim hari ini: daftar pendek papan kandidat.
RULE_PAPAN = "papan-teratas"

#: Daftar pendek 3 sampai 5 nama per sesi (keputusan Q1); dipotong, bukan
#: diperbesar. Di data nyata lima sesi (12-16 Okt 2026) daftarnya 4, 5, 4, 5, 5.
SHORTLIST_SIZE = 5

#: Urutan sebab gerbang. Sebab pertama yang menolak itulah yang dilaporkan, dan
#: urutannya sengaja tetap: halt manual pemilik di atas segalanya.
GATE_REASONS = ("manual", "data", "regime", "verdict")

#: Sebab penolakan pre-commit (spec 6.2): klaim tidak lengkap tidak masuk buku.
REJECT_MISSING_STOP = "tanpa_stop"
REJECT_MISSING_TARGET = "tanpa_target"
REJECT_MISSING_SIZE = "tanpa_ukuran"
REJECT_GEOMETRY = "geometri"
REJECT_HORIZON = "horizon"
REJECT_EMPTY_POOL = "kolam_kosong"

#: Status baris buku. ``unresolved`` milik claim_eval sengaja tetap "terbuka":
#: hari yang belum teramati bukan hari yang sudah lewat.
STATUS_OPEN = "terbuka"
STATUS_TARGET = "kena_target"
STATUS_STOP = "kena_stop"
STATUS_EXPIRED = "kedaluwarsa"

#: Kolom buku klaim yang diisi Python. ``recorded_at`` diisi DB, jadi tidak ikut.
CLAIM_COLUMNS = (
    "code",
    "trade_date",
    "rule",
    "direction",
    "grade",
    "ref_raw",
    "stop_raw",
    "target_raw",
    "ref_adj",
    "horizon_days",
    "size_pct",
    "size_lots",
    "size_rupiah",
    "regime",
    "vol_state",
    "freshness_status",
    "status",
    "outcome_abn",
    "touch_date",
)


def _num(value: Any) -> float | None:
    """Angka finit atau None. Bool ditolak (True bukan 1 di konteks ini)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int_of(value: Any) -> int | None:
    n = _num(value)
    return None if n is None else int(n)


def claim_gate(
    *,
    manual_halt: bool = False,
    halt_reason: str | None = None,
    freshness_status: str | None = None,
    gate_blocked: bool = False,
    gate_reason: str | None = None,
    verdict: str | None = None,
) -> dict[str, Any]:
    """Boleh atau tidak menulis klaim baru, lengkap dengan sebab pertamanya.

    ``verdict`` adalah status verdict aturan (``claim_eval.claim_verdict``), bukan
    seluruh dictnya. ``freshness_status`` None bukan basi: pemanggil yang belum
    menyambungkan pemeriksa kesegaran tidak boleh dituduh menahan klaim.
    """
    if manual_halt:
        sebab = halt_reason or "tanpa sebab tertulis"
        return {
            "allowed": False,
            "reason": "manual",
            "message": f"Klaim baru dihentikan manual: {sebab}.",
        }
    if freshness_status is not None and freshness_status != SEGAR:
        return {
            "allowed": False,
            "reason": "data",
            "message": (
                f"Data pasar belum segar ({freshness_status}); klaim baru ditahan "
                "sampai sesi terakhir benar-benar masuk."
            ),
        }
    if gate_blocked:
        sebab = gate_reason or "gerbang rezim menutup"
        return {
            "allowed": False,
            "reason": "regime",
            "message": f"Gerbang rezim menolak klaim baru: {sebab}.",
        }
    if verdict == "tidak terbukti":
        return {
            "allowed": False,
            "reason": "verdict",
            "message": (
                "Verdict aturan ini tidak terbukti; klaim baru dihentikan sampai "
                "aturannya diperbaiki."
            ),
        }
    return {"allowed": True, "reason": None, "message": "Gerbang klaim terbuka."}


def claim_reject_reason(
    *,
    entry: Any,
    stop: Any,
    target: Any,
    size_pct: Any = None,
    size_lots: Any = None,
    horizon_days: Any = None,
) -> str | None:
    """Sebab klaim ditolak, atau None kalau lengkap. Urutannya tetap.

    Ini yang menjaga janji spec 6.2: klaim yang tidak bisa dieksekusi tidak
    pernah masuk buku, dan penolakannya punya nama supaya bisa dibaca.
    """
    harga_masuk = _num(entry)
    harga_stop = _num(stop)
    harga_target = _num(target)

    if harga_stop is None:
        return REJECT_MISSING_STOP
    if harga_target is None:
        return REJECT_MISSING_TARGET
    if (_num(size_pct) or 0.0) <= 0 and (_int_of(size_lots) or 0) <= 0:
        return REJECT_MISSING_SIZE
    if (
        harga_masuk is None
        or harga_masuk <= 0
        or harga_stop >= harga_masuk
        or harga_target <= harga_masuk
    ):
        return REJECT_GEOMETRY
    horizon = _int_of(horizon_days)
    if horizon is None or horizon < 1:
        return REJECT_HORIZON
    return None


def claim_from_candidate(
    candidate: dict[str, Any],
    *,
    trade_date: str,
    rule: str = RULE_PAPAN,
    ref_adj: float | None = None,
    regime: str | None = None,
    vol_state: str | None = None,
    freshness_status: str | None = None,
) -> dict[str, Any] | None:
    """Baris buku dari satu kandidat papan, atau None kalau tidak lengkap.

    Tidak ada angka yang dikarang: yang tidak ada di kandidat ditulis None, dan
    satu-satunya nilai yang ditetapkan modul ini adalah ``direction`` BUY dan
    ``status`` terbuka.
    """
    code = str(candidate.get("code") or "").strip()
    if not code:
        return None

    size_pct = _num(candidate.get("size_pct"))
    size_lots = _int_of(candidate.get("size_lots"))
    horizon = _int_of(candidate.get("horizon_days"))
    harga_masuk = _num(candidate.get("entry_ref"))
    harga_stop = _num(candidate.get("stop"))
    harga_target = _num(candidate.get("target"))

    ditolak = claim_reject_reason(
        entry=harga_masuk,
        stop=harga_stop,
        target=harga_target,
        size_pct=size_pct,
        size_lots=size_lots,
        horizon_days=horizon,
    )
    if ditolak is not None:
        return None

    return {
        "code": code,
        "trade_date": str(trade_date),
        "rule": rule,
        "direction": "BUY",
        "grade": candidate.get("grade"),
        "ref_raw": harga_masuk,
        "stop_raw": harga_stop,
        "target_raw": harga_target,
        "ref_adj": _num(ref_adj),
        "horizon_days": horizon,
        "size_pct": size_pct,
        "size_lots": size_lots,
        "size_rupiah": _num(candidate.get("size_rupiah")),
        "regime": regime,
        "vol_state": vol_state,
        "freshness_status": freshness_status,
        "status": STATUS_OPEN,
        "outcome_abn": None,
        "touch_date": None,
    }


def summarize_claims(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agregat buku: rata-rata per sesi dulu, baru antar sesi.

    Rata-rata per klaim akan membuat satu sesi dengan banyak nama mendominasi
    seluruh rekaman, dan rekaman itu yang dibaca verdict. Sesi tanpa hasil
    terukur tidak dihitung nol: ia tidak dihitung sama sekali.

    Dua hitungan sesi yang berbeda, jadi namanya dipisah: ``sessions`` menghitung
    sesi yang **menyumbang angka terukur** (arti yang sama dengan medan senama di
    ``claim_eval.session_aggregate``, dan itu yang dipakai verdict sebagai ambang
    enam minggu), sedangkan ``claim_sessions`` menghitung sesi yang menulis klaim,
    termasuk yang belum matang. Selisih keduanya adalah cerita "klaimnya sudah ada,
    hasilnya belum jalan", dan tanpa pemisahan ini ambang verdict bisa tercapai
    oleh 30 sesi tulis sementara rata-ratanya berdiri di atas 8 sesi.

    Baris yang horizonnya bukan ``VERDICT_HORIZON`` tetap dihitung sebagai klaim
    dan tetap muncul di ``horizon_mismatch``, tetapi tidak masuk ``measured_claims``,
    rata-rata, maupun verdict: rata-rata campur horizon bukan bukti untuk ambang
    yang dikunci di satu horizon, dan yang tidak sebanding lebih baik dinyatakan
    daripada dirata-ratakan.
    """
    per_sesi: dict[str, list[float]] = {}
    klaim_per_sesi: dict[str, int] = {}
    hit_target = hit_stop = expired = terbuka = mismatch = terukur = 0

    for row in rows:
        tanggal = str(row.get("trade_date"))
        per_sesi.setdefault(tanggal, [])
        klaim_per_sesi[tanggal] = klaim_per_sesi.get(tanggal, 0) + 1

        cocok_horizon = _int_of(row.get("horizon_days")) == VERDICT_HORIZON
        if not cocok_horizon:
            mismatch += 1

        hasil = _num(row.get("outcome_abn")) if cocok_horizon else None
        if hasil is not None:
            per_sesi[tanggal].append(hasil)
            terukur += 1

        status = row.get("status")
        if status == STATUS_TARGET:
            hit_target += 1
        elif status == STATUS_STOP:
            hit_stop += 1
        elif status == STATUS_EXPIRED:
            expired += 1
        elif status == STATUS_OPEN:
            terbuka += 1

    rata_sesi = [hasil for _, hasil in sorted(per_sesi.items()) if hasil]
    mean = (
        sum(sum(hasil) / len(hasil) for hasil in rata_sesi) / len(rata_sesi)
        if rata_sesi
        else None
    )

    return {
        "claims": len(rows),
        "measured_claims": terukur,
        "sessions": len(rata_sesi),
        "claim_sessions": len(klaim_per_sesi),
        "hit_target": hit_target,
        "hit_stop": hit_stop,
        "expired": expired,
        "open": terbuka,
        "horizon_mismatch": mismatch,
        "mean_abnormal": mean,
        "per_session": [
            {
                "trade_date": tanggal,
                "claims": klaim_per_sesi[tanggal],
                "mean_abnormal": (
                    sum(hasil) / len(hasil) if (hasil := per_sesi[tanggal]) else None
                ),
            }
            for tanggal in sorted(per_sesi)
        ],
    }
