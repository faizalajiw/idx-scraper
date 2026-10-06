"""Health-check data EOD — peringatan kalau data hari bursa belum masuk.

Kenapa ada: job EOD (`refresh_s2` 16:05 / `refresh_s2_late` 16:20) bisa gagal
diam-diam — Cloudflare, koneksi DB putus, atau scheduler mati. Dashboard lalu
cuma menampilkan data hari sebelumnya tanpa ada yang sadar. Check ini
membandingkan tanggal terbaru di layer PIT (`research.latest_pit`) dan close
resmi IHSG (`index_quotes`) dengan hari bursa yang diharapkan, lalu memberi
peringatan lewat log scheduler + Telegram.

Dipakai oleh:
- ``idx health`` — cek manual; exit code 1 kalau data telat (bisa dipakai cron)
- job ``health_eod`` di ``idx serve`` — Sen–Jum 17:00 WIB, kirim Telegram kalau telat

Catatan: tanpa kalender libur bursa, hari libur nasional ikut dianggap hari
bursa. Set ``IDX_HOLIDAYS=2026-12-25,2027-01-01`` (ISO, dipisah koma) untuk
membisukan alarm di hari libur.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

# Lewat jam ini (WIB) di hari kerja, EOD hari ini dianggap telat kalau belum ada.
EOD_DEADLINE_HOUR = int(os.getenv("IDX_EOD_DEADLINE_HOUR", "17"))


def parse_holidays(raw: str | None = None) -> set[date]:
    """Parse daftar libur bursa ISO (YYYY-MM-DD, dipisah koma) — entri rusak diabaikan."""
    if raw is None:
        raw = os.getenv("IDX_HOLIDAYS", "")
    out: set[date] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(date.fromisoformat(part))
        except ValueError:
            continue
    return out


def expected_trading_date(now: datetime, holidays: set[date] | None = None) -> date:
    """Hari bursa terakhir yang seharusnya sudah punya data EOD.

    Mundur melewati akhir pekan & daftar libur. ``now`` wajib eksplisit supaya
    fungsi ini murni dan gampang diuji.
    """
    holidays = holidays if holidays is not None else parse_holidays()
    d = now.date()
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


@dataclass
class EodHealth:
    """Hasil satu kali cek. ``ok`` = True kalau tidak ada yang perlu ditindak."""

    expected: date
    stocks_latest: date | None
    index_latest: date | None
    checked_at: datetime
    missing: list[str] = field(default_factory=list)
    overdue: bool = False

    @property
    def ok(self) -> bool:
        return not self.overdue


def evaluate_health(
    expected: date,
    stocks_latest: date | None,
    index_latest: date | None,
    now: datetime,
    deadline_hour: int = EOD_DEADLINE_HOUR,
) -> EodHealth:
    """Bandingkan data terbaru vs hari bursa yang diharapkan (murni, tanpa DB).

    ``overdue`` hanya True di hari kerja setelah ``deadline_hour`` — sebelum itu
    data memang belum wajib ada, jadi jangan alarm.
    """
    missing: list[str] = []
    if stocks_latest is None or stocks_latest < expected:
        missing.append(f"saham EOD (terbaru {stocks_latest or '-'})")
    if index_latest is None or index_latest < expected:
        missing.append(f"close IHSG (terbaru {index_latest or '-'})")

    overdue = bool(missing) and now.weekday() < 5 and now.hour >= deadline_hour
    return EodHealth(
        expected=expected,
        stocks_latest=stocks_latest,
        index_latest=index_latest,
        checked_at=now,
        missing=missing,
        overdue=overdue,
    )


def check_eod_health(dsn: str | None = None, now: datetime | None = None) -> EodHealth:
    """Cek data EOD terbaru vs hari bursa yang diharapkan.

    Read-only. Kalau ``DATABASE_URL`` tidak ada, hasilnya ditandai ``missing``
    supaya pemanggil tetap tahu ada masalah konfigurasi.
    """
    import psycopg

    dsn = dsn or os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    now = now or datetime.now(WIB)
    expected = expected_trading_date(now)
    if not dsn:
        return EodHealth(
            expected=expected,
            stocks_latest=None,
            index_latest=None,
            checked_at=now,
            missing=["DATABASE_URL tidak di-set"],
            overdue=now.weekday() < 5 and now.hour >= EOD_DEADLINE_HOUR,
        )

    with psycopg.connect(dsn, autocommit=True, connect_timeout=8) as conn, conn.cursor() as cur:
        cur.execute("set time zone 'Asia/Jakarta'")
        cur.execute("select max(trade_date) from research.latest_pit")
        stocks_latest = cur.fetchone()[0]
        # Close resmi COMPOSITE = baris index_quotes setelah 16:00 WIB.
        cur.execute(
            "select max((captured_at at time zone 'Asia/Jakarta')::date) "
            "from index_quotes where code = 'COMPOSITE' "
            "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16"
        )
        index_latest = cur.fetchone()[0]

    return evaluate_health(expected, stocks_latest, index_latest, now)


def format_health_message(h: EodHealth) -> str:
    """Pesan Telegram (HTML) untuk kondisi data telat."""
    stamp = h.checked_at.strftime("%Y-%m-%d %H:%M WIB")
    detail = ", ".join(h.missing)
    return (
        "<b>⚠️ Data EOD belum masuk</b>\n"
        f"Hari bursa <b>{h.expected}</b>, tapi ini belum ada: {detail}.\n"
        "Cek: proses <code>idx serve</code> masih jalan? Kalau perlu, "
        "jalankan <code>idx eod</code> manual.\n"
        f"<i>Dicek {stamp}</i>"
    )
