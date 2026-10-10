"""CLI: unduh logo emiten IDX dari halaman simbol TradingView.

Alur per kode:
  1. GET https://www.tradingview.com/symbols/IDX-{CODE}/
  2. Cari URL logo perusahaan (s3-symbol-logo ... --600.png / --big.svg).
  3. Unduh, validasi signature + dimensi, simpan ke idx-web/public/logos/.
  4. Catat hasil di idx-web/public/logos/manifest.json.

Status manifest:
  ok        -> file tersimpan dan valid
  fallback  -> halaman tidak punya logo perusahaan (UI tampilkan monogram)
  failed    -> error jaringan / file tidak valid

Jalankan manual (dari idx-scraper/):
    set -a && . ./.env && set +a
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_tradingview_logos
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_tradingview_logos --only BBCA,TLKM
    PYTHONPATH=src .venv/Scripts/python.exe -m scripts.fetch_tradingview_logos --force
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
LOGO_DIR = ROOT / "idx-web" / "public" / "logos"
MANIFEST = LOGO_DIR / "manifest.json"
ENV_FILE = ROOT / "idx-scraper" / ".env"

PAGE_URL = "https://www.tradingview.com/symbols/IDX-{code}/"
LOGO_RE = re.compile(
    r"https://s3-symbol-logo\.tradingview\.com/([a-z0-9-]+?)(--600\.png|--big\.svg)"
)
USER_AGENT = "market-labs-logo-sync/1.0 (+contact: local dev; polite 1 req/s)"
TIMEOUT_S = 15
MAX_BYTES = 2 * 1024 * 1024
MIN_PX = 32
MIN_BYTES = 500
RATE_LIMIT_S = 1.0

_last_request = 0.0


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def universe_codes() -> list[str]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "SELECT DISTINCT code FROM stock_summary_daily ORDER BY code"
        ).fetchall()
    return [r[0].upper() for r in rows if r[0]]


def throttle() -> None:
    global _last_request
    wait = RATE_LIMIT_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def http_get(url: str, accept: str) -> bytes:
    throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"respons melebihi {MAX_BYTES} byte")
    return data


def find_logo_url(html: str) -> str | None:
    # Hindari logo indeks/sumber (source/IDX.svg, indices/*) — regex hanya cocok slug perusahaan.
    for match in LOGO_RE.finditer(html):
        slug = match.group(1)
        if slug.startswith(("indices", "source")):
            continue
        return match.group(0)
    return None


def validate_image(data: bytes) -> tuple[str, int | None]:
    """Kembalikan (format, dimensi_px). Raise ValueError bila tidak valid."""
    if len(data) < MIN_BYTES:
        raise ValueError(f"file terlalu kecil ({len(data)} byte)")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        if min(width, height) < MIN_PX:
            raise ValueError(f"PNG terlalu kecil ({width}x{height})")
        return "png", width
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg", None
    raise ValueError("signature gambar tidak dikenali")


def load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST.exists():
        return {}
    entries = json.loads(MANIFEST.read_text(encoding="utf-8")).get("entries", [])
    return {e["code"]: e for e in entries}


def save_manifest(entries: dict[str, dict[str, Any]]) -> None:
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "tradingview",
        "license_note": "[BELUM TERVERIFIKASI] Logo milik masing-masing emiten dan TradingView.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            s: sum(1 for e in entries.values() if e["status"] == s)
            for s in ("ok", "fallback", "failed")
        },
        "entries": [entries[k] for k in sorted(entries)],
    }
    MANIFEST.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_one(code: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "source": "tradingview",
        "page_url": PAGE_URL.format(code=code),
        "logo_url": None,
        "file": None,
        "format": None,
        "bytes": None,
        "status": "failed",
        "reason": None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        html = http_get(entry["page_url"], "text/html").decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            entry.update(status="fallback", reason="halaman simbol 404")
        else:
            entry["reason"] = f"HTTP {exc.code} pada halaman simbol"
        return entry
    except Exception as exc:  # noqa: BLE001 - catat dan lanjut
        entry["reason"] = f"halaman: {exc}"
        return entry

    logo_url = find_logo_url(html)
    if not logo_url:
        entry.update(status="fallback", reason="tidak ada logo perusahaan di halaman")
        return entry
    entry["logo_url"] = logo_url

    try:
        data = http_get(logo_url, "image/png,image/svg+xml,image/*")
        fmt, _ = validate_image(data)
    except Exception as exc:  # noqa: BLE001
        entry["reason"] = f"logo: {exc}"
        return entry

    ext = "png" if fmt == "png" else "svg"
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    (LOGO_DIR / f"{code}.{ext}").write_bytes(data)
    entry.update(file=f"{code}.{ext}", format=fmt, bytes=len(data), status="ok")
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="unduh ulang kode yang sudah ok")
    parser.add_argument("--only", help="daftar kode dipisah koma, mis. BBCA,TLKM")
    args = parser.parse_args()

    load_env()
    manifest = load_manifest()
    codes = universe_codes()
    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",") if c.strip()}
        codes = [c for c in codes if c in wanted]
    print(f"Universe: {len(codes)} kode")

    todo = []
    for code in codes:
        prev = manifest.get(code)
        file_ok = prev and prev.get("status") == "ok" and (LOGO_DIR / prev["file"]).exists()
        if file_ok and not args.force:
            continue
        todo.append(code)
    print(f"Diunduh: {len(todo)} kode (sisanya sudah ok, lewati)")

    for i, code in enumerate(todo, 1):
        entry = fetch_one(code)
        manifest[code] = entry
        print(f"[{i}/{len(todo)}] {code}: {entry['status']}" + (f" ({entry['reason']})" if entry["reason"] else ""))
        if i % 25 == 0:
            save_manifest(manifest)

    save_manifest(manifest)
    counts = {s: sum(1 for e in manifest.values() if e["status"] == s) for s in ("ok", "fallback", "failed")}
    print(f"Ringkasan: ok={counts['ok']} fallback={counts['fallback']} failed={counts['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
