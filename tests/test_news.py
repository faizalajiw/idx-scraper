"""Tes offline untuk parser Snips dan ranking berita. Jalankan: pytest tests/test_news.py"""

from datetime import UTC, datetime, timedelta

from idx_scraper.news import WIB, parse_date, parse_snips

HTML = """
<div class="summary-item-list-container">
  <span class="summary-collection-title">Snips Terbaru</span>
  <div class="summary-item">
    <a class="summary-title-link" href="/snips-terbaru/a-slug">💸 APBN Cetak Defisit</a>
    <time datetime="2026-10-09">x</time>
  </div>
  <div class="summary-item">
    <a class="summary-title-link" href="/snips-terbaru/a-slug">Duplikat</a>
    <time datetime="2026-10-09">x</time>
  </div>
  <div class="summary-item">
    <a class="summary-title-link" href="/snips-terbaru/no-date">Tanpa tanggal</a>
  </div>
</div>
<div class="summary-item-list-container">
  <span class="summary-collection-title">Edukasi</span>
  <div class="summary-item">
    <a class="summary-title-link" href="/edukasi/b">Bonus Rp100 ribu</a>
    <time datetime="2026-01-25">x</time>
  </div>
</div>
"""


def test_parse_snips_extracts_unique_cards_with_absolute_urls():
    items = parse_snips(HTML)
    assert [i["url"] for i in items] == [
        "https://snips.stockbit.com/snips-terbaru/a-slug",
        "https://snips.stockbit.com/edukasi/b",
    ]
    assert items[0]["title"] == "💸 APBN Cetak Defisit"
    assert items[0]["source"] == "Stockbit Snips"


def test_parse_date_is_midnight_wib_in_utc():
    assert parse_date("2026-10-09") == datetime(2026, 10, 8, 17, tzinfo=UTC)
    assert parse_date("bukan-tanggal") is None
    assert parse_date(None) is None
    assert parse_date("2026-10-09T10:00:00+07:00").tzinfo is not None
    assert WIB.utcoffset(None) == timedelta(hours=7)


def test_run_once_melewati_item_lebih_tua_dari_retensi(monkeypatch):
    """Item yang lebih tua dari RETENTION_DAYS tidak boleh dikirim ke `upsert`.

    Kalau ikut dikirim, `prune` menghapusnya di transaksi yang sama — sehingga
    setiap run mengulang INSERT + DELETE untuk baris yang sama dan hitungan
    "item baru" di log melaporkan item yang sebenarnya langsung dibuang.
    """
    from idx_scraper import news

    now = datetime(2026, 10, 17, 4, 0, tzinfo=UTC)  # cutoff = 2026-10-10 04:00 UTC
    lama = {
        "url": "https://x/lama",
        "title": "Lama",
        "source": "Stockbit Snips",
        "published_at": datetime(2026, 7, 8, 17, tzinfo=UTC),
    }
    tepat_batas = {
        "url": "https://x/batas",
        "title": "Batas",
        "source": "Stockbit Snips",
        "published_at": datetime(2026, 10, 10, 4, tzinfo=UTC),
    }
    baru = {
        "url": "https://x/baru",
        "title": "Baru",
        "source": "Stockbit Snips",
        "published_at": datetime(2026, 10, 16, 17, tzinfo=UTC),
    }

    monkeypatch.setattr(news, "fetch_snips", lambda *a, **k: [lama, tepat_batas, baru])
    monkeypatch.setattr(news, "_dsn", lambda: "postgresql://fake/db")

    dikirim = []
    tersimpan: set[str] = set()

    class FakeCur:
        def __init__(self, rowcount: int) -> None:
            self.rowcount = rowcount

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            if "insert into news_items" not in sql:
                return FakeCur(0)
            url = params["url"]
            dikirim.append(url)
            # Meniru `on conflict (url) do nothing`: url yang sudah ada -> 0 baris.
            rowcount = 0 if url in tersimpan else 1
            tersimpan.add(url)
            return FakeCur(rowcount)

        def commit(self):
            pass

    monkeypatch.setattr(news.psycopg, "connect", lambda *a, **k: FakeConn())

    assert news.run_once(now=now) == 2
    assert "https://x/lama" not in dikirim
    # Batas inklusif, konsisten dengan `where published_at < cutoff` di prune,
    # supaya item di tepi retensi tidak bolak-balik masuk lalu terhapus.
    assert dikirim == ["https://x/batas", "https://x/baru"]
    # Run kedua atas input yang sama: nol item baru, jadi angka di log ("item baru")
    # melaporkan yang benar-benar tersimpan, bukan yang langsung dibuang `prune`.
    assert news.run_once(now=now) == 0


def test_prune_deletes_older_than_cutoff():
    from idx_scraper.news import prune

    calls = []

    class FakeCur:
        rowcount = 3

    class FakeConn:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return FakeCur()

    now = datetime(2026, 10, 11, 10, 0, tzinfo=UTC)
    removed = prune(FakeConn(), days=7, now=now)
    assert removed == 3
    sql, params = calls[0]
    assert sql == "delete from news_items where published_at < %s"
    assert params == (datetime(2026, 10, 4, 10, 0, tzinfo=UTC),)
