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
import time
from typing import Any

IDX_BASE = "https://www.idx.co.id"
_REVIVE_COOLDOWN_SEC = 600
# Override via env supaya script manual bisa jalan berdampingan dengan
# scheduler yang sedang memegang profile utama (Chrome menolak dua instance
# pada user-data-dir yang sama).
PROFILE_DIR = os.environ.get("IDX_CHROME_PROFILE_DIR") or os.path.join(
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
        # HTTP status of the most recent get(); lets callers tell a 429 throttle
        # apart from a legitimately empty payload.
        self.last_status: int | None = None
        # Jangan retry revive terlalu sering saat profile dikunci proses lain.
        self._revive_blocked_until: float = 0.0

    # ---------------- background loop management ----------------

    async def _open_browser_async(self) -> None:
        """Buka Chrome persistent + lewati challenge; isi _context/_page.

        Dipisah dari _ensure_loop supaya bisa dipanggil ulang saat halaman
        mati (di-close manual / Chrome crash) tanpa membangun ulang loop.
        """
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

    def _revive_if_dead(self) -> Any:
        """Pastikan halaman hidup; bangun ulang bila tertutup/crash.

        Transport headful gampang mati karena user tidak sengaja menutup
        jendelanya. Tanpa revive, SEMUA fetch IDX gagal selamanya dengan
        "browser page closed" sampai `idx serve` di-restart manual.

        Bila revive gagal (mis. profile masih dikunci Chrome lain), jangan
        retry tiap request: tunda _REVIVE_COOLDOWN_SEC agar log tidak banjir.
        """
        self._ensure_loop()
        page = self._page
        if page is not None and not page.is_closed():
            return page
        if self._loop is None or not self._loop.is_running():
            return page  # loop juga mati -> biarkan _ensure_loop yang rebuild
        if time.monotonic() < self._revive_blocked_until:
            return page

        async def _reopen():
            # Tutup sisa context lama (kalau ada) sebelum buka yang baru.
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                self._page = None
            await self._open_browser_async()

        try:
            asyncio.run_coroutine_threadsafe(_reopen(), self._loop).result(timeout=180)
        except Exception as e:
            self._revive_blocked_until = time.monotonic() + _REVIVE_COOLDOWN_SEC
            print(
                f"[cf-transport] revive gagal: {e!r}; retry dalam {_REVIVE_COOLDOWN_SEC}s "
                f"(profile {PROFILE_DIR} mungkin dikunci Chrome lain)",
                file=sys.stderr,
            )
        return self._page

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop

        async def _run():
            await self._open_browser_async()

        def _bootstrap():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _startup():
                try:
                    await _run()
                except Exception as e:
                    print(f"[cf-transport] browser open failed: {e!r}", file=sys.stderr)
                finally:
                    self._ready.set()

            # Keep the loop alive for the whole transport lifetime so that
            # run_coroutine_threadsafe() from get()/close() actually executes.
            # (run_until_complete would stop the loop right after setup, leaving
            # Chrome running but the loop dead → the next call relaunches Chrome
            # on the same profile → "Opening in existing browser session".)
            self._loop.create_task(_startup())
            self._loop.run_forever()

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
        return self._revive_if_dead()

    def get(self, path: str) -> Any | None:
        """Fetch an IDX JSON endpoint from inside the cleared page."""
        page = self.ensure_open()

        async def _fetch():
            if page is None or page.is_closed():
                raise RuntimeError("browser page closed (revive blocked or profile locked)")
            url = f"{IDX_BASE}{path}"
            data = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, { headers: { 'Accept': 'application/json' } });
                    const text = await r.text();
                    return { status: r.status, text: text.slice(0, 2000000) };
                }""",
                url,
            )
            self.last_status = data["status"]
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
                except Exception:
                    pass
                self._context = None
                self._page = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

        try:
            self._call(_close())
        except Exception:
            pass
        finally:
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            self._loop = None
            self._ready.clear()


