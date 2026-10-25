"""Buku klaim (lapis 4, P6): data sesi, tulis klaim, job harian."""

from __future__ import annotations

from datetime import datetime

import dotenv

from .fetch import WIB

dotenv.load_dotenv()


from .. import db
from ..logging_setup import say
from ..notify import (
    TelegramNotifier,
    format_claim_hits,
    format_claim_silence,
)


def _journal_dsn() -> str | None:
    """DSN DB untuk job buku klaim, atau None kalau belum diatur."""
    return db.dsn()


def _now_wib() -> datetime:
    """Waktu WIB sekarang, dipisah supaya tes bisa memakukannya."""
    return datetime.now(WIB)


def _last_session_date(dsn: str) -> str | None:
    """Tanggal bursa terakhir yang datanya sudah masuk (``research.raw_eod``)."""
    with (
        db.connect(dict_rows=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("select max(trade_date) as tanggal from research.raw_eod")
        row = cur.fetchone() or {}
    tanggal = row.get("tanggal")
    return str(tanggal) if tanggal else None


def _journal_session(dsn: str | None = None, trade_date: str | None = None) -> str:
    """Sesi yang ditulis job: ``--trade-date`` kalau ada, kalau tidak sesi bursa terakhir.

    Bukan tanggal kalender: job jam 00:10 WIB masih milik sesi kemarin, dan tanggal
    kalender akan membuatnya menulis sesi yang datanya belum ada.
    """
    if trade_date:
        return str(trade_date)
    terakhir = _last_session_date(dsn) if dsn else None
    return terakhir or _now_wib().date().isoformat()


def _journal_gate_parts(dsn: str) -> tuple[dict, dict]:
    """(gerbang, konteks) untuk sesi yang akan ditulis. Tidak pernah melempar.

    Sebabnya dibaca dari sumber yang sudah ada: halt manual dari berkas halt,
    kesegaran dari ``freshness.current``, rezim dari gerbang papan, verdict dari
    buku klaim aturan ini sendiri. Yang tidak terbaca ditulis sebagai peringatan
    dan gerbangnya dibiarkan seperti apa adanya - bukan ditebak.
    """
    from .. import journal_halt
    from ..freshness import current as freshness_current
    from ..research import journal_store
    from ..research.journal import RULE_PAPAN, claim_gate

    halt = journal_halt.state()
    kesegaran: str | None = None
    verdict: str | None = None
    regime: str | None = None
    vol_state: str | None = None
    blocked = False
    alasan_rezim: str | None = None

    try:
        with (
            db.connect(dict_rows=True) as conn,
            conn.cursor() as cur,
        ):
            kesegaran = str(freshness_current(cur).get("status") or "") or None
            try:
                ringkas = journal_store.journal_summary(cur, rule=RULE_PAPAN)
                verdict = str(ringkas["verdict"].get("status") or "") or None
            except Exception as e:
                say(f"[warn] verdict buku klaim belum terbaca: {e}", err=True)
    except Exception as e:
        say(f"[warn] kesegaran data tidak terbaca: {e}", err=True)

    try:
        from ..api import analytics as analytics_api

        regime_row = analytics_api.get_market_regime() or {}
        regime = regime_row.get("regime")
        vol_state = regime_row.get("vol_state")
        gate = analytics_api._regime_gate() or {}
        blocked = bool(gate.get("blocked"))
        alasan_rezim = str(gate.get("reason") or gate.get("regime") or "") or None
    except Exception as e:
        say(f"[warn] gerbang rezim tidak terbaca: {e}", err=True)

    return (
        claim_gate(
            manual_halt=bool(halt.get("halted")),
            halt_reason=halt.get("reason"),
            freshness_status=kesegaran,
            gate_blocked=blocked,
            gate_reason=alasan_rezim,
            verdict=verdict,
        ),
        {"regime": regime, "vol_state": vol_state, "freshness_status": kesegaran},
    )


def _journal_write(dsn: str, *, trade_date: str | None = None, cetak: bool = True) -> dict:
    """Tulis satu sesi: gerbang dulu, baru kandidat; yang ditolak tetap bercatatan."""
    from ..research import journal_store

    sesi = _journal_session(dsn, trade_date)
    gerbang, konteks = _journal_gate_parts(dsn)
    with (
        db.connect(dict_rows=True) as conn,
        conn.cursor() as cur,
    ):
        hasil = journal_store.write_session(
            dsn, cur, trade_date=sesi, gate=gerbang, konteks=konteks
        )
    if cetak:
        sebab = f" ({hasil['reason']})" if hasil.get("reason") else ""
        say(
            f"[ok] sesi {sesi}: {hasil['written']} klaim ditulis, "
            f"{hasil['refused']} penolakan{sebab}"
        )
    return {"session": sesi, **hasil}


def _job_journal_claims() -> None:
    """Tulis klaim sesi bursa terakhir; diulang tiap jam 17:55-21:55.

    Kalau klaim sesi itu sudah ada, `write_session` tidak menulis apa-apa (kunci
    code+tanggal+aturan); kalau datanya belum masuk, ia mencatat penolakannya dan
    pengulangan jam berikutnya mencoba lagi. Tidak ada jalur yang menimpa.
    """
    dsn = _journal_dsn()
    if not dsn:
        return
    try:
        _journal_write(dsn)
    except Exception as e:  # DB down must not kill the scheduler
        say(f"[err] buku klaim job: {e}", err=True)


def _job_journal_outcomes() -> None:
    """Isi hasil klaim yang sudah matang. Perpindahan status tidak dibatalkan."""
    dsn = _journal_dsn()
    if not dsn:
        return
    try:
        from ..research import journal_store

        with (
            db.connect(dict_rows=True) as conn,
            conn.cursor() as cur,
        ):
            hitung = journal_store.fill_outcomes(cur)
        say(
            f"[{_now_wib().isoformat()}] buku klaim: diperiksa {hitung['diperiksa']} klaim "
            f"terbuka · target {hitung['kena_target']} · stop {hitung['kena_stop']} · "
            f"kedaluwarsa {hitung['kedaluwarsa']}"
        )
    except Exception as e:
        say(f"[err] isi hasil buku klaim: {e}", err=True)


def _job_journal_reminder() -> int:
    """Pengingat 19:20: klaim yang tersentuh hari ini + penjaga sesi yang bisu."""
    notifier = TelegramNotifier()
    dsn = _journal_dsn()
    if not notifier.enabled or not dsn:
        return 0
    try:
        sesi = _journal_session(dsn)
        hits: list[dict] = []
        with (
            db.connect(dict_rows=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "select code, status, outcome_abn from research.claim_journal"
                " where touch_date = %s and status in ('kena_target','kena_stop')"
                " order by code",
                (sesi,),
            )
            hits = [dict(r) for r in cur.fetchall()]
            if hits:
                teks = format_claim_hits(hits)
            else:
                cur.execute(
                    "select (select count(*) from research.claim_journal"
                    " where trade_date = %s) as klaim,"
                    " (select count(*) from research.claim_refusal"
                    " where trade_date = %s) as penolakan",
                    (sesi, sesi),
                )
                row = cur.fetchone() or {}
                ada_catatan = int(row.get("klaim") or 0) or int(row.get("penolakan") or 0)
                teks = format_claim_hits([]) if ada_catatan else format_claim_silence(sesi)
        return len(hits) if notifier.send_message(teks) else 0
    except Exception as e:
        say(f"[err] pengingat buku klaim: {e}", err=True)
        return 0


def cmd_journal(args) -> None:
    """Buku klaim: tulis, isi hasil, ringkas, keadaan, halt, lanjut."""
    aksi = getattr(args, "journal_cmd", None)
    if aksi == "halt":
        cmd_journal_halt(args)
        return
    if aksi == "resume":
        cmd_journal_resume(args)
        return

    dsn = _journal_dsn()
    if not dsn:
        say("[err] DATABASE_URL belum diisi", err=True)
        return

    from .. import journal_halt
    from ..research import journal_store
    from ..research.journal import RULE_PAPAN

    if aksi == "record":
        _journal_write(dsn, trade_date=getattr(args, "trade_date", None))
        return

    if aksi in ("outcomes", "summary", "state"):
        sesi = _journal_session(dsn, getattr(args, "trade_date", None))
        with (
            db.connect(dict_rows=True) as conn,
            conn.cursor() as cur,
        ):
            if aksi == "outcomes":
                hitung = journal_store.fill_outcomes(cur, trade_date=sesi)
                say(
                    f"[ok] sesi {sesi}: diperiksa {hitung['diperiksa']} klaim terbuka · "
                    f"target {hitung['kena_target']} · stop {hitung['kena_stop']} · "
                    f"kedaluwarsa {hitung['kedaluwarsa']} · masih terbuka {hitung['terbuka']}"
                )
                return
            if aksi == "summary":
                aturan = getattr(args, "rule", None) or RULE_PAPAN
                ringkas = journal_store.journal_summary(
                    cur, rule=aturan, code=getattr(args, "code", None)
                )
                celah = journal_store.gaps(cur, sessions=10)
                say(f"{aturan}: {ringkas['record_line']}")
                say(
                    f"baris {ringkas['claims']} · terukur {ringkas['measured_claims']} · "
                    f"sesi terukur {ringkas['sessions']} dari {ringkas['claim_sessions']} "
                    f"sesi menulis · verdict {ringkas['verdict']['status']}"
                )
                if celah:
                    say(
                        "[warn] sesi tanpa klaim dan tanpa penolakan: "
                        + ", ".join(c["trade_date"] for c in celah),
                        err=True,
                    )
                return

            halt = journal_halt.state()
            ringkas = journal_store.journal_summary(cur, rule=RULE_PAPAN)
            celah = journal_store.gaps(cur, sessions=10)
            cur.execute(
                "select trade_date, reason, detail from research.claim_refusal"
                " order by trade_date desc, reason limit 10"
            )
            penolakan = [dict(r) for r in cur.fetchall()]

        say(
            "halt: "
            + ("ya" if halt["halted"] else "tidak")
            + (f" ({halt['reason']} · {halt['by']} {halt['at']})" if halt["halted"] else "")
        )
        if halt.get("note"):
            say(f"[warn] {halt['note']}", err=True)
        say(f"aturan {RULE_PAPAN}: {ringkas['record_line']}")
        say(f"verdict: {ringkas['verdict']['status']} ({ringkas['verdict']['reason']})")
        for p in penolakan:
            say(f"  {p['trade_date']} {p['reason']}: {p['detail'] or '-'}")
        if celah:
            say(
                "[warn] sesi bisu (tanpa klaim dan tanpa penolakan): "
                + ", ".join(c["trade_date"] for c in celah),
                err=True,
            )
        return

    say(
        "[err] aksi journal tidak dikenal: pakai record, outcomes, summary, state, halt, resume",
        err=True,
    )


def cmd_journal_halt(args) -> None:
    """Hentikan penulisan klaim baru. Sebab wajib: halt tanpa sebab tidak bisa dibaca."""
    from .. import journal_halt

    alasan = str(getattr(args, "reason", "") or "").strip()
    if not alasan:
        say(
            "[err] --reason wajib: sebab halt adalah satu-satunya yang membedakannya "
            "dari mesin yang rusak",
            err=True,
        )
        return
    keadaan = journal_halt.set_halt(reason=alasan, by=getattr(args, "by", None) or "pemilik")
    say(f"[ok] klaim baru dihentikan: {keadaan['reason']} ({keadaan['at']} oleh {keadaan['by']})")


def cmd_journal_resume(args) -> None:
    """Buka lagi penulisan klaim; waktu dan pelakunya tetap tercatat."""
    from .. import journal_halt

    keadaan = journal_halt.clear(by=getattr(args, "by", None) or "pemilik")
    say(f"[ok] klaim baru jalan lagi ({keadaan['at']} oleh {keadaan['by']})")
