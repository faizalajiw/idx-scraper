"""Halt manual penulisan klaim: satu berkas JSON, bukan tabel.

Gerbang klaim lapis 4 punya empat sebab; yang paling atas milik pemilik. Keadaan
itu disimpan di berkas kecil (default ``data/journal_halt.json``) dengan pola yang
sama seperti ``alerts_store``: konfigurasi pemilik tidak menulis DB, dan berkas
yang cacat tidak boleh mematikan scheduler.

``IDX_JOURNAL_HALT`` menimpa lokasinya (dipakai tes).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WIB = timezone(timedelta(hours=7))

#: Nama variabel lingkungan penimpa lokasi berkas.
HALT_PATH_ENV = "IDX_JOURNAL_HALT"

#: Keadaan sebelum ada apa-apa: tidak dihentikan, tanpa jejak.
_KOSONG: dict[str, Any] = {
    "halted": False,
    "reason": None,
    "at": None,
    "by": None,
    "note": None,
}


def _default_path() -> Path:
    override = os.getenv(HALT_PATH_ENV)
    if override:
        return Path(override)
    # Akar proyek = dua tingkat di atas berkas ini (src/idx_scraper/ -> ..).
    return Path(__file__).resolve().parents[2] / "data" / "journal_halt.json"


def _tulis(keadaan: dict[str, Any], path: Path | None) -> dict[str, Any]:
    """Simpan atomik (berkas sementara lalu replace), seperti alerts_store."""
    p = path or _default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(keadaan, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return keadaan


def state(path: Path | None = None) -> dict[str, Any]:
    """Keadaan halt apa adanya. Tidak pernah melempar.

    Berkas hilang berarti belum pernah dihentikan; berkas cacat juga dibaca
    "tidak dihentikan", tetapi sebabnya ditaruh di ``note`` supaya halt yang gagal
    dibaca tidak diam-diam berubah jadi halt yang tak terlihat.
    """
    p = path or _default_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(_KOSONG)
    except (OSError, ValueError) as e:
        # ValueError menampung json.JSONDecodeError sekaligus UnicodeDecodeError:
        # berkas yang bukan UTF-8 (mis. tersentuh editor lain) harus pulang sebagai
        # "tidak dihentikan" + catatan, bukan melempar ke pemanggil.
        return {**_KOSONG, "note": f"berkas halt rusak atau tidak terbaca: {e}"}
    if not isinstance(data, dict):
        return {**_KOSONG, "note": "berkas halt rusak: isinya bukan objek JSON"}
    return {
        "halted": bool(data.get("halted")),
        "reason": data.get("reason") or None,
        "at": data.get("at") or None,
        "by": data.get("by") or None,
        "note": None,
    }


def set_halt(
    *, reason: str, by: str = "pemilik", path: Path | None = None
) -> dict[str, Any]:
    """Hentikan penulisan klaim baru. Sebab wajib.

    Halt tanpa sebab tidak bisa dibedakan dari mesin yang rusak, dan itu justru
    yang membuat pemiliknya tidak percaya.
    """
    bersih = (reason or "").strip()
    if not bersih:
        raise ValueError("sebab halt wajib diisi")
    return _tulis(
        {
            "halted": True,
            "reason": bersih,
            "at": datetime.now(WIB).isoformat(timespec="seconds"),
            "by": by,
        },
        path,
    )


def clear(*, by: str = "pemilik", path: Path | None = None) -> dict[str, Any]:
    """Buka lagi penulisan klaim; waktu dan pelakunya tetap tercatat."""
    return _tulis(
        {
            "halted": False,
            "reason": None,
            "at": datetime.now(WIB).isoformat(timespec="seconds"),
            "by": by,
        },
        path,
    )
