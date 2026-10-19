"""Unit tests for idx_scraper.research.journal_store (buku klaim di DB).

Tidak menyentuh Postgres: cursor palsu dengan tabel in-memory, plus panel harga
buatan untuk bagian pengisian hasil (bagian murni `fill_outcomes_from_frames`).

Yang dikunci di sini adalah janji yang membuat buku ini bernilai: baris yang
sudah tertulis tidak bisa ditimpa, "tidak ada klaim" selalu punya sebab yang
tercatat, hari yang belum teramati tetap terbuka (bukan kedaluwarsa), dan klaim
yang belum matang tidak dihitung sebagai hasil.
"""

from __future__ import annotations

import pandas as pd
import pytest

from idx_scraper.research import journal_store as toko
from idx_scraper.research.journal import (
    CLAIM_COLUMNS,
    RULE_PAPAN,
    STATUS_EXPIRED,
    STATUS_OPEN,
    STATUS_STOP,
    STATUS_TARGET,
    claim_gate,
)
from idx_scraper.research.signal_log import NLAGS_DEFAULT

#: Sepuluh sesi bursa (Senin sampai Jumat). Cukup panjang untuk horizon 3 dengan
#: jeda ``nlags`` sesi sebelum jendela ukur dimulai, seperti harness P3.
_SESI = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-10-15", periods=10)]

#: Sesi pertama yang benar-benar dilihat ``claim_eval`` untuk sebuah klaim:
#: entry di close T+nlags, jendela sentuhan mulai T+nlags+1.
_TOUCH = _SESI[NLAGS_DEFAULT + 1]


class _FakeCursor:
    """Cursor minim dengan tabel in-memory.

    Meniru ``insert ... on conflict do nothing`` (baris lama menang, jumlah masuk
    lewat ``rowcount``) dan menjawab query baca dengan baris yang disemai. Bukan
    Postgres: WHERE tidak ditafsirkan, kecuali penanda yang memang dipakai
    modulnya (``status = 'terbuka'`` dan filter daftar tanggal).
    """

    def __init__(self, *, klaim=None, penolakan=None, sesi=None):
        self.klaim = [dict(k) for k in (klaim or [])]
        self.penolakan = [dict(p) for p in (penolakan or [])]
        self.sesi = list(sesi or [])
        self.rowcount = 0
        self.queries: list[str] = []
        self._out: list[dict] = []

    def execute(self, query, params=None):
        q = " ".join(str(query).split()).lower()
        self.queries.append(q)
        self.rowcount = 0
        self._out = []

        if q.startswith("insert into research.claim_journal"):
            baris = dict(zip(CLAIM_COLUMNS, params))
            kunci = (str(baris["code"]), str(baris["trade_date"])[:10], baris["rule"])
            ada = {
                (str(k["code"]), str(k["trade_date"])[:10], k["rule"]) for k in self.klaim
            }
            # `rowcount` mengikuti janji SQL-nya: tanpa `on conflict ... do nothing`,
            # Postgres akan menimpa, dan tesnya harus ikut gagal.
            if "do nothing" not in q or kunci not in ada:
                self.klaim.append(baris)
                self.rowcount = 1
        elif q.startswith("insert into research.claim_refusal"):
            tanggal, sebab, detail = params
            kunci = (str(tanggal)[:10], sebab)
            ada = {(str(p["trade_date"])[:10], p["reason"]) for p in self.penolakan}
            if "do nothing" not in q or kunci not in ada:
                self.penolakan.append(
                    {"trade_date": tanggal, "reason": sebab, "detail": detail}
                )
                self.rowcount = 1
        elif "from research.claim_journal" in q:
            baris = list(self.klaim)
            if "status = 'terbuka'" in q:
                baris = [k for k in baris if k["status"] == STATUS_OPEN]
            if params and "any(%s)" in q:
                ingin = {str(t)[:10] for t in params[0]}
                baris = [k for k in baris if str(k["trade_date"])[:10] in ingin]
            self._out = baris
        elif "from research.claim_refusal" in q:
            baris = list(self.penolakan)
            if params and "any(%s)" in q:
                ingin = {str(t)[:10] for t in params[0]}
                baris = [k for k in baris if str(k["trade_date"])[:10] in ingin]
            self._out = baris
        elif "from research.raw_eod" in q:
            batas = int(params[0]) if params else len(self.sesi)
            self._out = [{"trade_date": t} for t in self.sesi[:batas]]

    def fetchall(self):
        return list(self._out)

    def fetchone(self):
        return self._out[0] if self._out else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- pembantu tes ------------------------------------------------------ #
    def baris(self, kolom: str, nilai: str):
        return [r for r in self.klaim if str(r.get(kolom)) == nilai]


def _klaim(**ubah) -> dict:
    klaim = {
        "code": "PEGE",
        "trade_date": "2026-10-15",
        "rule": RULE_PAPAN,
        "direction": "BUY",
        "grade": "A",
        "ref_raw": 113.42,
        "stop_raw": 106.33,
        "target_raw": 127.60,
        "ref_adj": 112.98,
        "horizon_days": 21,
        "size_pct": 16.0,
        "size_lots": None,
        "size_rupiah": None,
        "regime": None,
        "vol_state": None,
        "freshness_status": "segar",
        "status": STATUS_OPEN,
        "outcome_abn": None,
        "touch_date": None,
    }
    klaim.update(ubah)
    return klaim


def _kandidat(**ubah) -> dict:
    kandidat = {
        "code": "PEGE",
        "trade_date": "2026-10-15",
        "grade": "A",
        "score": 56.7,
        "entry_ref": 113.42,
        "stop": 106.33,
        "target": 127.60,
        "horizon_days": 21,
        "ref_adj": 112.98,
        "size_pct": 16.0,
    }
    kandidat.update(ubah)
    return kandidat


def _raw(extra=None, code: str = "PEGE") -> pd.DataFrame:
    """Panel mentah tenang (tidak menyentuh level), dengan pengecualian opsional."""
    baris = {d: (112.0, 108.0, 110.0) for d in _SESI}
    hilang: set[str] = set()
    for item in extra or []:
        if item[0] is None:
            hilang.add(item[1])
            continue
        d, h, lo, c = item
        baris[d] = (h, lo, c)
    return pd.DataFrame(
        [
            {"code": code, "date": d, "high": h, "low": lo, "close": c}
            for d, (h, lo, c) in baris.items()
            if d not in hilang
        ]
    )


def _adj(sejak_touch: float | None = None) -> pd.DataFrame:
    """Panel terkoreksi: PEGE sebagai emiten uji + dua emiten pasar yang datar.

    Pembanding abnormal milik harness P3 adalah rata-rata panel itu sendiri, jadi
    tanpa emiten pasar tambahan abnormalnya akan selalu nol. PEGE rata di 112,98
    sampai sesi *sebelum* jendela ukur, lalu pindah ke ``sejak_touch``.
    """
    tutup = {d: 112.98 for d in _SESI}
    if sejak_touch is not None:
        for d in _SESI[NLAGS_DEFAULT + 1 :]:
            tutup[d] = sejak_touch
    baris = [{"code": "PEGE", "date": d, "close": c} for d, c in tutup.items()]
    for code in ("PASAR1", "PASAR2"):
        baris += [{"code": code, "date": d, "close": 100.0} for d in _SESI]
    return pd.DataFrame(baris)


def test_rekam_ulang_tidak_menimpa() -> None:
    """Percobaan kedua dengan angka berbeda masuk 0 dan tidak mengubah baris lama."""
    cur = _FakeCursor()
    assert toko.record_claims(cur, [_klaim()]) == 1
    assert toko.record_claims(cur, [_klaim(stop_raw=100.0, target_raw=130.0)]) == 0
    assert len(cur.klaim) == 1
    assert float(cur.klaim[0]["stop_raw"]) == pytest.approx(106.33)


def test_kandidat_lengkap_ditulis_dengan_ukuran_dan_harga_terkoreksi(monkeypatch) -> None:
    cur = _FakeCursor()
    monkeypatch.setattr(toko, "candidates_for", lambda *a, **k: [_kandidat()])
    out = toko.write_session("dsn", cur, trade_date="2026-10-15")
    assert out == {"written": 1, "refused": 0, "reason": None}
    baris = cur.klaim[0]
    assert baris["size_pct"] == pytest.approx(16.0)
    assert baris["ref_adj"] == pytest.approx(112.98)
    assert baris["status"] == STATUS_OPEN


def test_kolam_kosong_dicatat_sebagai_penolakan(monkeypatch) -> None:
    """Kolam kosong: tidak ada klaim, tetapi sesinya tetap bisa dijelaskan."""
    cur = _FakeCursor()
    monkeypatch.setattr(toko, "candidates_for", lambda *a, **k: [])
    monkeypatch.setattr(toko, "pool_counts", lambda *a, **k: (55, 0))
    out = toko.write_session("dsn", cur, trade_date="2026-10-15")
    assert out["written"] == 0
    assert out["reason"] == "kolam_kosong"
    assert cur.penolakan[0]["reason"] == "kolam_kosong"
    assert "55" in cur.penolakan[0]["detail"]


def test_gerbang_menolak_maka_tidak_ada_klaim_dan_sebabnya_tercatat(monkeypatch) -> None:
    cur = _FakeCursor()
    monkeypatch.setattr(toko, "candidates_for", lambda *a, **k: [_kandidat()])
    out = toko.write_session(
        "dsn", cur, trade_date="2026-10-15", gate=claim_gate(freshness_status="basi")
    )
    assert out["written"] == 0
    assert cur.klaim == []
    assert cur.penolakan[0]["reason"] == "data"


def test_kandidat_tak_lengkap_ditolak_dengan_sebabnya(monkeypatch) -> None:
    """Kandidat tanpa stop tidak masuk buku, dan sebabnya tercatat per nama."""
    cur = _FakeCursor()
    monkeypatch.setattr(
        toko, "candidates_for", lambda *a, **k: [_kandidat(code="XX", stop=None)]
    )
    out = toko.write_session("dsn", cur, trade_date="2026-10-15")
    assert out["written"] == 0
    assert cur.klaim == []
    assert cur.penolakan[0]["reason"] == "tanpa_stop"
    assert "XX" in cur.penolakan[0]["detail"]


def test_bar_hilang_tidak_dianggap_kedaluwarsa() -> None:
    """Bar mentah hilang di tengah jendela: klaim tetap terbuka, hasilnya kosong."""
    baris = toko.fill_outcomes_from_frames(
        [_klaim(horizon_days=3)],
        raw=_raw([(None, _TOUCH)]),
        adj=_adj(),
    )
    assert len(baris) == 1
    assert baris[0]["status"] == STATUS_OPEN
    assert baris[0]["outcome_abn"] is None


def test_hasil_jelek_ditulis_apa_adanya() -> None:
    baris = toko.fill_outcomes_from_frames(
        [_klaim(horizon_days=1)],
        raw=_raw([(_TOUCH, 110.0, 100.0, 104.0)]),
        adj=_adj(sejak_touch=104.0),
    )
    assert baris[0]["status"] == STATUS_STOP
    assert baris[0]["outcome_abn"] < 0
    assert baris[0]["touch_date"] == _TOUCH


def test_sentuhan_target_ditulis_kena_target() -> None:
    baris = toko.fill_outcomes_from_frames(
        [_klaim(horizon_days=1)],
        raw=_raw([(_TOUCH, 128.0, 110.0, 126.0)]),
        adj=_adj(sejak_touch=126.0),
    )
    assert baris[0]["status"] == STATUS_TARGET
    assert baris[0]["outcome_abn"] > 0


def test_klaim_yang_sudah_selesai_tidak_diperiksa_ulang() -> None:
    cur = _FakeCursor(klaim=[_klaim(status=STATUS_TARGET, outcome_abn=0.05)])
    assert toko.fill_outcomes(cur) == {
        "diperiksa": 0,
        "kena_target": 0,
        "kena_stop": 0,
        "kedaluwarsa": 0,
        "terbuka": 0,
    }


def test_ringkasan_memakai_kalimat_rekaman_yang_sudah_ada() -> None:
    cur = _FakeCursor()
    ringkas = toko.journal_summary(cur, rule=RULE_PAPAN)
    assert ringkas["claims"] == 0
    assert ringkas["record_line"] == "Aturan ini: belum punya rekaman"
    assert ringkas["verdict"]["status"] == "belum cukup"


def test_ringkasan_klaim_terbuka_bukan_klaim_terukur() -> None:
    """Satu klaim terbuka: barisnya ada, tapi verdict belum boleh menghitungnya."""
    cur = _FakeCursor(klaim=[_klaim()])
    ringkas = toko.journal_summary(cur)
    assert ringkas["claims"] == 1
    assert ringkas["open"] == 1
    assert ringkas["verdict"]["measured_claims"] == 0
    assert ringkas["verdict"]["mean_abnormal"] is None


def test_ringkasan_menghitung_hasil_yang_sudah_ada() -> None:
    cur = _FakeCursor(
        klaim=[
            _klaim(status=STATUS_TARGET, outcome_abn=0.04),
            _klaim(code="CAKK", status=STATUS_STOP, outcome_abn=-0.02),
        ]
    )
    ringkas = toko.journal_summary(cur)
    assert ringkas["claims"] == 2
    assert ringkas["sessions"] == 1
    assert ringkas["hit_target"] == 1
    assert ringkas["hit_stop"] == 1
    assert ringkas["verdict"]["measured_claims"] == 2
    assert ringkas["mean_abnormal"] == pytest.approx(0.01)
    assert "2 klaim" in ringkas["record_line"]


def test_sesi_tanpa_klaim_dan_tanpa_penolakan_masuk_daftar_celah() -> None:
    cur = _FakeCursor(sesi=["2026-10-16", "2026-10-15"], klaim=[_klaim()])
    celah = toko.gaps(cur, sessions=5)
    assert [c["trade_date"] for c in celah] == ["2026-10-16"]


def test_sesi_yang_punya_penolakan_tidak_masuk_daftar_celah() -> None:
    """Sesi dengan penolakan sudah bisa dijelaskan; sesi yang benar-benar bisu tetap muncul."""
    cur = _FakeCursor(
        sesi=["2026-10-16", "2026-10-15"],
        penolakan=[{"trade_date": "2026-10-16", "reason": "data", "detail": None}],
    )
    celah = toko.gaps(cur, sessions=5)
    assert [c["trade_date"] for c in celah] == ["2026-10-15"]


def test_klaim_diukur_pada_horizon_klaimnya_sendiri() -> None:
    """Hasil dibaca pada horizon klaim itu, bukan menunggu horizon verdict.

    Klaim 3 sesi yang tidak menyentuh level apa pun selesai sebagai kedaluwarsa
    dan punya angka abnormal 3 sesi, bukan angka kosong sampai sesi ke-21.
    """
    baris = toko.fill_outcomes_from_frames([_klaim(horizon_days=3)], raw=_raw(), adj=_adj())
    assert baris[0]["status"] == STATUS_EXPIRED
    assert baris[0]["outcome_abn"] == pytest.approx(0.0)


def test_fill_outcomes_menulis_status_lewat_kunci_tiga_kolom(monkeypatch) -> None:
    """Badan ``fill_outcomes``: baca klaim terbuka, susun panel, tulis statusnya.

    ``fill_outcomes_from_frames`` (murni) sudah diuji; yang belum pernah dijalankan
    adalah badannya. Kunci ``where code/trade_date/rule`` ikut diuji karena SQL yang
    salah kunci akan menulis status ke baris yang salah tanpa satu pun tes gagal.
    """
    cur = _FakeCursor(klaim=[_klaim(horizon_days=3)])
    monkeypatch.setattr(
        toko, "panel", lambda *a, **k: (_raw([(_TOUCH, 105.0, 90.0, 95.0)]), _adj())
    )
    out = toko.fill_outcomes(cur)
    assert out == {
        "diperiksa": 1,
        "kena_target": 0,
        "kena_stop": 1,
        "kedaluwarsa": 0,
        "terbuka": 0,
    }
    update = [q for q in cur.queries if q.startswith("update research.claim_journal")]
    assert len(update) == 1
    assert "where code = %s and trade_date = %s and rule = %s" in update[0]


def test_penolakan_yang_sama_tidak_menumpuk() -> None:
    """Penolakan = catatan percobaan: sesi dan sebab yang sama tetap satu baris.

    Percobaan kedua jam 18:05 tidak boleh menimpa sebab yang dicatat jam 17:05.
    """
    cur = _FakeCursor()
    toko.record_refusal(cur, trade_date="2026-10-16", reason="regime", detail="pertama")
    toko.record_refusal(cur, trade_date="2026-10-16", reason="regime", detail="kedua")
    assert len(cur.penolakan) == 1
    assert cur.penolakan[0]["detail"] == "pertama"
    assert "on conflict (trade_date, reason) do nothing" in cur.queries[0]


def test_kandidat_geometrinya_salah_menyebut_geometri_bukan_tidak_lengkap(monkeypatch) -> None:
    """Sebab penolakan ditanya ke aturan yang menolaknya, bukan disusun ulang di penulis."""
    cur = _FakeCursor()
    monkeypatch.setattr(toko, "candidates_for", lambda *a, **k: [_kandidat(stop=200.0)])
    out = toko.write_session("dsn", cur, trade_date="2026-10-15")
    assert out["written"] == 0
    assert cur.penolakan[0]["reason"] == "geometri"


def test_kandidat_tanpa_horizon_menyebut_horizon(monkeypatch) -> None:
    cur = _FakeCursor()
    monkeypatch.setattr(toko, "candidates_for", lambda *a, **k: [_kandidat(horizon_days=None)])
    out = toko.write_session("dsn", cur, trade_date="2026-10-15")
    assert out["written"] == 0
    assert cur.penolakan[0]["reason"] == "horizon"


def test_ringkasan_membaca_baris_terbaru_dulu() -> None:
    """Batas baca memotong riwayat lama, bukan kejadian hari ini.

    Dengan urutan menaik, tabel yang sudah lewat ``SUMMARY_LIMIT`` akan menyembunyikan
    klaim terbaru dari agregat dan verdict tanpa satu pun tanda di layar.
    """
    cur = _FakeCursor()
    toko.journal_summary(cur, rule=RULE_PAPAN)
    assert "order by trade_date desc" in cur.queries[0]
