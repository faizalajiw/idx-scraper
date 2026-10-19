"""Buku klaim lapis 4 untuk layar: ringkasan, daftar klaim, keadaan gerbang.

Seluruh angka dan aturannya datang dari ``research.journal_store`` dan
``research.journal`` — jalur yang sama dengan CLI dan job terjadwal. Layar tidak
pernah menghitung ulang apa pun; kalau ia punya definisi sendiri, ia jadi
implementasi kedua yang perlahan berbeda dari mesin yang diaturnya.

Yang gagal dibaca tidak dilaporkan sebagai nol dan tidak jadi 500: keadaan
gerbang yang tidak terbaca ditulis apa adanya di ``note`` dan gerbangnya
dibiarkan terbuka, karena API yang mati akan mematikan layar untuk masalah yang
bukan miliknya (pola fail-open yang sama dengan penahanan P4).
"""

from __future__ import annotations

from typing import Any

from .. import journal_halt
from ..research import journal_store
from ..research.journal import RULE_PAPAN, claim_gate
from .database import get_cursor
from .services import get_freshness

#: Batas baris satu sesi yang boleh diminta layar. Bukunya kecil, tapi batasnya
#: tetap ada supaya satu permintaan tidak bisa menarik seluruh riwayat.
CLAIM_LIMIT_MAX = 200

#: Kolom baris buku yang dipakai layar (``recorded_at`` diisi DB, tidak diikut).
CLAIM_FIELDS = (
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

_TANGGAL_FIELDS = ("trade_date", "touch_date")


def _teks(value: Any) -> str | None:
    """Tanggal dari DB jadi teks ISO; None tetap None (bukan "None")."""
    return None if value is None else str(value)


def _last_session(cur: Any) -> str | None:
    """Sesi bursa terakhir yang datanya sudah masuk (None kalau belum ada)."""
    cur.execute("select max(trade_date) as tanggal from research.raw_eod")
    row = cur.fetchone() or {}
    return _teks(row.get("tanggal"))


def _regime() -> tuple[bool, str | None]:
    """(gerbang rezim menutup?, sebabnya). Tidak terbaca berarti tidak menutup."""
    try:
        from . import analytics as analytics_api

        gate = (analytics_api.get_market_regime() or {}).get("gate") or {}
    except Exception:
        return False, None
    sebab = str(gate.get("reason") or gate.get("regime") or "") or None
    return bool(gate.get("blocked")), sebab


def _freshness() -> tuple[str | None, str | None]:
    """(status kesegaran, catatan kalau tidak terbaca)."""
    try:
        return str((get_freshness() or {}).get("status") or "") or None, None
    except Exception as e:
        return None, f"kesegaran belum terbaca: {e}"


def summary(*, rule: str | None = None, code: str | None = None) -> dict[str, Any]:
    """Agregat buku + kalimat rekaman + verdict. Bawaan: aturan papan."""
    with get_cursor() as cur:
        return journal_store.journal_summary(cur, rule=rule or RULE_PAPAN, code=code)


def claims(
    *, trade_date: str | None = None, code: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Baris buku klaim, urut kode; tanpa tanggal dipakai sesi bursa terakhir."""
    batas = max(1, min(int(limit or 50), CLAIM_LIMIT_MAX))
    with get_cursor() as cur:
        tanggal = _teks(trade_date) or _last_session(cur)
        if tanggal is None:
            return []
        sql = (
            f"select {', '.join(CLAIM_FIELDS)} from research.claim_journal"
            " where trade_date = %s"
        )
        params: list[Any] = [tanggal]
        if code:
            sql += " and upper(code) = %s"
            params.append(str(code).upper())
        sql += " order by code limit %s"
        params.append(batas)
        cur.execute(sql, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]

    return [
        {k: _teks(v) if k in _TANGGAL_FIELDS else v for k, v in row.items()}
        for row in rows
    ]


def state() -> dict[str, Any]:
    """Keadaan gerbang klaim sekarang, dengan sebab pertama yang menolak.

    Tidak pernah melempar: yang tidak terbaca masuk ``note`` dan gerbangnya
    dibiarkan seperti apa adanya, bukan ditebak.
    """
    catatan: list[str] = []
    halt_state = journal_halt.state()
    if halt_state.get("note"):
        catatan.append(str(halt_state["note"]))

    kesegaran, catatan_kesegaran = _freshness()
    if catatan_kesegaran:
        catatan.append(catatan_kesegaran)

    verdict: str | None = None
    celah: list[dict[str, Any]] = []
    try:
        with get_cursor() as cur:
            ringkas = journal_store.journal_summary(cur, rule=RULE_PAPAN)
            verdict = str((ringkas.get("verdict") or {}).get("status") or "") or None
            celah = journal_store.gaps(cur, sessions=10)
    except Exception as e:
        catatan.append(f"buku klaim belum terbaca: {e}")

    blocked, sebab_rezim = _regime()
    gerbang = claim_gate(
        manual_halt=bool(halt_state.get("halted")),
        halt_reason=halt_state.get("reason"),
        freshness_status=kesegaran,
        gate_blocked=blocked,
        gate_reason=sebab_rezim,
        verdict=verdict,
    )
    return {
        **gerbang,
        "halt": halt_state,
        "gaps": celah,
        "note": " ".join(catatan) or None,
    }


def halt(*, reason: str, by: str = "pemilik") -> dict[str, Any]:
    """Hentikan penulisan klaim baru. Sebabnya wajib; kosong = ValueError (422)."""
    alasan = str(reason or "").strip()
    if not alasan:
        raise ValueError(
            "reason wajib diisi: halt tanpa sebab tidak bisa dibedakan dari "
            "mesin yang rusak"
        )
    journal_halt.set_halt(reason=alasan, by=by or "pemilik")
    return state()


def resume(*, by: str = "pemilik") -> dict[str, Any]:
    """Buka lagi penulisan klaim; waktu dan pelakunya tetap tercatat."""
    journal_halt.clear(by=by or "pemilik")
    return state()
