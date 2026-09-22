"""
Pengukur delay data IDX (near-real-time).

Tujuan: mengukur SEBERAPA LAMA delay data gratis dibanding waktu sekarang,
dengan cara polling endpoint IDX + Yahoo Finance lalu membandingkan timestamp
yang dikembalikan server terhadap jam lokal (WIB).

Jalankan saat jam bursa (Senin-Jumat, ~09:00-16:00 WIB) supaya angkanya valid.

Contoh:
    python measure_delay.py                 # 1 sampel, lalu keluar
    python measure_delay.py --loop 10 --interval 30   # 10 sampel tiap 30 detik
    python measure_delay.py --loop 20 --interval 15 --out report.json

Catatan:
- Delay diukur dari selisih (now_WIB - timestamp_data). Kalau endpoint tidak
  memberi timestamp yang jelas, kolomnya akan diisi None dan ditandai.
- Ini murni membaca data publik untuk riset pribadi. Rate-limit dijaga sopan.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta

from curl_cffi import requests as cf_requests

WIB = timezone(timedelta(hours=7))

IDX_BASE = "https://www.idx.co.id"


def now_wib() -> datetime:
    return datetime.now(WIB)


@dataclass
class Sample:
    source: str
    field: str  # data apa yang diukur (mis. "COMPOSITE index")
    server_time: str | None  # timestamp dari data (ISO) kalau ada
    local_time: str  # jam saat kita fetch (WIB)
    delay_seconds: float | None  # None kalau tak bisa dihitung
    value: str | None  # nilai data (buat sanity check)
    note: str = ""


def _parse_dt(raw: str) -> datetime | None:
    """Coba beberapa format timestamp yang biasa dipakai IDX/Yahoo."""
    if not raw:
        return None
    raw = str(raw).strip()
    fmts = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d %b %Y %H:%M:%S",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(raw, f)
            return dt.replace(tzinfo=WIB)  # asumsi server IDX = WIB
        except ValueError:
            continue
    # time-only "HH:MM:SS" -> gabung dengan tanggal hari ini (WIB)
    try:
        t = datetime.strptime(raw, "%H:%M:%S").time()
        today = now_wib().date()
        return datetime.combine(today, t, tzinfo=WIB)
    except ValueError:
        pass
    # epoch (detik atau milidetik)
    try:
        num = float(raw)
        if num > 1e12:  # ms
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=WIB)
    except (ValueError, OverflowError):
        return None


class IDXSession:
    """Warm-up cookie IDX lalu fetch endpoint internal.

    IDX diproteksi Cloudflare, jadi kita pakai curl_cffi yang meniru TLS
    fingerprint Chrome supaya tidak kena 403.
    """

    def __init__(self) -> None:
        self.client = cf_requests.Session(impersonate="chrome", timeout=25.0)
        self._warm = False

    def warmup(self) -> None:
        if self._warm:
            return
        try:
            self.client.get(f"{IDX_BASE}/id")
            time.sleep(1.0)
            self.client.get(f"{IDX_BASE}/primary/home/GetIndexList")
            self._warm = True
        except Exception as e:  # curl_cffi raises its own error types
            print(f"[warn] gagal warmup session IDX: {e}", file=sys.stderr)

    def get_json(self, path: str) -> object | None:
        self.warmup()
        try:
            r = self.client.get(f"{IDX_BASE}{path}")
            if r.status_code != 200:
                print(f"[warn] IDX {path} status {r.status_code}", file=sys.stderr)
                return None
            return r.json()
        except Exception as e:
            print(f"[warn] IDX {path} gagal: {e}", file=sys.stderr)
            return None

    def close(self) -> None:
        self.client.close()


def sample_idx_index(sess: IDXSession) -> list[Sample]:
    """GetIndexList: harga index terkini (IHSG dsb)."""
    out: list[Sample] = []
    data = sess.get_json("/primary/home/GetIndexList")
    local = now_wib()
    if not isinstance(data, list):
        out.append(Sample("IDX", "index-list", None, local.isoformat(), None, None,
                           "response bukan list / kosong"))
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        code = item.get("IndexCode", "?")
        if code not in ("COMPOSITE", "LQ45"):
            continue
        raw_ts = item.get("Date") or item.get("LastUpdate") or item.get("Time")
        server_dt = _parse_dt(raw_ts) if raw_ts else None
        delay = (local - server_dt).total_seconds() if server_dt else None
        out.append(Sample(
            source="IDX",
            field=f"index {code}",
            server_time=server_dt.isoformat() if server_dt else None,
            local_time=local.isoformat(),
            delay_seconds=delay,
            value=str(item.get("Current") or item.get("Closing")),
            note="" if server_dt else "endpoint tak beri timestamp jelas",
        ))
    return out


def sample_idx_trading_daily(sess: IDXSession, code: str) -> Sample:
    """GetTradingInfoDaily: snapshot harga 1 emiten."""
    local = now_wib()
    data = sess.get_json(f"/primary/ListedCompany/GetTradingInfoDaily?code={code}")
    if not isinstance(data, dict) or not data.get("SecurityCode"):
        return Sample("IDX", f"trading-daily {code}", None, local.isoformat(), None, None,
                      "kosong / kode tidak ditemukan")
    raw_ts = data.get("Date") or data.get("LastTradingDate") or data.get("Time")
    server_dt = _parse_dt(raw_ts) if raw_ts else None
    delay = (local - server_dt).total_seconds() if server_dt else None
    return Sample(
        source="IDX",
        field=f"trading-daily {code}",
        server_time=server_dt.isoformat() if server_dt else None,
        local_time=local.isoformat(),
        delay_seconds=delay,
        value=str(data.get("LastPrice") or data.get("ClosingPrice")),
        note="" if server_dt else "endpoint tak beri timestamp jelas",
    )


def sample_yahoo(symbol: str = "^JKSE") -> Sample:
    """Yahoo Finance quote via endpoint publik (delay ~15 mnt untuk IDX)."""
    local = now_wib()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    try:
        r = cf_requests.get(url, impersonate="chrome", timeout=20.0)
        if r.status_code != 200:
            return Sample("Yahoo", f"quote {symbol}", None, local.isoformat(), None, None,
                          f"status {r.status_code}")
        payload = r.json()
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        market_time = meta.get("regularMarketTime")
        server_dt = datetime.fromtimestamp(market_time, tz=WIB) if market_time else None
        delay = (local - server_dt).total_seconds() if server_dt else None
        return Sample(
            source="Yahoo",
            field=f"quote {symbol}",
            server_time=server_dt.isoformat() if server_dt else None,
            local_time=local.isoformat(),
            delay_seconds=delay,
            value=str(meta.get("regularMarketPrice")),
        )
    except (KeyError, json.JSONDecodeError, Exception) as e:
        return Sample("Yahoo", f"quote {symbol}", None, local.isoformat(), None, None,
                      f"gagal: {e}")


def collect_once(sess: IDXSession, watchlist: list[str]) -> list[Sample]:
    samples: list[Sample] = []
    samples.extend(sample_idx_index(sess))
    for code in watchlist:
        samples.append(sample_idx_trading_daily(sess, code))
        time.sleep(0.5)  # sopan ke server IDX
    samples.append(sample_yahoo("^JKSE"))
    for code in watchlist:
        samples.append(sample_yahoo(f"{code}.JK"))
        time.sleep(0.3)
    return samples


def print_samples(samples: list[Sample]) -> None:
    print(f"\n=== Snapshot {now_wib().strftime('%H:%M:%S WIB')} ===")
    header = f"{'SOURCE':6} {'FIELD':22} {'DELAY':>10}  {'VALUE':>12}  NOTE"
    print(header)
    print("-" * len(header))
    for s in samples:
        delay_str = f"{s.delay_seconds:,.0f}s" if s.delay_seconds is not None else "n/a"
        val = s.value if s.value not in (None, "None") else "-"
        print(f"{s.source:6} {s.field:22} {delay_str:>10}  {val:>12}  {s.note}")


def summarize(all_samples: list[Sample]) -> None:
    print("\n=== RINGKASAN DELAY ===")
    by_source: dict[str, list[float]] = {}
    for s in all_samples:
        if s.delay_seconds is not None and s.delay_seconds >= 0:
            by_source.setdefault(s.source, []).append(s.delay_seconds)
    if not by_source:
        print("Tidak ada delay yang bisa dihitung (endpoint tak memberi timestamp).")
        print("Yahoo biasanya paling andal untuk pengukuran delay ini.")
        return
    for source, delays in by_source.items():
        med = statistics.median(delays)
        print(f"{source:6} : median {med/60:.1f} menit  "
              f"(min {min(delays)/60:.1f} / max {max(delays)/60:.1f} mnt, n={len(delays)})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ukur delay data IDX gratis")
    ap.add_argument("--loop", type=int, default=1, help="jumlah sampel (default 1)")
    ap.add_argument("--interval", type=int, default=30, help="jeda antar sampel (detik)")
    ap.add_argument("--watchlist", type=str, default="BBCA,BBRI,TLKM",
                    help="ticker dipisah koma")
    ap.add_argument("--out", type=str, default=None, help="simpan hasil ke file JSON")
    args = ap.parse_args()

    watchlist = [c.strip().upper() for c in args.watchlist.split(",") if c.strip()]
    sess = IDXSession()
    all_samples: list[Sample] = []

    try:
        for i in range(args.loop):
            samples = collect_once(sess, watchlist)
            print_samples(samples)
            all_samples.extend(samples)
            if i < args.loop - 1:
                print(f"\n... tunggu {args.interval} detik ...")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[dihentikan user]")
    finally:
        sess.close()

    summarize(all_samples)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in all_samples], f, ensure_ascii=False, indent=2)
        print(f"\nHasil disimpan: {args.out}")


if __name__ == "__main__":
    main()
