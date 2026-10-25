"""Skema jurnal klaim (P6): summary, claims, gerbang halt."""

from __future__ import annotations

from pydantic import BaseModel


class JournalSessionSummary(BaseModel):
    """Satu sesi di buku klaim: berapa klaim, dan rata-rata abnormalnya.

    ``mean_abnormal`` dalam fraksi (0,014 = +1,4%). None berarti sesi itu belum
    punya hasil terukur — bukan nol, dan bukan sesi yang kalah.
    """

    trade_date: str
    claims: int
    mean_abnormal: float | None = None


class JournalVerdict(BaseModel):
    """Verdict aturan buku klaim: terbukti / tidak terbukti / belum cukup.

    Ambangnya memakai ``measured_claims`` (klaim yang sudah punya angka), bukan
    jumlah baris. "Belum cukup" bukan kegagalan; itu jawaban jujur selama bukti
    belum terkumpul, dan ``reason`` menyebut sudah berapa yang ada.
    """

    status: str
    reason: str
    claims: int
    measured_claims: int
    sessions: int
    mean_abnormal: float | None = None


class JournalSummary(BaseModel):
    """Rekaman aturan buku klaim: rata-rata per sesi dulu, baru antar sesi.

    ``mean_abnormal`` = abnormal rata-rata vs pasar dalam fraksi. ``record_line``
    adalah kalimat rekaman P3 — satu baris yang mengikat mesin pada klaimnya
    sendiri; yang belum terukur ditulis "belum terukur", bukan 0,0%.

    ``sessions`` = sesi yang menyumbang angka terukur (ambang verdict);
    ``claim_sessions`` = sesi yang menulis klaim, termasuk yang belum matang.
    ``note`` diisi kalau bukunya belum/tidak terbaca — bentuknya tetap sama.
    """

    claims: int
    measured_claims: int
    sessions: int
    claim_sessions: int = 0
    hit_target: int
    hit_stop: int
    expired: int
    open: int
    horizon_mismatch: int
    mean_abnormal: float | None = None
    per_session: list[JournalSessionSummary] = []
    verdict: JournalVerdict | None = None
    record_line: str
    note: str | None = None


class JournalClaim(BaseModel):
    """Satu baris buku klaim apa adanya, termasuk yang masih terbuka.

    ``status`` = terbuka | kena_target | kena_stop | kedaluwarsa. Level
    (``ref_raw``/``stop_raw``/``target_raw``) dari harga mentah, sedangkan
    ``outcome_abn`` (fraksi) diukur pada harga terkoreksi dan **hanya** terisi
    kalau klaimnya sudah selesai: None berarti belum terukur, bukan nol.
    """

    code: str
    trade_date: str | None = None
    rule: str | None = None
    direction: str | None = None
    grade: str | None = None
    ref_raw: float | None = None
    stop_raw: float | None = None
    target_raw: float | None = None
    ref_adj: float | None = None
    horizon_days: int | None = None
    size_pct: float | None = None
    size_lots: int | None = None
    size_rupiah: float | None = None
    regime: str | None = None
    vol_state: str | None = None
    freshness_status: str | None = None
    status: str
    outcome_abn: float | None = None
    touch_date: str | None = None


class JournalHalt(BaseModel):
    """Keadaan halt manual pemilik (dari berkas, bukan DB)."""

    halted: bool
    reason: str | None = None
    at: str | None = None
    by: str | None = None
    note: str | None = None


class JournalGap(BaseModel):
    """Sesi yang tidak punya klaim DAN tidak punya penolakan: bisu tanpa sebab.

    Daftar ini yang membedakan "tidak ada kandidat" dari "job-nya tidak jalan".
    """

    trade_date: str
    sebab: str


class JournalGateState(BaseModel):
    """Boleh atau tidak menulis klaim baru sekarang, dengan sebab pertamanya.

    ``reason`` = manual | data | regime | verdict; hanya sebab pertama yang
    dilaporkan, urutannya tetap (halt manual pemilik di atas segalanya).
    ``allowed`` True disertai ``note`` bukan berarti sehat: keadaan yang tidak
    terbaca ditulis apa adanya, bukan dijadikan kegagalan.
    """

    allowed: bool
    reason: str | None = None
    message: str
    halt: JournalHalt
    gaps: list[JournalGap] = []
    note: str | None = None


class JournalHaltRequest(BaseModel):
    """Hentikan penulisan klaim baru. ``reason`` wajib: tanpa sebab, halt tidak
    bisa dibedakan dari mesin yang rusak (server menolak dengan 422)."""

    reason: str
    by: str = "pemilik"


class JournalResumeRequest(BaseModel):
    """Buka lagi penulisan klaim; waktu dan pelakunya tetap tercatat."""

    by: str = "pemilik"
