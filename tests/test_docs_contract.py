"""Kontrak dokumentasi terhadap kode: README, DEPLOY-LINUX, dan rute FastAPI.

Tes ini menjaga agar dokumen tidak kembali menyimpang dari kode setelah audit
25 Okt 2026 (issue FAI-34, FAI-35, FAI-36, FAI-38).
"""

from __future__ import annotations

import re
from pathlib import Path

from idx_scraper.api.app import app

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8-sig")
DEPLOY = (ROOT / "DEPLOY-LINUX.md").read_text(encoding="utf-8-sig")

_ROW = re.compile(
    r"^\|\s*`(?P<path>/[^`]*)`\s*\|\s*(?P<method>GET|POST|PUT|PATCH|DELETE)\b", re.MULTILINE
)
_PARAM = re.compile(r"\{[^}]+\}")


def _normal(path: str) -> str:
    return _PARAM.sub("{}", path.split("?")[0].rstrip("/"))


ROUTES: dict[str, set[str]] = {}
for _route in app.routes:
    ROUTES.setdefault(_normal(_route.path), set()).update(
        getattr(_route, "methods", set()) or set()
    )


def _rows(text: str) -> list[tuple[str, str]]:
    section = text.split("## API Reference", 1)[1]
    return [(_normal(m.group("path")), m.group("method")) for m in _ROW.finditer(section)]


def test_readme_api_table_matches_real_routes() -> None:
    rows = _rows(README)
    assert rows, "tabel API Reference tidak ditemukan di README.md"

    missing = [
        f"{method} {path}"
        for path, method in rows
        if path not in ROUTES or method not in ROUTES[path]
    ]
    assert not missing, f"path di README tidak ada di api/app.py: {missing}"


def test_readme_does_not_claim_api_key_auth() -> None:
    assert "IDX_API_KEY" not in README
    assert "X-API-KEY" not in README


def test_readme_describes_sveltekit_frontend() -> None:
    for stale in ("Next.js", "NEXT_PUBLIC_", "Recharts", "SWR"):
        assert stale not in README, f"README masih menyebut {stale}"


def test_readme_places_scheduler_in_serve_process() -> None:
    assert "idx serve" in README, "README harus menyebut proses yang menjalankan scheduler"
    assert re.search(r"scheduler.{0,400}`idx serve`", README, re.DOTALL | re.IGNORECASE), (
        "narasi scheduler harus menunjuk proses `idx serve`, bukan API"
    )


def test_deploy_guide_has_no_nextjs_steps() -> None:
    for stale in ("next start", "NEXT_PUBLIC_", "next build"):
        assert stale not in DEPLOY, f"DEPLOY-LINUX.md masih memuat instruksi {stale}"
