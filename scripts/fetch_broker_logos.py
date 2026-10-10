"""CLI: unduh logo broker IDX dari aset statis idx.co.id.

Alur per kode:
  1. Ambil daftar kode dari research.broker_daily.
  2. GET https://www.idx.co.id/StaticData/Brokers/Logo/{CODE}.jpg lewat
     BrowserTransport (Chrome persisten), karena IDX memblokir curl biasa
     dengan Cloudflare (403). Kandidat URL: .jpg lalu .JPG (beberapa kode
     memakai huruf besar).
  3. Validasi signature (JPEG/PNG/BMP) dan ukuran minimum, simpan ke
     idx-web/public/logos/brokers/.
  4. Catat hasil di idx-web/public/logos/brokers/manifest.json.

Status manifest:
  ok        -> file tersimpan dan valid
  fallback  -> aset logo tidak tersedia di IDX (UI tampilkan kode saja)
  failed    -> error browser/jaringan atau file tidak valid

Jalankan manual (dari idx-scraper/). Chrome profil harus tidak sedang
dipakai scheduler; pakai IDX_CHROME_PROFILE_DIR berbeda bila perlu.
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_broker_logos
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_broker_logos --only YU,CC
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_broker_logos --force
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
LOGO_DIR = ROOT / "idx-web" / "public" / "logos" / "brokers"
MANIFEST = LOGO_DIR / "manifest.json"
ENV_FILE = ROOT / "idx-scraper" / ".env"

LOGO_URL = "https://www.idx.co.id/StaticData/Brokers/Logo/{code}{ext}"
CANDIDATE_EXT = (".jpg", ".JPG")
MIN_BYTES = 200
MIN_PX = 16
RATE_LIMIT_S = 1.0
MAX_ATTEMPTS = 2  # 503/transient: coba ulang sekali


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def broker_codes() -> list[str]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "SELECT DISTINCT broker_code FROM research.broker_daily ORDER BY broker_code"
        ).fetchall()
    return [r[0].upper() for r in rows if r[0]]


def validate_image(data: bytes) -> tuple[str, str]:
    """Kembalikan (format, ekstensi_file). Raise ValueError bila tidak valid."""
    if len(data) < MIN_BYTES:
        raise ValueError(f"file terlalu kecil ({len(data)} byte)")
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        if min(width, height) < MIN_PX:
            raise ValueError(f"PNG terlalu kecil ({width}x{height})")
        return "png", "png"
    if data[:2] == b"BM":
        width, height = struct.unpack("<ii", data[18:26])
        if min(abs(width), abs(height)) < MIN_PX:
            raise ValueError(f"BMP terlalu kecil ({width}x{height})")
        return "bmp", "bmp"
    raise ValueError("signature gambar tidak dikenali")


def load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST.exists():
        return {}
    entries = json.loads(MANIFEST.read_text(encoding="utf-8")).get("entries", [])
    return {e["code"]: e for e in entries}


def save_manifest(entries: dict[str, dict[str, Any]]) -> None:
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "idx.co.id/StaticData/Brokers/Logo",
        "license_note": "[BELUM TERVERIFIKASI] Logo milik masing-masing broker dan Bursa Efek Indonesia.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            s: sum(1 for e in entries.values() if e["status"] == s)
            for s in ("ok", "fallback", "failed")
        },
        "entries": [entries[k] for k in sorted(entries)],
    }
    MANIFEST.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_one(transport: Any, code: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "logo_url": None,
        "file": None,
        "format": None,
        "bytes": None,
        "status": "failed",
        "reason": None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    last_status: int | None = None
    for ext in CANDIDATE_EXT:
        url = LOGO_URL.format(code=code, ext=ext)
        for attempt in range(MAX_ATTEMPTS):
            time.sleep(RATE_LIMIT_S)
            try:
                data = transport.get_bytes(url)
            except Exception as exc:  # noqa: BLE001 - catat dan lanjut
                entry["reason"] = f"browser: {exc}"
                return entry
            last_status = transport.last_status
            if data is not None:
                break
            if last_status == 404:
                break
        if data is None:
            continue
        try:
            fmt, file_ext = validate_image(data)
        except ValueError as exc:
            entry["logo_url"] = url
            entry["reason"] = f"logo: {exc}"
            return entry
        LOGO_DIR.mkdir(parents=True, exist_ok=True)
        (LOGO_DIR / f"{code}.{file_ext}").write_bytes(data)
        entry.update(
            logo_url=url,
            file=f"{code}.{file_ext}",
            format=fmt,
            bytes=len(data),
            status="ok",
        )
        return entry

    if last_status == 404:
        entry.update(status="fallback", reason="aset logo tidak ada di IDX (404)")
    else:
        entry["reason"] = f"HTTP {last_status} pada semua kandidat URL"
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="unduh ulang kode yang sudah ok")
    parser.add_argument("--only", help="daftar kode dipisah koma, mis. YU,CC")
    args = parser.parse_args()

    load_env()
    manifest = load_manifest()
    codes = broker_codes()
    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",") if c.strip()}
        codes = [c for c in codes if c in wanted]
    print(f"Universe: {len(codes)} kode broker")

    todo = []
    for code in codes:
        prev = manifest.get(code)
        file_ok = prev and prev.get("status") == "ok" and (LOGO_DIR / prev["file"]).exists()
        if file_ok and not args.force:
            continue
        todo.append(code)
    print(f"Diunduh: {len(todo)} kode (sisanya sudah ok, lewati)")
    if not todo:
        save_manifest(manifest)
        return 0

    from idx_scraper.cf_transport import BrowserTransport

    transport = BrowserTransport()
    try:
        for i, code in enumerate(todo, 1):
            entry = fetch_one(transport, code)
            manifest[code] = entry
            print(f"[{i}/{len(todo)}] {code}: {entry['status']}" + (f" ({entry['reason']})" if entry["reason"] else ""))
            if i % 10 == 0:
                save_manifest(manifest)
    finally:
        transport.close()

    save_manifest(manifest)
    counts = {s: sum(1 for e in manifest.values() if e["status"] == s) for s in ("ok", "fallback", "failed")}
    print(f"Ringkasan: ok={counts['ok']} fallback={counts['fallback']} failed={counts['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
