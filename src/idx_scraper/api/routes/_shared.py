"""Helper bersama rute: settings, resolver kode, parser tanggal, dan Router."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException

from ..config import get_settings

settings = get_settings()


def _resolve_codes(codes: str | None) -> list[str]:
    if codes:
        return list(dict.fromkeys(c.strip().upper() for c in codes.split(",") if c.strip()))
    return settings.watchlist


def _parse_optional_date(value: str | None, field: str) -> date | None:
    """Parse a YYYY-MM-DD query/body value, or None when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} harus format YYYY-MM-DD") from None


class Router:
    """Dekorator ditunda: mengumpulkan rute dulu, memasangnya saat register().

    Dipakai alih-alih ``APIRouter`` dengan ``include_router`` karena FastAPI
    0.141 menyisipkan objek ``_IncludedRouter`` (malas, tanpa ``.path``) ke
    ``app.routes``; itu merusak introspeksi daftar rute (kontrak README).
    Pola ini juga menjaga fungsi rute tetap bisa dipanggil langsung di tes.
    """

    def __init__(self) -> None:
        self.rute: list[tuple[str, str, dict[str, Any], Callable[..., Any]]] = []

    def _daftar(
        self, metode: str, path: str, kwargs: dict[str, Any]
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.rute.append((metode, path, kwargs, fn))
            return fn

        return deco

    def get(self, path: str, **kw: Any) -> Callable[..., Any]:
        return self._daftar("get", path, kw)

    def post(self, path: str, **kw: Any) -> Callable[..., Any]:
        return self._daftar("post", path, kw)

    def put(self, path: str, **kw: Any) -> Callable[..., Any]:
        return self._daftar("put", path, kw)

    def delete(self, path: str, **kw: Any) -> Callable[..., Any]:
        return self._daftar("delete", path, kw)

    def register(self, app: FastAPI) -> None:
        """Pasang semua rute ke app, berurutan seperti saat dideklarasikan."""
        for metode, path, kwargs, fn in self.rute:
            getattr(app, metode)(path, **kwargs)(fn)
