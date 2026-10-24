"""``cli._eod_fresh_for``: close indeks harian ikut dihitung, bukan cuma snapshot 16:00.

Latar 22 Okt 2026: mesin mati sepanjang hari, jadi tidak ada snapshot
``index_quotes`` pukul 16:00. Close resmi sesi itu tetap ada di
``index_summary_daily`` (ditambal dari Yahoo), dan ``regime_daily``-nya bisa
dihitung. Tanpa aturan ini, catchup startup terus melaporkan sesi tersebut
"tanpa close indeks resmi" padahal seri hariannya lengkap.

Tes ini tidak menyentuh DB: koneksi psycopg dipalsukan dan tiap jawaban query
dikembalikan berurutan.
"""

from __future__ import annotations

from datetime import date

import psycopg

from idx_scraper import cli


class _Cur:
    """Kursor palsu: satu jawaban ``fetchone`` per ``execute``."""

    def __init__(self, jawaban) -> None:
        self._jawaban = list(jawaban)
        self.perintah: list[str] = []
        self._hasil = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.perintah.append(" ".join(str(sql).split()))
        self._hasil = self._jawaban.pop(0) if self._jawaban else None

    def fetchone(self):
        return self._hasil


class _Koneksi:
    def __init__(self, cur) -> None:
        self._cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cur


def test_close_harian_dihitung_tanpa_snapshot_1600(monkeypatch) -> None:
    """Saham ada, snapshot 16:00 tidak ada, close harian ada -> sesi lengkap."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://contoh")
    cur = _Cur([(1,), None, (1,)])
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Koneksi(cur))

    assert cli._eod_fresh_for(date(2026, 10, 22)) == (True, True)
    assert any("index_summary_daily" in p for p in cur.perintah), cur.perintah
