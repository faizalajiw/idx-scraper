"""Kepemilikan emiten & aksi pemilik — dari keterbukaan IDX (gratis).

IDX ``/primary/ListedCompany/GetCompanyProfilesDetail`` mengembalikan
``PemegangSaham`` per emiten: nama pemilik, kategori (``Lebih dari 5%``,
``Masyarakat Warkat``/``Non Warkat``, ``Direksi``, ``Komisaris``,
``Saham Treasury``), jumlah lembar, persen, dan flag ``Pengendali``.

Modul pure (tanpa DB): parse payload, hitung **free float** publik, dan
bandingkan dua snapshot untuk menemukan **aksi pemilik** — siapa menambah /
mengurangi / masuk / keluar. Ini jawaban gratis untuk "siapa pemain besar di
emiten ini, dan apa yang mereka lakukan" (IDX tidak menyediakan tape broker
per saham; lihat catatan di README/AUDIT).

**Snapshot, bukan deret waktu**: IDX hanya memberi komposisi terkini, jadi
"aksi pemilik" muncul setelah ada dua snapshot di tanggal berbeda.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Kategori IDX yang dianggap pemegang publik -> dipakai untuk free float.
PUBLIC_CATEGORIES = frozenset({"Masyarakat Warkat", "Masyarakat Non Warkat"})

#: Ambang perubahan persen agar dianggap "aksi" (mengabaikan pembulatan).
DEFAULT_MIN_DELTA_PCT = 0.05


def _f(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def classify(category: str | None) -> str:
    """Kategori kasar untuk UI: publik / pengendali / manajemen / treasury / lain."""
    c = (category or "").strip()
    if c in PUBLIC_CATEGORIES:
        return "publik"
    if c == "Saham Treasury":
        return "treasury"
    if c in {"Direksi", "Komisaris"}:
        return "manajemen"
    if c.startswith("Lebih dari") or "5%" in c:
        return "besar"
    return "lain"


def parse_shareholders(profile: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """``PemegangSaham`` -> baris bersih, urut persen menurun.

    Baris tanpa nama dilewati. Field hilang -> None (bukan 0), supaya "tidak
    ada data" beda dari "memang nol". Tidak pernah raise.
    """
    if not isinstance(profile, Mapping):
        return []
    raw = profile.get("PemegangSaham")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("Nama")
        if name is None or not str(name).strip():
            continue
        out.append(
            {
                "holder_name": str(name).strip(),
                "category": (str(item.get("Kategori")).strip() if item.get("Kategori") else None),
                "shares": _f(item.get("Jumlah")),
                "pct": _f(item.get("Persentase")),
                "is_controller": bool(item.get("Pengendali")),
            }
        )
    out.sort(key=lambda r: (r["pct"] if r["pct"] is not None else -1.0), reverse=True)
    return out


def free_float_pct(holders: Iterable[Mapping[str, Any]]) -> float | None:
    """Free float = jumlah persen kategori publik (Warkat + Non Warkat).

    Definisi Indonesia yang lazim: saham di tangan masyarakat (bukan
    pengendali/afiliasi/treasury). None bila tak ada baris publik (tak bisa
    dihitung, jangan mengarang 0).
    """
    total = 0.0
    found = False
    for h in holders:
        if classify(h.get("category")) == "publik":
            p = _f(h.get("pct"))
            if p is not None:
                total += p
                found = True
    return round(total, 3) if found else None


def controller_names(holders: Iterable[Mapping[str, Any]]) -> list[str]:
    """Nama pemegang saham yang ditandai IDX sebagai pengendali."""
    return [str(h["holder_name"]) for h in holders if h.get("is_controller")]


def _named_only(holders: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Indeks nama -> baris, hanya pemilik bernama (kecuali agregat masyarakat
    & treasury — yang bergerak di situ bukan 'aksi pemilik' tertentu)."""
    out: dict[str, Mapping[str, Any]] = {}
    for h in holders:
        if classify(h.get("category")) in {"publik", "treasury"}:
            continue
        out[str(h["holder_name"])] = h
    return out


def owner_changes(
    prev: Iterable[Mapping[str, Any]],
    curr: Iterable[Mapping[str, Any]],
    min_delta_pct: float = DEFAULT_MIN_DELTA_PCT,
) -> list[dict[str, Any]]:
    """Bandingkan dua snapshot pemilik -> daftar aksi (tambah/kurang/baru/keluar).

    Hanya pemilik **bernama** yang dibandingkan (agregat masyarakat & treasury
    dikecualikan). Perubahan di bawah ``min_delta_pct`` poin persen diabaikan.
    Urut menurun berdasar |delta|. Tidak pernah raise.
    """
    p = _named_only(prev)
    c = _named_only(curr)
    out: list[dict[str, Any]] = []
    for name in set(p) | set(c):
        pr, cu = p.get(name), c.get(name)
        prev_pct = _f(pr.get("pct")) if pr else None
        curr_pct = _f(cu.get("pct")) if cu else None
        delta = (
            (curr_pct or 0.0) - (prev_pct or 0.0)
            if (prev_pct is not None or curr_pct is not None)
            else None
        )
        if pr is None and cu is not None:
            if curr_pct is not None and curr_pct < min_delta_pct:
                continue
            action = "baru"
        elif cu is None and pr is not None:
            if prev_pct is not None and prev_pct < min_delta_pct:
                continue
            action = "keluar"
        else:
            if delta is None or abs(delta) < min_delta_pct:
                continue
            action = "tambah" if delta > 0 else "kurang"
        out.append(
            {
                "holder_name": name,
                "category": (cu or pr or {}).get("category"),
                "prev_pct": prev_pct,
                "curr_pct": curr_pct,
                "delta_pct": round(delta, 4) if delta is not None else None,
                "action": action,
            }
        )
    out.sort(key=lambda r: abs(r["delta_pct"] or 0.0), reverse=True)
    return out
