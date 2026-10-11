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
