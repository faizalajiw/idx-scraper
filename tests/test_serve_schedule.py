"""Kunci jadwal job ``idx serve`` supaya revisi jam tidak lolos diam-diam.

Kenapa ada: jam job ini pernah diubah manual (mis. EOD 16:05 -> 16:10) dan
tidak ada yang menangkap kalau ada job tertinggal di jam lama, kalau
``health_eod`` dipindah ke sebelum ``IDX_EOD_DEADLINE_HOUR`` sehingga alarmnya
tidak pernah berbunyi, atau kalau alarm ditaruh sebelum jaring EOD sore sempat
bekerja (alarm palsu tiap kali retry menyelamatkan hari).

Tes ini membangun scheduler sungguhan lewat ``cmd_serve``, tapi mengganti
seluruh efek sampingnya (kunci satu-instance, IDXClient, DB, live capture,
Telegram) dengan stub — jadi tidak menyentuh jaringan, DB, maupun Chrome.
"""

from __future__ import annotations

import pytest

from idx_scraper import cli, health


class _StubScheduler:
    """Menangkap panggilan ``add_job`` tanpa pernah menjalankannya."""

    def __init__(self, *args, **kwargs) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func=None, trigger=None, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        """No-op: scheduler asli memblokir proses selamanya."""

    def shutdown(self) -> None:
        pass


class _StubClient:
    def close(self) -> None:
        pass


class _StubStorage:
    def close(self) -> None:
        pass


class _StubLiveCapture:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


class _StubNotifier:
    enabled = False

    def send_message(self, *args, **kwargs) -> None:  # pragma: no cover
        pass


@pytest.fixture()
def jobs(monkeypatch) -> list[dict]:
    """Jalankan ``cmd_serve`` dengan semua efek samping distub."""
    captured: list[dict] = []

    class _Recorder(_StubScheduler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            captured.append(self)

    monkeypatch.setattr(cli, "BlockingScheduler", _Recorder)
    monkeypatch.setattr(cli, "_take_single_instance_lock", lambda: True)
    monkeypatch.setattr(cli, "IDXClient", _StubClient)
    monkeypatch.setattr(cli, "get_storage_from_env", lambda: _StubStorage())
    monkeypatch.setattr(cli, "show_signals", lambda *a, **k: None)
    monkeypatch.setattr(cli, "LiveCapture", _StubLiveCapture)
    monkeypatch.setattr(cli, "TelegramNotifier", _StubNotifier)
    # Startup catchup juga jalan di sini: paksa "sudah lengkap" supaya tes tidak
    # pernah menyentuh DB dan tidak tergantung jam berapa suite dijalankan.
    monkeypatch.setattr(cli, "_eod_fresh_for", lambda day: (True, True))
    monkeypatch.setenv("IDX_WATCHLIST", "BBCA,BBRI")

    cli.cmd_serve(None)
    return captured[0].jobs


_HARI = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _jam_aktif(hour) -> set[int]:
    """Nilai ``hour`` APScheduler (``16`` / ``"16-20"`` / ``"8,12"``) -> himpunan jam."""
    if isinstance(hour, int):
        return {hour}
    out: set[int] = set()
    for bagian in str(hour).split(","):
        if "-" in bagian:
            awal, akhir = bagian.split("-")
            out |= set(range(int(awal), int(akhir) + 1))
        else:
            out.add(int(bagian))
    return out


def _menit(hour, minute: int) -> int:
    """Menit-dalam-hari dari jam pertama (untuk ``hour`` berupa rentang)."""
    return min(_jam_aktif(hour)) * 60 + minute


def _hari_aktif(expr: str) -> set[int]:
    """Ekspresi APScheduler (``mon-thu`` / ``fri`` / ``sat,sun``) -> indeks hari."""
    if "-" in expr:
        awal, akhir = expr.split("-")
        return set(range(_HARI[awal], _HARI[akhir] + 1))
    return {_HARI[bagian] for bagian in expr.split(",")}


def _cron(jobs: list[dict]) -> dict[str, tuple]:
    return {
        j["id"]: (j.get("day_of_week"), j.get("hour"), j.get("minute"))
        for j in jobs
        if j.get("trigger") == "cron"
    }


def test_jadwal_cron_lengkap(jobs):
    """Peta id -> (hari, jam, menit) harus persis seperti yang disepakati.

    Urutan sore 16:10 -> 16:30 -> 16:50 -> 17:00 -> 17:30 -> 17:45 -> 17:55 ->
    18:10 -> 19:20 sengaja satu arah: rantai EOD, jaring kedua (diulang), IC,
    snapshot ownership, alarm EOD SESUDAH tenggat, lalu buku klaim (tulis klaim
    SEBELUM hasilnya diketahui, isi hasil, ingatkan). Tenggat EOD 17:30 ada di
    belakang jendela terbit IDX - lihat
    ``test_tenggat_eod_di_belakang_jam_terbit_terukur``.
    Jaring kedua diulang tiap jam 16:30-20:30 (lihat
    ``test_jaring_eod_diulang_sampai_malam``), dan penulisan klaim diulang tiap
    jam 17:55-21:55 sampai datanya masuk.
    """
    assert _cron(jobs) == {
        "refresh_preopen": ("mon-fri", 8, 50),
        "refresh_s1": ("mon-thu", 12, 0),
        "refresh_s1_fri": ("fri", 11, 30),
        "refresh_s2": ("mon-fri", 16, 10),
        "eod_fallback": ("mon-fri", "16-20", 30),
        "health_eod": ("mon-fri", 17, 45),
        "daily_ic": ("mon-fri", 16, 50),
        "journal_claims": ("mon-fri", "17-21", 55),
        "journal_outcomes": ("mon-fri", 18, 10),
        "journal_reminder": ("mon-fri", 19, 20),
        "refresh_weekend_am": ("sat,sun", 8, 45),
        "refresh_weekend_pm": ("sat,sun", 16, 0),
    }


def test_job_susulan_tanpa_syarat_tetap_dihapus(jobs):
    """``refresh_s2_late`` (16:20, selalu jalan) tidak boleh kembali.

    Penggantinya ``eod_fallback`` bersyarat: keluar ~1 detik kalau 16:10 sudah
    berhasil, dan menyalakan pipeline penuh hanya kalau belum. Job yang selalu
    jalan berarti dua pipeline penuh tiap sore — persis yang dihindari.
    """
    ids = {j["id"] for j in jobs}
    assert "refresh_s2_late" not in ids
    assert "eod_fallback" in ids


def test_health_eod_tidak_sebelum_deadline(jobs):
    """Alarm hanya berbunyi setelah tenggat, bukan cuma setelah jamnya.

    Kalau job ini dijadwalkan lebih awal dari tenggat, ``evaluate_health``
    mengembalikan ``overdue=False`` -> cek EOD jadi no-op tanpa suara. Tenggatnya
    presisi menit (17:30), jadi perbandingannya menit-dalam-hari - cek jam saja
    akan meloloskan alarm 17:00 yang tidak pernah bisa berbunyi.
    """
    jam, menit = _cron(jobs)["health_eod"][1:]
    tenggat = health.EOD_DEADLINE_HOUR * 60 + health.EOD_DEADLINE_MINUTE
    assert jam * 60 + menit >= tenggat


def test_tenggat_eod_di_belakang_jam_terbit_terukur(jobs):
    """Tenggat harus di belakang jam terbit IDX, bukan di depannya.

    Terukur di ``_serve.log`` (dua sesi dengan bukti detik): ringkasan EOD per
    saham IDX baru terbit 17:13:32 (16 Okt) dan 17:16:16 (19 Okt); percobaan
    16:10, 16:14, 16:30, 16:34 - dan 17:15:00 tepat - semuanya dibalas 0 baris.
    Dengan tenggat 16:00, alarm 16:45 berbunyi "EOD TELAT" 6 dari 6 hari bursa
    sementara datanya belum ada di mana pun. Alarm yang selalu berbunyi tidak
    lagi membawa informasi apa pun.
    """
    tenggat = health.EOD_DEADLINE_HOUR * 60 + health.EOD_DEADLINE_MINUTE
    assert tenggat > 17 * 60 + 16  # di belakang terbit terukur terakhir
    jam, menit = _cron(jobs)["health_eod"][1:]
    assert jam * 60 + menit >= tenggat  # alarm menyusul setelah tenggat


def test_jaring_eod_setelah_rantai_dan_sebelum_alarm(jobs):
    """Fallback (16:30) sesudah rantai, alarm (17:45) sesudah fallback.

    Rantai 16:10 terpanjang ~12 menit (retry 60+180 detik di dua langkah), jadi
    fallback butuh jarak >12 menit supaya tidak bertemu rantai. Alarm harus
    sesudah fallback: kalau tidak, hari yang berhasil diselamatkan jaring kedua
    tetap memicu Telegram "EOD TELAT" - alarm palsu yang lama-lama diabaikan.
    """
    cron = _cron(jobs)
    rantai = _menit(cron["refresh_s2"][1], cron["refresh_s2"][2])
    fallback = _menit(cron["eod_fallback"][1], cron["eod_fallback"][2])
    alarm = _menit(cron["health_eod"][1], cron["health_eod"][2])
    assert fallback - rantai > 12
    assert alarm > fallback


def test_jaring_eod_diulang_sampai_malam(jobs):
    """Jaring kedua bukan sekali jalan: laptop tidur bisa melewati 16:10-16:30.

    Proses ``serve`` tidak mati saat mesin tidur, ia ikut beku — jadi slot yang
    terlewat harus bisa dikerjakan begitu mesin bangun. Karena itu jaring kedua
    diulang tiap jam sampai 20:30 dan `misfire_grace_time` 2 jam membuat cek
    terakhir (20:30) masih menyala kalau mesin bangun sebelum ~22:30. Tanpa ini,
    laptop yang tidur sore kehilangan EOD sehari tanpa alarm apa pun.
    """
    j = next(x for x in jobs if x["id"] == "eod_fallback")
    assert _jam_aktif(j["hour"]) == {16, 17, 18, 19, 20}
    assert j["minute"] == 30
    assert j["misfire_grace_time"] >= 7200
    assert j["coalesce"] is True


def test_alarm_health_menyusul_setelah_mesin_bangun(jobs):
    """Alarm 17:45 juga harus menyusul, bukan dibuang diam-diam saat mesin tidur.

    Tanpa `misfire_grace_time`, laptop yang tidur melewati 17:45 melewatkan
    alarm hari itu sepenuhnya — padahal sesi yang telat masih bisa terlihat
    sebelum jendela tutup, dan inilah satu-satunya jalur yang berbicara ke
    Telegram.
    """
    j = next(x for x in jobs if x["id"] == "health_eod")
    assert j["misfire_grace_time"] >= 7200
    assert j["coalesce"] is True


def test_buku_klaim_sesudah_jendela_terbit(jobs):
    """Tulis klaim bergerak ke belakang jendela terbit, bukan lagi 17:05.

    Job 17:05 (jadwal lama) selalu menulis sesi *kemarin* - EOD sesi hari ini
    baru masuk lewat jaring kedua 17:30 - lalu pengingat 17:50 menemukan sesi
    hari ini bisu dan mengirim "ada job yang gagal tanpa bersuara" tiap hari
    bursa. Ketiganya digeser: tulis setelah tenggat, isi hasil setelah tulis,
    dan pengingat hanya setelah dua kesempatan tulis.
    """
    cron = _cron(jobs)
    tenggat = health.EOD_DEADLINE_HOUR * 60 + health.EOD_DEADLINE_MINUTE
    tulis = _menit(cron["journal_claims"][1], cron["journal_claims"][2])
    hasil = _menit(cron["journal_outcomes"][1], cron["journal_outcomes"][2])
    ingat = _menit(cron["journal_reminder"][1], cron["journal_reminder"][2])
    assert tulis > tenggat
    assert hasil > tulis
    slot_tulis = sorted(
        jam * 60 + cron["journal_claims"][2] for jam in _jam_aktif(cron["journal_claims"][1])
    )
    assert len([s for s in slot_tulis if s < ingat]) >= 2


def test_tidak_ada_job_cron_bertabrakan(jobs):
    """Dua job cron tidak boleh jatuh di menit yang sama pada hari yang sama.

    Job interval (``idx``/``wl``/``news_refresh``) memang hidup terus dan tidak
    bisa dihindari; yang dijaga di sini adalah slot berat. Slot yang bertabrakan
    berarti dua klien IDX berebut sumber yang sama — dan kalau transport jatuh
    ke Chrome, berebut profil yang sama — penyebab klasik EOD 0 baris.
    """
    slot: dict[tuple[int, int], list[str]] = {}
    for j in jobs:
        if j.get("trigger") != "cron":
            continue
        for hari in _hari_aktif(j.get("day_of_week") or "mon-fri"):
            for jam in _jam_aktif(j["hour"]):
                slot.setdefault((hari, _menit(jam, j["minute"])), []).append(j["id"])
    bentrok = {k: v for k, v in slot.items() if len(v) > 1}
    assert bentrok == {}


def test_job_interval_inti_terdaftar(jobs):
    interval = {j["id"]: j.get("seconds") for j in jobs if j.get("trigger") == "interval"}
    assert interval["idx"] == 60
    assert interval["wl"] == 60
    assert interval["news_refresh"] == 900
    # Telegram OFF (stub) -> job alert sengaja tidak didaftarkan.
    assert "alerts" not in interval


def test_snapshot_ownership_senin_sore(jobs):
    ownership = next(j for j in jobs if j["id"] == "ownership_weekly")
    assert ownership["weeks"] == 1
    assert ownership["start_date"].weekday() == 0  # Senin
    assert (ownership["start_date"].hour, ownership["start_date"].minute) == (17, 0)


def test_ownership_berjarak_lebar_dari_rantai_eod(jobs):
    """``ownership`` membuka klien IDX sendiri — beri jarak dari ``refresh_s2``.

    ``daily_ic`` dan ``health_eod`` murni DB (tidak menyentuh IDX), jadi boleh
    berdekatan. ``ownership`` tidak: kalau IDX menolak jalur HTTP dan transport
    jatuh ke Chrome, dua klien bersamaan berebut profil yang sama dan keduanya
    gagal. Rantai 16:10 terukur ~4,6 menit dan bisa molor ke ~12 menit lewat
    retry, jadi jaraknya dijaga >=30 menit (17:00 = 50 menit).
    """
    cron = _cron(jobs)
    eod_start = _menit(cron["refresh_s2"][1], cron["refresh_s2"][2])
    ownership = next(j for j in jobs if j["id"] == "ownership_weekly")
    own_start = _menit(ownership["start_date"].hour, ownership["start_date"].minute)
    assert own_start - eod_start >= 30


def test_pra_buka_sesudah_launcher_dan_sebelum_open(jobs):
    """Pra-buka harus SESUDAH task Windows menyalakan serve, dan sebelum 09:00.

    Di hari `serve` baru dinyalakan (mis. reboot pagi), prosesnya lahir dari task
    IDX-Serve/API/Web pukul 08:45. Jadwal yang lebih awal dari launcher sudah
    lewat saat APScheduler menghitung jadwal pertamanya, jadi ia melompat ke
    besok - recompute pra-buka hilang sehari tanpa alarm apa pun. 09:00 batas
    atasnya: pra-buka harus selesai sebelum pasar buka.
    """
    jam, menit = _cron(jobs)["refresh_preopen"][1:]
    assert (jam, menit) > (8, 45)  # launcher Windows: IDX-Serve/API/Web 08:45
    assert (jam, menit) < (9, 0)  # pasar buka 09:00 WIB
