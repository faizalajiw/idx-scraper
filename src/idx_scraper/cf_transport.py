"""Cloudflare-protected transport for the IDX unofficial endpoints.

IDX sits behind Cloudflare's managed challenge. Once Cloudflare locks an IP in,
plain TLS impersonation (curl_cffi) is rejected with 403 "Just a moment...",
and a separately-acquired ``cf_clearance`` cookie is useless because it is tied
to a specific user-agent + IP and chronology.

The reliable path is to keep ONE real browser session alive and make every
request from inside that same page via ``page.evaluate(fetch(...))``, which
shares the page's (already-cleared) Cloudflare cookies and user-agent.

This module runs a persistent Chromium (``channel="chrome"``, this machine's
installed Chrome) on a dedicated background event-loop thread and issues IDX
JSON requests through the same page, so a single browser session serves the
whole lifetime of an ``IDXClient`` (including repeated scheduler polling).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from typing import Any

IDX_BASE = "https://www.idx.co.id"
PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "idx-scraper-chrome-profile",
)
CHALLENGE_TITLES = ("just a moment", "tunggu sebentar", "checking your browser")


def _is_challenge(title: str) -> bool:
    return any(t in title.lower() for t in CHALLENGE_TITLES)


class BrowserTransport:
    """Persistent Chrome page on a background event loop; curl-like get()."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._page: Any = None
        self._context: Any = None
        self._playwright: Any = None
        self._ready = threading.Event()

    # ---------------- background loop management ----------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop

        async def _run():
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            context = await self._playwright.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 800},
                locale="id-ID",
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"{IDX_BASE}/id", wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):
                await page.wait_for_timeout(2500)
                title = await page.title()
                if not _is_challenge(title):
                    break
            self._context, self._page = context, page

        def _bootstrap():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(_run())
            except Exception as e:  # noqa: BLE001
                print(f"[cf-transport] browser open failed: {e!r}", file=sys.stderr)
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=_bootstrap, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=180)
        return self._loop

    def _call(self, coro):
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=120)

    # ---------------- public API ----------------

    def ensure_open(self) -> Any:
        self._ensure_loop()
        return self._page

    def get(self, path: str) -> Any | None:
        """Fetch an IDX JSON endpoint from inside the cleared page."""
        page = self.ensure_open()

        async def _fetch():
            if page.is_closed():
                raise RuntimeError("browser page closed")
            url = f"{IDX_BASE}{path}"
            data = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, { headers: { 'Accept': 'application/json' } });
                    const text = await r.text();
                    return { status: r.status, text: text.slice(0, 2000000) };
                }""",
                url,
            )
            if data["status"] != 200:
                print(f"[warn] IDX {path} status {data['status']}", file=sys.stderr)
                return None
            try:
                return json.loads(data["text"])
            except json.JSONDecodeError:
                print(f"[warn] IDX {path} non-JSON body", file=sys.stderr)
                return None

        return self._call(_fetch())

    def close(self) -> None:
        if self._loop is None:
            return

        async def _close():
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:  # noqa: BLE001
                    pass
                self._context = None
                self._page = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None

        try:
            self._call(_close())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._loop = None


