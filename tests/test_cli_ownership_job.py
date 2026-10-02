"""Unit tests: helper scheduler snapshot pemegang saham (tanpa DB/network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from idx_scraper import cli

WIB = timezone(timedelta(hours=7))


def test_snapshot_ownership_empty_codes_is_noop():
    # Tidak ada kode -> tidak menyentuh DB/network sama sekali.
    assert cli.snapshot_ownership([]) == (0, 0)


def test_next_weekday_at_picks_next_monday():
    # Senin berikutnya harus jatuh di hari Senin (0), jam yang diminta, dan
    # selalu di masa depan.
    nxt = cli._next_weekday_at(0, 6, 45)
    assert nxt.weekday() == 0
    assert (nxt.hour, nxt.minute) == (6, 45)
    assert nxt > datetime.now(WIB)


def test_next_weekday_at_rolls_past_today(monkeypatch):
    # Bila sekarang sudah lewat jam target di hari yang sama, jadwal harus
    # digeser 7 hari ke depan (bukan waktu di masa lalu).
    frozen = datetime(2026, 10, 5, 8, 0, tzinfo=WIB)  # Senin 08:00 WIB

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(cli, "datetime", _FixedDatetime)
    nxt = cli._next_weekday_at(0, 6, 45)
    # Senin berikutnya pukul 06:45 (satu minggu setelah Senin ini).
    assert nxt == datetime(2026, 10, 12, 6, 45, tzinfo=WIB)
    assert nxt > frozen
