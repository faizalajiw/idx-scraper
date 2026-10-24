"""Penyiapan logging terpusat dan satu pintu keluaran untuk seluruh paket.

Dua mode, dipilih otomatis:

- Sebelum :func:`setup` dipanggil, :func:`say` menulis langsung ke stdout/stderr.
  Ini perilaku ``print`` lama, jadi tes (``capsys``) dan pemanggil pustaka tidak
  berubah.
- Setelah :func:`setup` (dipanggil ``idx`` sekali di awal setiap perintah),
  :func:`say` menulis lewat ``logging``: tiap baris membawa waktu, level, nama
  job, dan ikut ke berkas bila ``IDX_LOG_DIR`` diset.

Karena itu ``print`` hanya boleh muncul di modul ini; modul lain memakai
``say`` (keluaran manusia) atau ``logger`` (diagnostik).

Env:
- ``IDX_LOG_LEVEL`` — DEBUG/INFO/WARNING/... (default INFO).
- ``IDX_LOG_DIR`` — direktori berkas log harian; kosong = hanya stderr.

DSN dan token disamarkan oleh :class:`_Redaksi` sebelum keluar ke handler mana
pun, supaya berkas log aman ditempel ke tiket.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_aktif = False
_job = "idx"

#: ``postgresql://user:rahasia@host`` -> ``postgresql://user:***@host``
_DSN = re.compile(r"((?:postgres(?:ql)?(?:\+\w+)?|rediss?)://[^:/@\s]+):[^@\s]+@")
#: ``TOKEN=abc``, ``api_key: abc`` -> nilainya disamarkan
_KUNCI = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:token|api[_-]?key|password|passwd|secret|dsn))\b(\s*[=:]\s*)([^\s,;'\"()]+)"
)


def _samarkan(m: re.Match[str]) -> str:
    kunci, pemisah, nilai = m.group(1), m.group(2), m.group(3)
    if "://" in nilai:  # DSN utuh sudah ditangani _DSN; jangan sembunyikan hostnya
        return kunci + pemisah + nilai
    return f"{kunci}{pemisah}***"


class _Redaksi(logging.Filter):
    """Samarkan DSN dan nilai berkunci sebelum record keluar ke handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        pesan = record.getMessage()
        pesan = _DSN.sub(r"\1:***@", pesan)
        pesan = _KUNCI.sub(_samarkan, pesan)
        if "args" in record.__dict__:
            record.args = ()
        record.msg = pesan
        return True


class _StreamAman(logging.StreamHandler):
    """StreamHandler yang tidak menjatuhkan jalur saat konsol tidak bisa emoji."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except UnicodeEncodeError:
            try:
                ascii_pesan = self.format(record).encode("ascii", "replace").decode("ascii")
                self.stream.write(f"{ascii_pesan}{self.terminator}")
                self.flush()
            except Exception:  # pragma: no cover - konsol rusak total
                self.handleError(record)


def _level() -> int:
    nama = os.getenv("IDX_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, nama, logging.INFO) if nama else logging.INFO


def setup(job: str = "idx") -> None:
    """Pasang handler stderr (dan berkas bila ``IDX_LOG_DIR`` diset). Idempoten."""
    global _aktif, _job
    if _aktif:
        return
    _job = job
    _aktif = True

    akar = logging.getLogger()
    akar.setLevel(_level())
    arus = _StreamAman(stream=sys.stderr)
    arus.setFormatter(logging.Formatter("%(message)s"))
    arus.addFilter(_Redaksi())
    akar.addHandler(arus)

    direktori = os.getenv("IDX_LOG_DIR", "").strip()
    if direktori:
        Path(direktori).mkdir(parents=True, exist_ok=True)
        berkas = logging.handlers.RotatingFileHandler(
            Path(direktori) / f"idx-{datetime.now():%Y%m%d}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        berkas.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(job)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        berkas.addFilter(_Redaksi())
        akar.addHandler(berkas)

    for bising in ("urllib3", "httpx", "httpcore", "yfinance", "asyncio"):
        logging.getLogger(bising).setLevel(logging.WARNING)

    # `job` dibaca formatter berkas; disuntik untuk record dari logger mana pun.
    lama = logging.getLogRecordFactory()

    def _pabrik(*args, **kwargs):  # type: ignore[no-untyped-def]
        record = lama(*args, **kwargs)
        if not hasattr(record, "job"):
            record.job = _job
        return record

    logging.setLogRecordFactory(_pabrik)


def say(*args: object, err: bool = False, level: int | None = None, **kw: object) -> None:
    """Keluaran manusia (status, tabel, laporan CLI).

    Sebelum :func:`setup` sama dengan ``print``: ke stdout, atau ke stderr bila
    ``err=True``. Sesudahnya jadi record log pada `level` (default INFO, atau
    ERROR bila `err=True`) sehingga ikut berlabel waktu dan nama job.
    """
    pesan = " ".join(str(a) for a in args)
    if _aktif:
        logger.log(level or (logging.ERROR if err else logging.INFO), pesan)
        return
    target = kw.pop("file", None)
    if target is not None:
        print(pesan, file=target, **kw)
    elif err:
        print(pesan, file=sys.stderr, **kw)
    else:
        print(pesan, **kw)
