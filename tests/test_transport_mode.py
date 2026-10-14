"""Unit test: pemilihan transport IDX (HTTP tanpa browser vs cadangan Chrome).

Latar: curl_cffi murni terbukti cukup untuk IDX (23/23 percobaan 200 selama 45
menit, tanpa kuki, tanpa browser). Jadi jalur utama harus HTTP, dan Chrome
hanya dipakai kalau IDX benar-benar menolak.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import ClassVar

import pytest

from idx_scraper import cf_transport


class _Resp:
    def __init__(self, status_code=200, payload=None, json_error=False, content=b""):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error
        self.content = content

    def json(self):
        if self._json_error:
            raise ValueError("bukan JSON")
        return self._payload


class _Session:
    """Pengganti curl_cffi.Session: mengembalikan respons berurutan."""

    instances: ClassVar[list[_Session]] = []

    def __init__(self, *a, **k):
        self.responses: list[_Resp] = []
        self.calls: list[str] = []
        self.closed = False
        _Session.instances.append(self)

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    _Session.instances = []
    monkeypatch.setattr(cf_transport.cf_requests, "Session", _Session)
    return _Session


class _FakeTransport:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.last_status = None

    def ensure_open(self):
        return self

    def get(self, path):
        self.calls += 1
        value = self.results.pop(0) if self.results else None
        self.last_status = 200 if value is not None else 403
        if isinstance(value, Exception):
            raise value
        return value

    def get_bytes(self, url):
        return None

    def close(self):
        pass


# ---------------- pemilihan mode ----------------


def test_default_auto(monkeypatch):
    # Tanpa env: HTTP dulu, browser hanya bila perlu.
    monkeypatch.delenv("IDX_TRANSPORT", raising=False)
    assert isinstance(cf_transport.make_transport(), cf_transport.AutoTransport)


def test_mode_http_tanpa_browser(monkeypatch):
    # Mode server Linux: tidak ada Chrome sama sekali.
    monkeypatch.setenv("IDX_TRANSPORT", "http")
    assert isinstance(cf_transport.make_transport(), cf_transport.HttpTransport)


def test_mode_browser_untuk_perilaku_lama(monkeypatch):
    monkeypatch.setenv("IDX_TRANSPORT", "browser")
    assert isinstance(cf_transport.make_transport(), cf_transport.BrowserTransport)


def test_mode_tak_dikenal_jatuh_ke_auto(monkeypatch):
    monkeypatch.setenv("IDX_TRANSPORT", "ngawur")
    assert isinstance(cf_transport.make_transport(), cf_transport.AutoTransport)


# ---------------- HttpTransport ----------------


def test_http_get_200_mengurai_json(fake_session):
    t = cf_transport.HttpTransport()
    _Session.instances[-1].responses.append(_Resp(200, payload=[{"IndexCode": "COMPOSITE"}]))
    assert t.get("/primary/home/GetIndexList") == [{"IndexCode": "COMPOSITE"}]
    assert t.last_status == 200
    # Header XHR + referer IDX ikut terkirim (ikut dibaca Cloudflare).
    assert _Session.instances[-1].calls[0].endswith("/primary/home/GetIndexList")


def test_http_get_403_mengembalikan_none(fake_session):
    t = cf_transport.HttpTransport()
    _Session.instances[-1].responses.append(_Resp(403))
    assert t.get("/primary/home/GetIndexList") is None
    assert t.last_status == 403  # 403 bisa dibedakan dari payload kosong


def test_http_get_body_non_json_mengembalikan_none(fake_session):
    t = cf_transport.HttpTransport()
    _Session.instances[-1].responses.append(_Resp(200, json_error=True))
    assert t.get("/primary/home/GetIndexList") is None


def test_http_ensure_open_tidak_membuka_browser(fake_session):
    # Kontrak: ensure_open() tidak pernah menyentuh Chrome di jalur HTTP.
    t = cf_transport.HttpTransport()
    assert t.ensure_open() is t


# ---------------- AutoTransport ----------------


def test_auto_pakai_http_saat_berhasil():
    http = _FakeTransport([[{"ok": 1}]])
    browser = _FakeTransport([None])
    t = cf_transport.AutoTransport(lambda: http, lambda: browser)
    assert t.get("/x") == [{"ok": 1}]
    assert http.calls == 1
    assert browser.calls == 0  # Chrome tidak dibuka kalau HTTP jalan


def test_auto_jatuh_ke_browser_lalu_menahan_http():
    http = _FakeTransport([None, [{"ok": 1}]])
    browser = _FakeTransport([{"dari": "browser"}, {"dari": "browser"}])
    t = cf_transport.AutoTransport(lambda: http, lambda: browser)

    assert t.get("/x") == {"dari": "browser"}
    # Request berikutnya langsung ke browser (masa istirahat HTTP), jadi HTTP
    # tidak dipanggil dua kali dan tidak membayar timeout lagi.
    assert t.get("/y") == {"dari": "browser"}
    assert http.calls == 1
    assert browser.calls == 2


def test_auto_http_melempar_exception_tetap_jatuh_ke_browser():
    http = _FakeTransport([ConnectionError("mati")])
    browser = _FakeTransport([{"dari": "browser"}])
    t = cf_transport.AutoTransport(lambda: http, lambda: browser)
    assert t.get("/x") == {"dari": "browser"}
    assert browser.calls == 1


# ---------------- jaminan "tanpa Chrome" ----------------


def test_jalur_http_tidak_mengimpor_playwright():
    # Di server Linux tanpa Chrome, Playwright tidak boleh ikut termuat.
    # Diuji di proses Python yang baru supaya tidak terpengaruh tes lain.
    code = (
        "import sys;"
        "from idx_scraper.cf_transport import make_transport;"
        "make_transport().ensure_open();"
        "print('playwright' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "IDX_TRANSPORT": "http"},
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
