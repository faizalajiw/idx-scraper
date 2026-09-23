"""Read/write the IDX_WATCHLIST line in the project .env file.

The .env file is the single source of truth for the watchlist: the scraper
polls it (cli.py via load_dotenv) and the API defaults to it. Editing is
line-preserving so comments and unrelated keys survive a rewrite.

IDX_ENV_FILE may override the .env location (used by tests).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^\s*(?:export\s+)?IDX_WATCHLIST\s*=")
_COMMENT_KEY_RE = re.compile(r"^\s*#\s*(?:export\s+)?IDX_WATCHLIST\s*=")


def _env_path() -> Path:
    override = os.getenv("IDX_ENV_FILE")
    if override:
        return Path(override)
    # Project root = two levels above this file (src/idx_scraper/ -> ..).
    return Path(__file__).resolve().parents[2] / ".env"


def _split_codes(raw: str) -> list[str]:
    return list(dict.fromkeys(c.strip().upper() for c in raw.split(",") if c.strip()))


def read_watchlist(path: Path | None = None) -> list[str]:
    """Return the deduped, uppercased codes from the IDX_WATCHLIST line."""
    p = path or _env_path()
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        if _KEY_RE.match(line):
            return _split_codes(line.split("=", 1)[1])
    return []


def write_watchlist(codes: list[str], path: Path | None = None) -> list[str]:
    """Replace the IDX_WATCHLIST line, preserving comments and other keys."""
    clean = _split_codes(",".join(codes))
    p = path or _env_path()
    try:
        lines = p.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    new_line = f"IDX_WATCHLIST={','.join(clean)}"

    replaced = False
    for i, line in enumerate(lines):
        if _KEY_RE.match(line):
            lines[i] = new_line
            replaced = True
            break

    if replaced:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return clean
    if lines:
        lines.append(new_line)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return clean
    # No usable .env file: refuse silently? No — fail loudly, config is missing.
    raise FileNotFoundError(
        f"{p} not found and has no content; cannot persist IDX_WATCHLIST"
    )


def update_watchlist(add: list[str], remove: list[str], path: Path | None = None) -> list[str]:
    """Add/remove codes, keeping current order (appends at the end)."""
    current = read_watchlist(path)
    add_clean = _split_codes(",".join(add))
    remove_clean = {c.strip().upper() for c in remove if c.strip()}
    merged = [c for c in current if c not in remove_clean]
    merged += [c for c in add_clean if c not in set(merged)]
    return write_watchlist(merged, path)
