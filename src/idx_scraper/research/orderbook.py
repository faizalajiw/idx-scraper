"""Faktor order-book dari snapshot intraday ``stock_quotes``.

Sumber data: baris snapshot yang punya best bid/offer (kedua volume > 0).
Dari situ dihitung dua faktor harian per emiten:

- ``ob_imbalance``  : rata-rata ketimpangan buku intraday
                      (bid_vol - offer_vol) / (bid_vol + offer_vol).
                      > 0 = bid lebih tebal (akumulasi diam-diam),
                      < 0 = offer lebih tebal (distribusi).
- ``ob_absorption`` : rata-rata signed imbalance pada interval harga yang
                      bergerak: imbalance × sign(return antar snapshot),
                      dinormalisasi jumlah interval bergerak.
                      > 0 = buku ikut arah harga (konfirmasi),
                      < 0 = buku melawan arah harga (absorption — offer tebal
                      tapi harga tetap naik = buyer kuat menyerap offer).

Aturan yang dipegang (sama dengan ``research.factors``):
1. **Pure pandas/numpy** — tanpa dependensi baru.
2. **No look-ahead.** Faktor di (code, date) hanya pakai snapshot tanggal itu
   (informasi yang sudah tertutup pada close hari T).
3. **Coverage rendah tetap NaN** (``min_snaps``) — bukan diisi angka rekaan.
4. **Dedup (code, ts)** — baris multi-board/multi-source tidak dihitung dobel;
   board RG (reguler) diprioritaskan sebagai wakil snapshot.

Modul ini murni transformasi DataFrame; akses DB hanya di ``load_snapshots``.
"""

from __future__ import annotations

import pandas as pd

# Batas kualitas: hari dengan snapshot buku lebih sedikit dari ini -> NaN.
MIN_SNAPS_DEFAULT = 8
# Absorption butuh minimal gerakan naik & turun; tanpa itu sign-nya tak bermakna.
ABS_MIN_MOVES = 2

OB_FACTOR_COLUMNS = ["ob_imbalance", "ob_absorption"]


def load_snapshots(
    dsn: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Muat snapshot buku (bid/offer volume > 0) dari stock_quotes.

    Filter tanggal memakai tanggal WIB dari captured_at. ``dsn`` dipakai
    apa-adanya (mis. DATABASE_URL) — pola yang sama dengan ic.load_panel.
    """
    import psycopg

    query = """
        select code, captured_at as ts, board,
               bid, bid_volume, offer, offer_volume, close
        from stock_quotes
        where bid_volume is not null and offer_volume is not null
          and bid_volume > 0 and offer_volume > 0
    """
    params: list = []
    if start:
        query += " and (captured_at at time zone 'Asia/Jakarta')::date >= %s"
        params.append(start)
    if end:
        query += " and (captured_at at time zone 'Asia/Jakarta')::date <= %s"
        params.append(end)
    query += " order by code, captured_at"

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    columns = ["code", "ts", "board", "bid", "bid_volume", "offer", "offer_volume", "close"]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows, columns=columns)
    for col in ("bid", "bid_volume", "offer", "offer_volume", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _wib(ts: pd.Series) -> pd.Series:
    """Konversi kolom timestamp ke wall-clock WIB naive (tz-aware atau tidak)."""
    ts = pd.to_datetime(ts)
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Jakarta")
    else:
        ts = ts.dt.tz_localize("Asia/Jakarta")
    return ts.dt.tz_localize(None)


def _dedup_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    """Satu baris per (code, ts): snapshot RG menang, sisanya ambil terakhir."""
    df = df.copy()
    df["_pref"] = (df["board"].astype(str).str.upper() == "RG").astype(int)
    df = df.sort_values(["code", "ts", "_pref"], kind="mergesort")
    return df.drop_duplicates(["code", "ts"], keep="last").drop(columns="_pref")


def compute_daily(
    snaps: pd.DataFrame,
    min_snaps: int = MIN_SNAPS_DEFAULT,
    min_moves: int = ABS_MIN_MOVES,
) -> pd.DataFrame:
    """Agregat snapshot buku -> faktor harian per (code, date).

    Returns DataFrame ``code, date, ob_imbalance, ob_absorption`` dengan
    ``date`` = tengah malam WIB (datetime64) agar match dengan panel IC.
    """
    if snaps.empty:
        return pd.DataFrame(columns=["code", "date", *OB_FACTOR_COLUMNS])

    df = snaps.copy()
    df["ts"] = _wib(df["ts"])
    df = df.dropna(subset=["bid_volume", "offer_volume", "close"])
    df = df[(df["bid_volume"] > 0) & (df["offer_volume"] > 0) & (df["close"] > 0)]
    if df.empty:
        return pd.DataFrame(columns=["code", "date", *OB_FACTOR_COLUMNS])

    df = _dedup_snapshots(df)
    df["date"] = df["ts"].dt.normalize()

    # Ketimpangan buku per snapshot: bid lebih tebal -> positif.
    book = df["bid_volume"] + df["offer_volume"]
    df["imb"] = (df["bid_volume"] - df["offer_volume"]) / book.where(book > 0)

    # Arah tick antar snapshot (per kode): backward-looking (pct_change).
    df["dir"] = (
        df.groupby("code")["close"]
        .transform(lambda s: s.pct_change())
        .pipe(lambda s: s.gt(0).astype(float) - s.lt(0).astype(float))
    )
    # Kontribusi signed: imbalance yang muncul saat harga bergerak ke arah itu.
    df["signed"] = df["imb"] * df["dir"]

    agg = df.groupby(["code", "date"]).agg(
        n=("imb", "size"),
        ob_imbalance=("imb", "mean"),
        signed_sum=("signed", "sum"),
        n_moves=("dir", lambda s: int((s != 0).sum())),
        n_up=("dir", lambda s: int((s > 0).sum())),
        n_dn=("dir", lambda s: int((s < 0).sum())),
    )

    # Gate coverage: terlalu sedikit snapshot -> NaN (jangan isi rekaan).
    agg["ob_imbalance"] = agg["ob_imbalance"].where(agg["n"] >= min_snaps)

    # Absorption: butuh cukup interval naik DAN turun supaya "melawan arah"
    # punya makna; normalisasi jumlah interval yang bergerak -> skala (-1, 1).
    enough_moves = (agg["n_up"] >= min_moves) & (agg["n_dn"] >= min_moves)
    agg["ob_absorption"] = (
        (agg["signed_sum"] / agg["n_moves"].where(agg["n_moves"] > 0))
        .where(enough_moves)
    )

    return agg.reset_index()[["code", "date", *OB_FACTOR_COLUMNS]]


def attach_orderbook_factors(panel: pd.DataFrame, ob_daily: pd.DataFrame) -> pd.DataFrame:
    """Merge faktor order-book ke panel (code, date) hasil compute_factors.

    Kode/tanggal tanpa data buku -> NaN (dibuang otomatis per tanggal oleh
    engine IC). Merge strict on (code, date) — tidak ada as-of lintas tanggal.
    """
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"]).dt.normalize()
    if ob_daily.empty:
        for c in OB_FACTOR_COLUMNS:
            panel[c] = float("nan")
        return panel
    ob = ob_daily.copy()
    ob["date"] = pd.to_datetime(ob["date"]).dt.normalize()
    return panel.merge(ob, on=["code", "date"], how="left")
