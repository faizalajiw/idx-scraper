"""Ingest & store komposisi pemegang saham IDX -> research.ownership.

Sumber gratis per-emiten: ``/primary/ListedCompany/GetCompanyProfilesDetail``
(kunci ``PemegangSaham``). Setiap kali dijalankan, satu **snapshot** disimpan
untuk ``snapshot_date`` (default: hari ini WIB). Karena IDX hanya memberi
komposisi terkini, aksi pemilik (siapa menambah/mengurangi) dihitung dengan
membandingkan snapshot terbaru vs sebelumnya — jadi jalankan berkala
(mingguan/bulanan) agar ada pembanding.

Modul ini:
- mem-parse payload secara defensif (angka bisa string berformat),
- memastikan tabel ada (DDL idempoten, mirror sql/research_schema.sql),
- upsert on (snapshot_date, code, holder_name, category) — aman diulang.

Pemanggil: ``cli.cmd_ownership`` (manual/scheduler).
"""

from __future__ import annotations

from typing import Any

from .ownership import parse_shareholders

DDL = """
create table if not exists research.ownership (
    snapshot_date date not null,
    code          text not null,
    holder_name   text not null,
    category      text,
    shares        numeric(30,4),
    pct           numeric(12,6),
    is_controller boolean not null default false,
    source        text not null default 'IDX',
    captured_at   timestamptz not null default now(),
    primary key (snapshot_date, code, holder_name, category)
);
create index if not exists idx_ownership_code
    on research.ownership (code, snapshot_date desc);
"""


def ensure_table(cur: Any) -> None:
    cur.execute(DDL)


def upsert_ownership(
    cur: Any, code: str, snapshot_date: str | None, rows: list[dict[str, Any]]
) -> int:
    """Upsert baris pemegang saham untuk (code, snapshot_date).

    ``snapshot_date`` ISO (YYYY-MM-DD); None -> hari ini (zona DB). Mengembalikan
    jumlah baris. Tabel dipastikan ada lebih dulu.
    """
    ensure_table(cur)
    code = code.upper()
    count = 0
    for r in rows:
        cur.execute(
            """insert into research.ownership
                   (snapshot_date, code, holder_name, category, shares, pct,
                    is_controller, source)
               values (coalesce(%s::date, current_date), %s, %s, %s, %s, %s, %s, 'IDX')
               on conflict (snapshot_date, code, holder_name, category) do update set
                 shares = excluded.shares,
                 pct = excluded.pct,
                 is_controller = excluded.is_controller,
                 captured_at = now()""",
            (
                snapshot_date,
                code,
                r["holder_name"],
                r.get("category"),
                r.get("shares"),
                r.get("pct"),
                bool(r.get("is_controller")),
            ),
        )
        count += 1
    return count


def snapshot_from_profile(
    cur: Any, code: str, profile: dict[str, Any] | None, snapshot_date: str | None = None
) -> int:
    """Parse profile -> upsert. Kembalikan jumlah baris (0 bila tak ada data)."""
    rows = parse_shareholders(profile)
    if not rows:
        return 0
    return upsert_ownership(cur, code, snapshot_date, rows)
