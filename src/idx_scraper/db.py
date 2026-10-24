"""Satu-satunya pintu koneksi Postgres untuk paket ini.

Semua modul memakai :func:`connect` dan :func:`dsn`; tidak ada lagi
``psycopg.connect`` atau pembacaan ``DATABASE_URL`` yang tersebar. Dengan satu
titik ini, timeout, ``application_name``, autocommit, dan pesan galatnya
konsisten.

Penggunaan::

    from . import db          # di src/ (modul satu level dari db.py)
    from .. import db        # di api/ atau research/
    conn = db.connect()                   # autocommit=True, timeout=15
    conn = db.connect(autocommit=False)   # transaksi eksplisit
    with db.connect(dict_rows=True) as conn, conn.cursor() as cur:
        ...
"""

from __future__ import annotations

import os
from typing import Any

try:  # instalasi sqlite tanpa extra postgres tetap bisa impor paket ini
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - hanya jalur sqlite
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

#: Batas tunggu sambung bawaan (detik). Job boleh menurunkannya, tidak menaikkan
#: tanpa alasan: job terjadwal harus gagal cepat, bukan menggantung.
DEFAULT_TIMEOUT = 15

#: ``application_name`` supaya ``pg_stat_activity`` bisa memisahkan klien kita
#: dari alat lain saat menyelidiki koneksi yang menggantung.
APP_NAME = os.getenv("IDX_DB_APP_NAME", "idx-scraper")


class DatabaseUrlMissing(RuntimeError):
    """``DATABASE_URL`` belum diisi; satu-satunya pesan untuk kondisi ini."""


def dsn(url: str | None = None) -> str:
    """DSN Postgres dari argumen atau environment. Melempar bila kosong."""
    if url is not None:
        return url
    nilai = os.getenv("DATABASE_URL", "").strip()
    if not nilai:
        raise DatabaseUrlMissing(
            "DATABASE_URL belum diisi — isi di .env (lihat .env.example) atau "
            "variabel environment sebelum menjalankan perintah."
        )
    return nilai


def tersedia() -> bool:
    """True bila ``DATABASE_URL`` terisi — untuk jalur yang hanya perlu tahu ada/tidak."""
    return bool(os.getenv("DATABASE_URL", "").strip())


def connect(
    *,
    url: str | None = None,
    autocommit: bool = True,
    dict_rows: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> psycopg.Connection:
    """Buka koneksi dengan setelan konsisten.

    ``autocommit=True`` adalah bawaan karena hampir semua pembacaan di sini
    berumur pendek dan tidak boleh meninggalkan transaksi menggantung. Pemanggil
    yang butuh transaksi (beberapa ``insert`` berurutan) meminta
    ``autocommit=False`` secara eksplisit.
    """
    kw: dict[str, Any] = {
        "autocommit": autocommit,
        "connect_timeout": timeout,
        "application_name": APP_NAME,
    }
    if psycopg is None:
        raise RuntimeError(
            "psycopg[binary] not installed. Run: pip install 'idx-scraper[postgres]'"
        )
    if dict_rows:
        kw["row_factory"] = dict_row
    return psycopg.connect(dsn(url), **kw)
