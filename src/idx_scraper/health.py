"""Health-check data EOD — peringatan kalau data hari bursa belum masuk.

Kenapa ada: job EOD (`refresh_s2` 16:10) bisa gagal
diam-diam — Cloudflare, koneksi DB putus, atau scheduler mati. Dashboard lalu
cuma menampilkan data hari sebelumnya tanpa ada yang sadar. Check ini
membandingkan tanggal terbaru di layer PIT (`research.latest_pit`) dan close
resmi IHSG (`index_quotes`) dengan hari bursa yang diharapkan, lalu memberi
peringatan lewat log scheduler + Telegram.

Cek kedua, `check_field_coverage`: tanggal bisa ada sementara isinya bolong.
192.613 kolom `foreign_*` di `research.raw_eod` kosong 2025-09-24 → 2026-07-29
tanpa satu pun alarm, karena tanggal terbarunya sudah lama. Guard ini memeriksa
cakupan kolom `foreign_net` per sesi, jadi celah ketahuan dalam ≤1 sesi.

Dipakai oleh:
- ``idx health`` — cek manual; exit code 1 kalau data telat (bisa dipakai cron)
- job ``health_eod`` di ``idx serve`` — Sen–Jum 16:45 WIB, kirim Telegram kalau telat

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
# Default 16: harus konsisten dengan jadwal `refresh_s2` (16:10) dan `health_eod`
# (16:45) — kalau dinaikkan ke 17, job health 16:45 tidak akan pernah berbunyi.
EOD_DEADLINE_HOUR = int(os.getenv("IDX_EOD_DEADLINE_HOUR", "16"))


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


@dataclass
class FieldCoverage:
    """Cakupan kolom ``foreign_*`` di ``research.raw_eod`` per sesi.

    ``EodHealth`` hanya melihat tanggal terbaru, jadi satu sesi yang tanggalnya
    ada tapi kolom foreign-nya bolong lolos tanpa alarm (kejadian 2025-09-24 →
    2026-07-29: 192.613 kolom kosong selama 9 bulan). Dataclass ini memeriksa
    isi per sesi supaya celah ketahuan dalam ≤1 sesi. ``ok`` = tidak ada sesi
    timpang di jendela yang dicek.
    """

    checked: int
    troublemakers: list[str] = field(default_factory=list)
    worst_pct: float = 100.0

    @property
    def ok(self) -> bool:
        return not self.troublemakers


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
        cur.execute("select max(trade_date) from research.prices_pit")
        stocks_latest = cur.fetchone()[0]
        # Close resmi COMPOSITE = baris index_quotes setelah 16:00 WIB.
        cur.execute(
            "select max((captured_at at time zone 'Asia/Jakarta')::date) "
            "from index_quotes where code = 'COMPOSITE' "
            "and extract(hour from captured_at at time zone 'Asia/Jakarta') >= 16"
        )
        index_latest = cur.fetchone()[0]

    return evaluate_health(expected, stocks_latest, index_latest, now)


def evaluate_field_coverage(
    rows: list[tuple[date, int, int]], sessions: int = 30
) -> FieldCoverage:
    """Cari sesi yang kolom ``foreign_net``-nya bolong (murni, tanpa DB).

    ``rows`` = ``(trade_date, total_baris, foreign_terisi)``, urut naik per
    tanggal. Hanya ``sessions`` baris terakhir yang dinilai — sesi lama yang
    sudah ditambal tidak ikut menghidupkan alarm setiap kali.
    """
    window = rows[-sessions:]
    persentase: list[float] = []
    troublemakers: list[str] = []
    for trade_date, total, terisi in window:
        pct = round(100 * terisi / total, 1)
        persentase.append(pct)
        if terisi < total:
            troublemakers.append(f"{trade_date} ({terisi}/{total}, {pct:.1f}%)")

    return FieldCoverage(
        checked=len(window),
        troublemakers=troublemakers,
        worst_pct=min(persentase) if persentase else 100.0,
    )


def check_field_coverage(dsn: str | None = None, sessions: int = 30) -> FieldCoverage:
    """Cek cakupan kolom ``foreign_*`` per sesi terakhir di ``research.raw_eod``.

    Read-only. Kalau ``DATABASE_URL`` tidak ada, hasilnya kosong dan dianggap
    ``ok`` (tanpa DB tidak ada yang bisa dinilai) — pemanggil tetap dapat
    laporannya lewat ``checked == 0``.
    """
    import psycopg

    dsn = dsn or os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        return FieldCoverage(checked=0, troublemakers=[], worst_pct=100.0)

    with psycopg.connect(dsn, autocommit=True, connect_timeout=8) as conn, conn.cursor() as cur:
        cur.execute(
            "select trade_date, count(*) total, count(foreign_net) terisi "
            "from research.raw_eod group by 1 order by 1"
        )
        rows = cur.fetchall()

    return evaluate_field_coverage(rows, sessions)


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


def format_field_coverage_message(c: FieldCoverage) -> str:
    """Pesan (HTML/terminal) untuk sesi yang kolom ``foreign_*``-nya bolong.

    Dipakai dua tempat: stdout ``idx health`` dan Telegram ``health_eod``.
    Teksnya sengaja tetap terbaca sebagai output terminal — tag HTML-nya cuma
    ``<b>``/``<code>`` seperti ``format_health_message``.
    """
    tampil = c.troublemakers[:5]
    baris = "\n".join(f"• {t}" for t in tampil)
    sisa = len(c.troublemakers) - len(tampil)
    if sisa > 0:
        baris += f"\n… (+{sisa} lagi)"

    tertua = c.troublemakers[0][:10]
    terbaru = c.troublemakers[-1][:10]
    return (
        "<b>⚠️ Cakupan kolom EOD timpang</b>\n"
        f"{len(c.troublemakers)} dari {c.checked} sesi terakhir punya kolom "
        f"foreign_* bolong (terburuk {c.worst_pct:.1f}%):\n"
        f"{baris}\n"
        "Perbaiki: "
        f"<code>python -m scripts.repair_foreign_flow --start-date {tertua} "
        f"--end-date {terbaru}</code>"
    )
