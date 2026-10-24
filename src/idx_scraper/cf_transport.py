"""Transport IDX — jalur utama tanpa browser.

Versi lama modul ini menyatakan IDX wajib lewat Chrome headful karena curl_cffi
selalu kena 403. Itu sudah tidak berlaku (diuji 15 Okt 2026): dengan
``impersonate="chrome124"``, seluruh endpoint JSON IDX — GetIndexList,
GetStockSummary, GetBrokerSummary, GetTradingInfoDaily, GetSecuritiesStock —
menjawab 200 tanpa kuki dan tanpa browser, 23 percobaan berturut-turut selama
45 menit, 0 gagal. Cloudflare IDX cukup puas dengan fingerprint TLS curl_cffi;
tidak ada challenge JS yang perlu dipecahkan.

Karena itu jalur utamanya ``HttpTransport``: `idx serve` bisa jalan di server
Linux tanpa Chrome, tanpa Xvfb, dan tanpa profil persisten yang mudah terkunci
(chrome yatim menahan profil = EOD masuk 0 baris, akar masalah lama).

``BrowserTransport`` tetap dipertahankan sebagai cadangan bila suatu hari
Cloudflare memperketat, dan ``AutoTransport`` memilih di antara keduanya.
Modul ini hanya mengimpor playwright di dalam BrowserTransport, jadi jalur HTTP
tidak pernah menyentuh Playwright sama sekali.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from typing import Any

from curl_cffi import requests as cf_requests

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

# Target impersonate curl_cffi. Menaikkan nilainya adalah satu-satunya perubahan
# yang perlu bila Cloudflare IDX memperketat pemeriksaan fingerprint.
IMPERSONATE = os.environ.get("IDX_IMPERSONATE") or "chrome124"
# Header disamakan dengan XHR halaman IDX sendiri; urutannya ikut terbaca CF.
_HTTP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Referer": f"{IDX_BASE}/",
    "X-Requested-With": "XMLHttpRequest",
}
# Lama jalur HTTP diistirahatkan setelah ditolak, sebelum dicoba lagi.
_HTTP_BLOCKED_SEC = int(os.environ.get("IDX_HTTP_COOLDOWN_SEC") or "600")


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
                with contextlib.suppress(Exception):
                    await self._context.close()
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

    def get_bytes(self, url: str) -> bytes | None:
        """Fetch any URL (biasanya binary: gambar) dari dalam halaman yang sudah lolos CF.

        Dipakai untuk aset statis ber-proteksi Cloudflare (mis. logo broker IDX)
        yang tidak bisa diunduh dengan curl biasa.
        """
        page = self.ensure_open()

        async def _fetch():
            if page is None or page.is_closed():
                raise RuntimeError("browser page closed (revive blocked or profile locked)")
            data = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u);
                    if (!r.ok) return { status: r.status, b64: null };
                    const buf = await r.arrayBuffer();
                    let binary = '';
                    const bytes = new Uint8Array(buf);
                    const chunk = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunk) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    return { status: r.status, b64: btoa(binary) };
                }""",
                url,
            )
            self.last_status = data["status"]
            if data["status"] != 200 or not data.get("b64"):
                print(f"[warn] CF fetch {url} status {data['status']}", file=sys.stderr)
                return None
            import base64

            return base64.b64decode(data["b64"])

        return self._call(_fetch())

    def close(self) -> None:
        if self._loop is None:
            return

        async def _close():
            if self._context is not None:
                with contextlib.suppress(Exception):
                    await self._context.close()
                self._context = None
                self._page = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
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


class HttpTransport:
    """Transport tanpa browser: satu sesi curl_cffi untuk seluruh endpoint IDX.

    Kontraknya sama dengan BrowserTransport supaya bisa saling tukar:
    - ``get(path)``  -> JSON (dict/list) pada 200, ``None`` pada non-200/non-JSON
    - ``get(path)``  -> melempar exception bila koneksi gagal (transport mati),
      sama seperti BrowserTransport yang melempar saat halaman tak bisa dipakai
    - ``last_status`` menyimpan HTTP status terakhir agar 429 bisa dibedakan
      dari payload kosong yang sah
    """

    def __init__(self, impersonate: str | None = None) -> None:
        self.impersonate = impersonate or IMPERSONATE
        self._session = cf_requests.Session()
        self.last_status: int | None = None

    def ensure_open(self) -> Any:
        """Tidak ada yang perlu dibuka — ada agar API-nya seragam."""
        return self

    def get(self, path: str) -> Any | None:
        url = f"{IDX_BASE}{path}"
        r = self._session.get(url, impersonate=self.impersonate, headers=_HTTP_HEADERS, timeout=30)
        self.last_status = r.status_code
        if r.status_code != 200:
            print(f"[warn] IDX {path} status {r.status_code}", file=sys.stderr)
            return None
        try:
            return r.json()
        except Exception:
            print(f"[warn] IDX {path} non-JSON body", file=sys.stderr)
            return None

    def get_bytes(self, url: str) -> bytes | None:
        """Unduh aset statis (mis. logo broker). Best-effort: None bila gagal."""
        try:
            r = self._session.get(url, impersonate=self.impersonate, timeout=30)
        except Exception as e:
            print(f"[warn] http fetch {url} gagal: {e!r}", file=sys.stderr)
            return None
        self.last_status = r.status_code
        if r.status_code != 200:
            print(f"[warn] http fetch {url} status {r.status_code}", file=sys.stderr)
            return None
        return r.content

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._session.close()


class AutoTransport:
    """curl_cffi dulu; pindah sementara ke browser bila IDX menolaknya.

    Tambahan browser hanya terpakai kalau HTTP benar-benar gagal, jadi di mesin
    tanpa Chrome IDX tetap jalan. Setelah HTTP ditolak, jalur HTTP diistirahatkan
    ``IDX_HTTP_COOLDOWN_SEC`` detik (pola sama dengan ``_revive_blocked_until``
    di BrowserTransport) supaya tiap request tidak membayar timeout + buka Chrome.
    """

    def __init__(self, http_factory: Any = None, browser_factory: Any = None) -> None:
        self._http_factory = http_factory or HttpTransport
        self._browser_factory = browser_factory or BrowserTransport
        self._http: Any = None
        self._browser: Any = None
        self._http_blocked_until = 0.0
        self.last_status: int | None = None

    @property
    def http(self) -> Any:
        if self._http is None:
            self._http = self._http_factory()
        return self._http

    @property
    def browser(self) -> Any:
        if self._browser is None:
            self._browser = self._browser_factory()
        return self._browser

    def ensure_open(self) -> Any:
        # Hanya menyiapkan HTTP; Chrome tidak dibuka sampai memang dibutuhkan.
        return self.http.ensure_open()

    def get(self, path: str) -> Any | None:
        if time.monotonic() >= self._http_blocked_until:
            raw = None
            try:
                raw = self.http.get(path)
            except Exception as e:
                print(f"[transport] HTTP {path} gagal: {e!r}", file=sys.stderr)
            if raw is not None:
                self.last_status = self.http.last_status
                return raw
            self._http_blocked_until = time.monotonic() + _HTTP_BLOCKED_SEC
            print(
                f"[transport] HTTP ditolak (status {self.http.last_status}) — "
                f"pindah ke browser {_HTTP_BLOCKED_SEC}s",
                file=sys.stderr,
            )
        raw = self.browser.get(path)
        self.last_status = self.browser.last_status
        return raw

    def get_bytes(self, url: str) -> bytes | None:
        data = self.http.get_bytes(url)
        if data is not None:
            return data
        return self.browser.get_bytes(url)

    def close(self) -> None:
        for t in (self._http, self._browser):
            if t is not None:
                with contextlib.suppress(Exception):
                    t.close()


def make_transport() -> Any:
    """Pilih transport dari env ``IDX_TRANSPORT``.

    - ``auto`` (default) — HTTP dulu, browser hanya bila IDX menolak
    - ``http``           — tanpa browser sama sekali (server Linux tanpa Chrome)
    - ``browser``        — perilaku lama: selalu lewat Chrome
    """
    mode = (os.getenv("IDX_TRANSPORT") or "auto").strip().lower()
    if mode == "http":
        return HttpTransport()
    if mode == "browser":
        return BrowserTransport()
    if mode != "auto":
        print(f"[warn] IDX_TRANSPORT={mode!r} tidak dikenal — dipakai 'auto'", file=sys.stderr)
    return AutoTransport()
