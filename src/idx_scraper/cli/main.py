"""Entry point console script `idx = idx_scraper.cli:main`."""

from __future__ import annotations

import argparse
import atexit
import os

import dotenv

from .alerts import cmd_alerts, cmd_health
from .eod import cmd_eod, cmd_refresh, cmd_seed, cmd_signals, cmd_snapshot, cmd_trade_summary
from .fetch import _parse_watchlist, fetch_and_store_watchlist
from .jobs import cmd_ic, cmd_ownership, cmd_recommendations, cmd_signal_log
from .journal import cmd_journal
from .serve import _tutup_pool_api, cmd_serve

dotenv.load_dotenv()


from ..client import (
    IDXClient,
)
from ..logging_setup import say, setup
from ..storage import get_storage_from_env


def main() -> None:
    atexit.register(_tutup_pool_api)
    parser = argparse.ArgumentParser(prog="idx")
    sub = parser.add_subparsers(dest="command")

    p_snap = sub.add_parser("snapshot", help="one-off fetch index + watchlist")
    p_snap.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="scan N most liquid tickers instead of .env watchlist (e.g. --top 200)",
    )
    p_eod = sub.add_parser("eod", help="fetch entire market EOD data (optional: specify YYYYMMDD)")
    p_eod.add_argument("date", nargs="?", help="YYYYMMDD (optional, defaults to today)")
    p_seed = sub.add_parser("seed", help="seed historical data (yfinance) for watchlist tickers")
    p_seed.add_argument(
        "--days", type=int, default=180, help="number of days of historical data (default: 180)"
    )
    sub.add_parser("signals", help="show buy/sell signals from stored EOD data")
    p_alerts = sub.add_parser(
        "alerts", help="kirim alert Telegram utk sinyal BUY/SELL yang berubah"
    )
    p_alerts.add_argument(
        "--test", action="store_true", help="test koneksi bot (getMe) lalu keluar"
    )
    p_ic = sub.add_parser(
        "ic", help="run IC analysis + simpan bobot faktor ke research.factor_ic_history"
    )
    p_ic.add_argument(
        "--k", type=int, nargs="+", default=[5, 10], help="horizon hari bursa (default: 5 10)"
    )
    p_slog = sub.add_parser("signal-log", help="backfill + tampilkan track record sinyal BUY/SELL")
    p_slog.add_argument(
        "--months", type=int, default=18, help="panjang histori (default: 18 bulan)"
    )
    p_slog.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=[5, 10, 21],
        help="horizon hari bursa (default: 5 10 21)",
    )
    p_rec = sub.add_parser(
        "recommendations", help="backfill + tampilkan track record kandidat beli"
    )
    p_rec.add_argument("--months", type=int, default=6, help="panjang histori (default: 6 bulan)")
    p_rec.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=[5, 10, 21],
        help="horizon hari bursa (default: 5 10 21)",
    )
    sub.add_parser("watchlist", help="fetch watchlist tickers (default: from .env)")
    p_ts = sub.add_parser(
        "trade-summary",
        help="agregat pasar reguler vs non-reguler (volume lot + value) per tanggal",
    )
    p_ts.add_argument("--date", help="YYYY-MM-DD (default: tanggal terbaru)")
    p_ts.add_argument(
        "--rebuild",
        action="store_true",
        help="hitung ulang semua tanggal dari research.raw_eod yang tersimpan",
    )
    p_own = sub.add_parser("ownership", help="snapshot komposisi pemegang saham (aksi pemilik)")
    p_own.add_argument("--codes", help="daftar kode dipisah koma (default: watchlist .env)")
    p_own.add_argument(
        "--all", action="store_true", help="semua emiten tercatat (berat, ±950 request)"
    )
    p_own.add_argument("--date", help="tanggal snapshot ISO YYYY-MM-DD (default: hari ini)")
    sub.add_parser(
        "health",
        help="cek data EOD hari bursa terakhir: kesegaran, cakupan kolom, baris per "
        "sesi, lubang sesi (exit 1 kalau ada temuan)",
    )
    sub.add_parser("serve", help="start continuous polling loop")
    p_refresh = sub.add_parser(
        "refresh",
        help="one-shot full refresh pipeline (EOD + recompute + clear cache)",
    )
    p_refresh.add_argument(
        "--skip-if-fresh",
        action="store_true",
        help="lewati bila EOD saham + close indeks hari ini sudah ada",
    )
    p_source = sub.add_parser("source", help="show / set live data source (YAHOO|IDX)")
    p_source.add_argument(
        "set",
        nargs="?",
        choices=["YAHOO", "IDX", "yahoo", "idx"],
        help="force source (optional; omit to just show state)",
    )

    from ..research.journal import RULE_PAPAN

    p_journal = sub.add_parser(
        "journal", help="buku klaim (lapis 4): tulis, isi hasil, ringkas, keadaan, halt"
    )
    jsub = p_journal.add_subparsers(dest="journal_cmd")
    j_rec = jsub.add_parser("record", help="tulis klaim sesi bursa terakhir (idempoten)")
    j_rec.add_argument("--trade-date", help="YYYY-MM-DD (default: sesi bursa terakhir)")
    j_out = jsub.add_parser("outcomes", help="isi hasil klaim yang sudah matang")
    j_out.add_argument("--trade-date", help="YYYY-MM-DD (batasi ke satu sesi)")
    j_sum = jsub.add_parser("summary", help="ringkasan buku + kalimat rekaman aturan")
    j_sum.add_argument("--rule", default=RULE_PAPAN, help=f"aturan (default: {RULE_PAPAN})")
    j_sum.add_argument("--code", help="batasi ke satu emiten")
    jsub.add_parser("state", help="keadaan gerbang: halt, verdict, penolakan terakhir, sesi bisu")
    j_halt = jsub.add_parser("halt", help="hentikan penulisan klaim baru (wajib --reason)")
    j_halt.add_argument("--reason", help="sebab halt; tercatat di berkas halt")
    j_halt.add_argument("--by", default="pemilik", help="pelaku (default: pemilik)")
    j_resume = jsub.add_parser("resume", help="buka lagi penulisan klaim")
    j_resume.add_argument("--by", default="pemilik", help="pelaku (default: pemilik)")

    args = parser.parse_args()
    # Semua perintah CLI (termasuk job terjadwal) menulis lewat satu kanal:
    # stderr berformat, plus berkas harian bila IDX_LOG_DIR diset.
    setup(job=args.command or "idx")
    if args.command == "snapshot":
        cmd_snapshot(args)
    elif args.command == "eod":
        cmd_eod(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "signals":
        cmd_signals(args)
    elif args.command == "alerts":
        cmd_alerts(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "ic":
        cmd_ic(args)
    elif args.command == "signal-log":
        cmd_signal_log(args)
    elif args.command == "recommendations":
        cmd_recommendations(args)
    elif args.command == "journal":
        cmd_journal(args)
    elif args.command == "watchlist":
        client = IDXClient()
        storage = get_storage_from_env()
        try:
            watchlist = _parse_watchlist(os.getenv("IDX_WATCHLIST"))
            n = fetch_and_store_watchlist(client, storage, watchlist)
            say(f"watchlist done — {n} stocks stored")
        finally:
            client.close()
            storage.close()
    elif args.command == "ownership":
        cmd_ownership(args)
    elif args.command == "trade-summary":
        cmd_trade_summary(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "refresh":
        cmd_refresh(args)
    elif args.command == "source":
        from ..source_state import force_source, get_state

        if getattr(args, "set", None):
            force_source(args.set)
            say(f"source di-set ke: {args.set.upper()}")
        st = get_state()
        say(f"source={st.source} probe_count={st.probe_count} idx_ok_count={st.idx_ok_count}")
    else:
        parser.print_help()
