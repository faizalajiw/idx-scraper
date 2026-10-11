"""Berita: scrape Stockbit Snips -> Postgres.

Writer: scheduler (`_job_news_refresh` di cli.py) via `run_once()`.
Reader: API (`latest`) lewat pool read-only.

Sumber tunggal (lihat `news_sources.py`).
"""

from __future__ import annotations

import os
import re
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import psycopg
from bs4 import BeautifulSoup

from .news_sources import BASE_URL, SOURCE

WIB = timezone(timedelta(hours=7))
UA = "Mozilla/5.0 (compatible; market-labs-news/1.0)"

DDL = """
create table if not exists news_items (
  url text primary key,
  title text not null,
  source text not null,
  published_at timestamptz not null,
  fetched_at timestamptz not null default now()
);
create index if not exists idx_news_items_published on news_items (published_at desc);
-- Migrasi: kolom topic_key dan ranking trending sudah dihapus.
alter table news_items drop column if exists topic_key;
drop index if exists idx_news_items_topic;
"""

def parse_date(raw: str | None) -> datetime | None:
    """Tanggal (`YYYY-MM-DD`) dari atribut `time[datetime]`. Snips tidak menyertakan
    jam, jadi dipatok tengah malam WIB lalu dikonversi ke UTC."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WIB)
    return dt.astimezone(UTC)


def parse_snips(html: str) -> list[dict[str, Any]]:
    """Ambil kartu snips dari blok `SECTIONS`, buang duplikat (url) dan item tanpa
    judul/tanggal. Judul dinormalkan (emoji pemanis dibuang) agar enak dibaca."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select("div.summary-item"):
        link = card.select_one("a.summary-title-link")
        stamp = card.select_one("time[datetime]")
        if link is None or stamp is None:
            continue
        title = _clean_title(link.get_text(" ", strip=True))
        published = parse_date(stamp.get("datetime"))
        url = urljoin(BASE_URL, link.get("href") or "")
        if not (title and url and published) or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "url": url,
                "title": title,
                "source": SOURCE,
                "published_at": published,
            }
        )
    return items


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def fetch_snips(url: str = BASE_URL, timeout: int = 20) -> list[dict[str, Any]]:
    """Gagal -> [] supaya satu kegagalan jaringan tidak menghentikan scheduler."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_snips(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[warn] snips gagal: {e}")
        return []


def _dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL/SUPABASE_DB_URL belum diset")
    return dsn


def ensure_table(conn: psycopg.Connection) -> None:
    conn.execute(DDL)


def upsert(conn: psycopg.Connection, items: list[dict[str, Any]]) -> int:
    """Simpan item baru (idempotent by url). Return jumlah baris yang benar-benar baru."""
    inserted = 0
    for it in items:
        cur = conn.execute(
            """
            insert into news_items (url, title, source, published_at)
            values (%(url)s, %(title)s, %(source)s, %(published_at)s)
            on conflict (url) do nothing
            """,
            it,
        )
        inserted += cur.rowcount
    return inserted


RETENTION_DAYS = 7

def prune(conn: psycopg.Connection, days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
    """Hapus berita yang `published_at`-nya lebih dari `days` hari yang lalu."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    cur = conn.execute("delete from news_items where published_at < %s", (cutoff,))
    return cur.rowcount

def run_once() -> int:
    """Scrape Snips, simpan, buang berita >7 hari, return jumlah item baru."""
    items = fetch_snips()
    with psycopg.connect(_dsn(), autocommit=False, connect_timeout=10) as conn:
        ensure_table(conn)
        new = upsert(conn, items)
        prune(conn)
        conn.commit()
    return new


def latest(cur: Any, limit: int = 7) -> list[dict[str, Any]]:
    cur.execute(
        """
        select title, source, url, published_at
        from news_items
        order by published_at desc, fetched_at desc
        limit %s
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]