"""Buku klaim di DB (lapis 4): tulis sekali, tolak dengan sebab, isi hasil, ringkas.

Lapisan tipis di atas ``research.journal`` (aturan murni) dan ``research.claim_eval``
(pengukuran hasil). DDL-nya di-mirror dari ``sql/journal.sql`` supaya tabelnya ikut
lahir saat perintah pertama dijalankan.

Dua tabel: ``research.claim_journal`` (klaim; kunci code + trade_date + rule) dan
``research.claim_refusal`` (jejak percobaan yang ditolak; kunci trade_date + reason).
Baris penolakan adalah catatan percobaan, bukan kesimpulan: satu sesi boleh punya
penolakan jam 17:00 lalu klaim jam 18:00, dan keduanya benar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from .claim_eval import VERDICT_HORIZON, claim_outcomes, claim_verdict, format_record_line
from .journal import (
    CLAIM_COLUMNS,
    REJECT_EMPTY_POOL,
    RULE_PAPAN,
    SHORTLIST_SIZE,
    STATUS_EXPIRED,
    STATUS_OPEN,
    STATUS_STOP,
    STATUS_TARGET,
    claim_from_candidate,
    summarize_claims,
)
from .position import position_plan

#: DDL yang sama dengan ``sql/journal.sql`` (berkas itu untuk deploy, ini untuk jalan).
_DDL = """
create table if not exists research.claim_journal (
    code             text          not null,
    trade_date       date          not null,
    rule             text          not null,
    direction        text          not null default 'BUY',
    grade            text,
    ref_raw          numeric(18,4) not null,
    stop_raw         numeric(18,4) not null,
    target_raw       numeric(18,4) not null,
    ref_adj          numeric(18,4),
    horizon_days     integer       not null,
    size_pct         numeric(9,4),
    size_lots        integer,
    size_rupiah      numeric(18,2),
    regime           text,
    vol_state        text,
    freshness_status text,
    status           text          not null default 'terbuka'
                     check (status in ('terbuka','kena_target','kena_stop','kedaluwarsa')),
    outcome_abn      numeric(12,8),
    touch_date       date,
    recorded_at      timestamptz   not null default now(),
    primary key (code, trade_date, rule)
);
create index if not exists idx_claim_journal_date
    on research.claim_journal (trade_date desc, status);

create table if not exists research.claim_refusal (
    trade_date date        not null,
    reason     text        not null,
    detail     text,
    created_at timestamptz not null default now(),
    primary key (trade_date, reason)
);
"""

#: Hasil ``claim_eval`` -> status baris buku. ``unresolved`` sengaja tidak ada di
#: sini: hari yang belum teramati bukan hari yang sudah lewat.
_OUTCOME_STATUS = {"target": STATUS_TARGET, "stop": STATUS_STOP, "timeout": STATUS_EXPIRED}

#: Batas baca ringkasan. Buku ini kecil (3 sampai 5 baris per sesi), tetapi
#: ringkasannya tidak boleh bergantung pada ukuran itu selamanya.
SUMMARY_LIMIT = 5000


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _tanggal(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)[:10]


def ensure_table(cur) -> None:
    """Pasang kedua tabel kalau belum ada (idempoten)."""
    cur.execute(_DDL)


# --------------------------------------------------------------------------- #
# Baca kandidat
# --------------------------------------------------------------------------- #


def pool_counts(dsn: str, trade_date: str) -> tuple[int, int]:
    """(jumlah baris kandidat, jumlah grade A) untuk sesi itu, apa adanya.

    Dipakai hanya supaya penolakan "kolam kosong" bisa dibaca: angka ini yang
    membedakan "job rekomendasi belum jalan" dari "sesi ini memang tanpa nama".
    """
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=15, row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) as semua,"
            " count(*) filter (where grade = 'A') as grade_a"
            " from research.recommendation_log where trade_date = %s",
            (trade_date,),
        )
        row = cur.fetchone() or {}
        return int(row.get("semua") or 0), int(row.get("grade_a") or 0)


def candidates_for(
    dsn: str, trade_date: str, *, limit: int = SHORTLIST_SIZE
) -> list[dict[str, Any]]:
    """Daftar pendek sesi itu: baris grade A, urut skor, dipotong ``limit``.

    Ukuran posisi dihitung tanpa modal (persen saja): penulis klaim terjadwal
    tidak memegang angka modal yang diketik pemilik di browser, jadi lot dan
    rupiah sengaja dibiarkan kosong alih-alih diarang.
    """
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=15, row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            """select r.code, r.trade_date, r.grade, r.score, r.entry_ref,
                      r.stop, r.target, r.horizon_days, a.adj_close as ref_adj
               from research.recommendation_log r
               left join lateral (
                   select p.adj_close
                   from research.prices_adjusted p
                   where p.code = r.code and p.trade_date = r.trade_date
                   order by p.knowledge_date desc
                   limit 1
               ) a on true
               where r.trade_date = %s and r.grade = 'A'
               order by r.score desc nulls last, r.code
               limit %s""",
            (trade_date, int(limit)),
        )
        rows = [dict(r) for r in cur.fetchall()]

    keluar: list[dict[str, Any]] = []
    for r in rows:
        rencana = position_plan(entry=_num(r.get("entry_ref")), stop=_num(r.get("stop")))
        keluar.append(
            {
                **r,
                "trade_date": _tanggal(r.get("trade_date")),
                "entry_ref": _num(r.get("entry_ref")),
                "stop": _num(r.get("stop")),
                "target": _num(r.get("target")),
                "horizon_days": r.get("horizon_days"),
                "ref_adj": _num(r.get("ref_adj")),
                "size_pct": _num(rencana.get("pct")),
            }
        )
    return keluar


# --------------------------------------------------------------------------- #
# Tulis
# --------------------------------------------------------------------------- #


def record_claims(cur, claims: list[dict[str, Any]]) -> int:
    """Tulis klaim; kunci yang sudah ada **tidak** ditimpa.

    Satu per satu, bukan ``executemany``: jumlah baris yang benar-benar masuk
    adalah bukti bahwa janji "tulis sekali" ditepati, dan jumlah itu hilang di
    jalur batch.
    """
    kolom = ", ".join(CLAIM_COLUMNS)
    tanda = ", ".join(["%s"] * len(CLAIM_COLUMNS))
    sql = (
        f"insert into research.claim_journal ({kolom}) values ({tanda}) "
        "on conflict (code, trade_date, rule) do nothing"
    )
    masuk = 0
    for klaim in claims:
        cur.execute(sql, tuple(klaim.get(k) for k in CLAIM_COLUMNS))
        masuk += int(getattr(cur, "rowcount", 0) or 0)
    return masuk


def record_refusal(
    cur, *, trade_date: str, reason: str, detail: str | None = None
) -> None:
    """Catat satu sebab penolakan sesi itu; sebab yang sama tidak menumpuk."""
    cur.execute(
        "insert into research.claim_refusal (trade_date, reason, detail)"
        " values (%s, %s, %s) on conflict (trade_date, reason) do nothing",
        (trade_date, reason, detail),
    )


def write_session(
    dsn: str,
    cur,
    *,
    trade_date: str,
    gate: dict[str, Any] | None = None,
    konteks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tulis buku satu sesi: klaim kalau gerbang mengizinkan, selain itu sebabnya.

    ``konteks`` berisi ``regime`` / ``vol_state`` / ``freshness_status`` yang sudah
    dibaca pemanggil untuk gerbang; diteruskan apa adanya supaya baris buku bisa
    dibaca bersama keadaannya tanpa modul ini menebak.
    """
    ensure_table(cur)
    konteks = konteks or {}

    if gate is not None and not gate.get("allowed"):
        sebab = str(gate.get("reason") or "gerbang")
        record_refusal(
            cur, trade_date=trade_date, reason=sebab, detail=str(gate.get("message") or "")[:500] or None
        )
        return {"written": 0, "refused": 0, "reason": sebab}

    kandidat = candidates_for(dsn, trade_date)
    if not kandidat:
        semua, grade_a = pool_counts(dsn, trade_date)
        record_refusal(
            cur,
            trade_date=trade_date,
            reason=REJECT_EMPTY_POOL,
            detail=f"{semua} baris kandidat, {grade_a} grade A",
        )
        return {"written": 0, "refused": 0, "reason": REJECT_EMPTY_POOL}

    klaim: list[dict[str, Any]] = []
    ditolak: dict[str, list[str]] = {}
    for k in kandidat:
        baris = claim_from_candidate(
            k,
            trade_date=trade_date,
            rule=RULE_PAPAN,
            ref_adj=k.get("ref_adj"),
            regime=konteks.get("regime"),
            vol_state=konteks.get("vol_state"),
            freshness_status=konteks.get("freshness_status"),
        )
        if baris is None:
            sebab = "tidak lengkap"
            if not k.get("stop"):
                sebab = "tanpa_stop"
            elif not k.get("target"):
                sebab = "tanpa_target"
            elif not k.get("size_pct"):
                sebab = "tanpa_ukuran"
            ditolak.setdefault(sebab, []).append(str(k.get("code")))
            continue
        klaim.append(baris)

    for sebab, kode in ditolak.items():
        record_refusal(
            cur,
            trade_date=trade_date,
            reason=sebab,
            detail=", ".join(kode)[:500],
        )

    masuk = record_claims(cur, klaim) if klaim else 0
    return {"written": masuk, "refused": len(ditolak), "reason": None}


# --------------------------------------------------------------------------- #
# Isi hasil
# --------------------------------------------------------------------------- #


def panel(
    cur, *, codes: list[str], start: str, end: str, pasar: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Panel mentah + terkoreksi untuk pengukuran hasil, kolomnya sesuai claim_eval.

    ``adj`` sengaja memuat **seluruh pasar** di jendela itu (bukan hanya nama yang
    diklaim): pembanding abnormal milik harness P3 adalah rata-rata panel itu
    sendiri, jadi memuat lima nama saja akan mengukur klaim terhadap dirinya.
    Harga terkoreksi diambil pada ``knowledge_date`` terbaru per (code, trade_date).
    """
    raw = pd.DataFrame(columns=["code", "date", "high", "low", "close"])
    adj = pd.DataFrame(columns=["code", "date", "close"])
    if not codes:
        return raw, adj

    cur.execute(
        "select code, trade_date as date, high, low, close from research.raw_eod"
        " where code = any(%s) and trade_date between %s and %s order by code, trade_date",
        (codes, start, end),
    )
    raw_rows = [dict(r) for r in cur.fetchall()]
    if raw_rows:
        raw = pd.DataFrame(raw_rows, columns=["code", "date", "high", "low", "close"])
        raw["date"] = pd.to_datetime(raw["date"])
        for kolom in ("high", "low", "close"):
            raw[kolom] = pd.to_numeric(raw[kolom], errors="coerce")

    cur.execute(
        """select distinct on (code, trade_date) code, trade_date as date, adj_close as close
           from research.prices_adjusted
           where (%s or code = any(%s)) and trade_date between %s and %s
           order by code, trade_date, knowledge_date desc""",
        (pasar, codes, start, end),
    )
    adj_rows = [dict(r) for r in cur.fetchall()]
    if adj_rows:
        adj = pd.DataFrame(adj_rows, columns=["code", "date", "close"])
        adj["date"] = pd.to_datetime(adj["date"])
        adj["close"] = pd.to_numeric(adj["close"], errors="coerce")
    return raw, adj


def fill_outcomes_from_frames(
    claims: list[dict[str, Any]],
    *,
    raw: pd.DataFrame,
    adj: pd.DataFrame,
    horizon: int = VERDICT_HORIZON,
) -> list[dict[str, Any]]:
    """Hitung hasil klaim yang masih terbuka dari panel (murni, tanpa DB).

    Klaim yang sudah selesai tidak dihitung ulang: keputusan sekali, seperti
    tulisannya. Angka ``outcome_abn`` hanya ditulis kalau klaimnya memang sudah
    selesai; klaim yang belum matang dibiarkan kosong, bukan diisi angka
    setengah jalan yang nanti terbaca seperti hasil.
    """
    terbuka = [c for c in claims if str(c.get("status") or STATUS_OPEN) == STATUS_OPEN]
    if not terbuka or raw.empty or adj.empty:
        return []

    horizons = tuple(sorted({int(c["horizon_days"]) for c in terbuka} | {int(horizon)}))
    frame = pd.DataFrame(
        [
            {
                "code": c["code"],
                "date": c["trade_date"],
                "direction": "BUY",
                "stop": c["stop_raw"],
                "target": c["target_raw"],
                "horizon_days": int(c["horizon_days"]),
            }
            for c in terbuka
        ]
    )
    hasil = claim_outcomes(frame, raw=raw, adj=adj, horizons=horizons)

    kunci = {(str(c["code"]).upper(), str(c["trade_date"])[:10]): c for c in terbuka}
    keluar: list[dict[str, Any]] = []
    for _, r in hasil.iterrows():
        kode = str(r["code"]).upper()
        tanggal = str(r["date"])[:10]
        asal = kunci.get((kode, tanggal))
        if asal is None:
            continue
        status = _OUTCOME_STATUS.get(str(r.get("outcome")), STATUS_OPEN)
        abn = None
        if status != STATUS_OPEN:
            abn = _num(r.get(f"abn_{int(r['horizon_days'])}"))
        keluar.append(
            {
                "code": asal["code"],
                "trade_date": asal["trade_date"],
                "rule": asal.get("rule") or RULE_PAPAN,
                "status": status,
                "outcome_abn": abn,
                "touch_date": _tanggal(r.get("touch_date")),
            }
        )
    return keluar


def open_claims(cur, *, trade_date: str | None = None) -> list[dict[str, Any]]:
    """Klaim yang masih terbuka di buku (yang boleh diperiksa ulang)."""
    sql = (
        "select code, trade_date, rule, stop_raw, target_raw, horizon_days, status"
        " from research.claim_journal where status = 'terbuka'"
    )
    params: tuple[Any, ...] = ()
    if trade_date is not None:
        sql += " and trade_date = %s"
        params = (trade_date,)
    cur.execute(sql, params or None)
    return [dict(r) for r in cur.fetchall()]


def fill_outcomes(cur, *, trade_date: str | None = None) -> dict[str, int]:
    """Isi hasil klaim yang sudah matang. Tidak ada klaim yang diperiksa dua kali."""
    ensure_table(cur)
    klaim = open_claims(cur, trade_date=trade_date)
    hitung = {
        "diperiksa": len(klaim),
        "kena_target": 0,
        "kena_stop": 0,
        "kedaluwarsa": 0,
        "terbuka": 0,
    }
    if not klaim:
        return hitung

    kode = sorted({str(c["code"]) for c in klaim})
    awal = min(str(c["trade_date"])[:10] for c in klaim)
    akhir = datetime.now().astimezone().date().isoformat()
    raw, adj = panel(cur, codes=kode, start=awal, end=akhir)

    for baris in fill_outcomes_from_frames(klaim, raw=raw, adj=adj):
        cur.execute(
            "update research.claim_journal set status = %s, outcome_abn = %s, touch_date = %s"
            " where code = %s and trade_date = %s and rule = %s",
            (
                baris["status"],
                baris["outcome_abn"],
                baris["touch_date"],
                baris["code"],
                baris["trade_date"],
                baris["rule"],
            ),
        )
        hitung[baris["status"]] = hitung.get(baris["status"], 0) + 1
    return hitung


# --------------------------------------------------------------------------- #
# Baca ringkasan
# --------------------------------------------------------------------------- #


def journal_summary(
    cur, *, rule: str | None = None, code: str | None = None
) -> dict[str, Any]:
    """Ringkasan buku + kalimat rekaman P3 + verdict P3.

    Penyaring dilakukan di Python setelah baca terbatas: bukunya kecil, dan satu
    jalur baca berarti satu tempat yang bisa salah.
    """
    cur.execute(
        "select code, trade_date, rule, status, outcome_abn, horizon_days"
        " from research.claim_journal order by trade_date, code limit %s",
        (SUMMARY_LIMIT,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    if rule is not None:
        rows = [r for r in rows if str(r.get("rule")) == rule]
    if code is not None:
        rows = [r for r in rows if str(r.get("code")).upper() == str(code).upper()]

    ringkas = summarize_claims(rows)
    ringkas["verdict"] = claim_verdict(ringkas)
    ringkas["record_line"] = format_record_line(ringkas)
    return ringkas


def gaps(cur, *, sessions: int = 10) -> list[dict[str, Any]]:
    """Sesi terakhir yang tidak punya klaim **dan** tidak punya penolakan.

    Ini daftar bisu: sesi yang kosong tanpa sebab berarti ada yang gagal tanpa
    bersuara, dan itu yang harus terlihat di layar.
    """
    cur.execute(
        "select trade_date from research.raw_eod group by trade_date"
        " order by trade_date desc limit %s",
        (int(sessions),),
    )
    tanggal = [_tanggal(r["trade_date"]) for r in cur.fetchall()]
    tanggal = [t for t in tanggal if t]
    if not tanggal:
        return []

    cur.execute(
        "select distinct trade_date from research.claim_journal where trade_date = any(%s)",
        (tanggal,),
    )
    ada_klaim = {_tanggal(r["trade_date"]) for r in cur.fetchall()}
    cur.execute(
        "select distinct trade_date from research.claim_refusal where trade_date = any(%s)",
        (tanggal,),
    )
    ada_sebab = {_tanggal(r["trade_date"]) for r in cur.fetchall()}
    return [
        {"trade_date": t, "sebab": "tidak ada klaim dan tidak ada penolakan"}
        for t in tanggal
        if t not in ada_klaim and t not in ada_sebab
    ]
