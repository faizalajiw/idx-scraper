"""Rolling per-emiten live capture from IDX ``GetTradingInfoDaily``.

IDX's whole-market ``GetStockSummary`` is EOD-only: while the market is open a
request for today returns zero rows, and the dateless request returns the last
*completed* session. The only genuinely live per-stock feed is
``GetTradingInfoDaily`` which takes a single ``code`` per request.

IDX rate-limits that endpoint hard (HTTP 429, token-bucket ~burst 20, refill
~1.3/s), so the sweep paces at ~1 req/s with adaptive backoff on 429.  Coverage
is therefore the *liquid* universe (~80 names), not all ~960 listed emiten.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

WIB = timezone(timedelta(hours=7))
_MIN_INTERVAL = 1.0
_BACKOFF_STEPS = (5, 15, 30, 60)
_THROTTLE_ABORT_RUN = 3

_UNIVERSE_LIQUID_SQL = """
    select code from research.latest_pit
    where trade_date = (select max(trade_date) from research.latest_pit)
      and value is not null and value > 0
    order by value desc limit %s
"""

_UNIVERSE_MOVERS_SQL = """
    select code from research.latest_pit
    where trade_date = (select max(trade_date) from research.latest_pit)
      and percent is not null and volume > 0
    order by abs(percent) desc limit %s
"""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def load_universe(cur: Any, top_n: int, movers_n: int = 20) -> list[str]:
    cur.execute(_UNIVERSE_LIQUID_SQL, (top_n,))
    codes = [r["code"] for r in cur.fetchall() if r["code"]]
    if movers_n > 0:
        cur.execute(_UNIVERSE_MOVERS_SQL, (movers_n,))
        codes.extend(r["code"] for r in cur.fetchall() if r["code"])
    return list(dict.fromkeys(codes))


class LiveCapture:
    def __init__(
        self, client: Any, *, top_n: int | None = None,
        delay_sec: float | None = None,
        should_run: Callable[[], bool] | None = None,
    ) -> None:
        self._client = client
        self._top_n = top_n if top_n is not None else _env_int("IDX_LIVE_TOP", 60)
        self._delay = (
            max(_MIN_INTERVAL, delay_sec) if delay_sec is not None
            else _env_int("IDX_LIVE_DELAY_MS", 1000) / 1000
        )
        self._should_run = should_run
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._throttle_run = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="idx-live-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _pause(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if (self._should_run is None or self._should_run()) and self._throttle_run < len(_BACKOFF_STEPS):
                    self.sweep_once()
            except Exception as e:
                print(f"[err] live capture: {e}", file=sys.stderr, flush=True)
            self._pause(5)

    def _status(self) -> int | None:
        return getattr(self._client.session, "last_status", None)

    def sweep_once(self) -> int:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                codes = load_universe(cur, self._top_n)
            if not codes:
                return 0

            if self._throttle_run:
                cool = _BACKOFF_STEPS[min(self._throttle_run, len(_BACKOFF_STEPS)) - 1]
                print(f"[{datetime.now(WIB).isoformat()}] live capture cooling down {cool}s", flush=True)
                self._pause(cool)

            rows: list[tuple[Any, ...]] = []
            throttled = misses = 0
            for code in codes:
                if self._stop.is_set():
                    break
                try:
                    q = self._client.fetch_trading_daily(code)
                except Exception as e:
                    print(f"[warn] live {code}: {e}", file=sys.stderr, flush=True)
                    q = None

                if q is not None and q.close is not None and q.close > 0:
                    rows.append((code, datetime.now(WIB), q.close, q.volume, q.value))
                    throttled = misses = 0
                else:
                    if self._status() == 429:
                        throttled += 1
                        misses = 0
                        if throttled >= _THROTTLE_ABORT_RUN:
                            print(f"[warn] live capture throttled at {code}; aborting cycle", file=sys.stderr, flush=True)
                            break
                    else:
                        misses += 1
                        throttled = 0
                self._pause(self._delay)

            if rows:
                with conn.cursor() as cur:
                    cur.execute("select research.ensure_intraday_partition(%s)", (rows[0][1].date(),))
                    cur.executemany(
                        """insert into research.intraday_ticks (code, ts, last, volume, value)
                           values (%s, %s, %s, %s, %s) on conflict (code, ts) do nothing""",
                        rows,
                    )

        if throttled >= _THROTTLE_ABORT_RUN:
            self._throttle_run += 1
        else:
            self._throttle_run = 0
        print(f"[{datetime.now(WIB).isoformat()}] live captured: {len(rows)}/{len(codes)} (untraded {misses})", flush=True)
        return len(rows)
