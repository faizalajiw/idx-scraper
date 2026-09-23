"""FastAPI backend for the IDX scraper.

Owns all business/data calculations (movers, signals, indicators, history)
and serves JSON to the Next.js + Recharts frontend. Reads from Postgres via
the same schema produced by ``SupabaseStorage``. The APScheduler CLI worker
remains the sole ingestion path — this API is read-only.
"""
