"""Tests for the two-layer ticker -> sector mapping."""

from __future__ import annotations

from idx_scraper.api.sector_map import (
    BUCKETS,
    FALLBACK_SECTOR,
    SECTOR_MAP,
    coverage,
    generated_map,
    sector_for,
)


def test_fallback_is_not_a_real_bucket() -> None:
    assert FALLBACK_SECTOR not in BUCKETS


def test_curated_map_only_uses_known_buckets() -> None:
    unknown = {code: s for code, s in SECTOR_MAP.items() if s not in BUCKETS}
    assert unknown == {}


def test_curated_codes_look_like_tickers() -> None:
    # IDX codes are four letters, with a handful of five-letter leftovers
    # (GOTOM) from delistings and re-listings.
    bad = [code for code in SECTOR_MAP if not code.isupper() or not 4 <= len(code) <= 5]
    assert bad == []


def test_generated_lookup_only_uses_known_buckets() -> None:
    generated = generated_map()
    unknown = {code: s for code, s in generated.items() if s not in BUCKETS}
    assert unknown == {}


def test_generated_lookup_is_keyed_by_uppercase_tickers() -> None:
    bad = [code for code in generated_map() if not code.isupper() or not 4 <= len(code) <= 5]
    assert bad == []


def test_curated_entry_wins_over_the_generated_one() -> None:
    # ASII is curated as Otomotif; whatever the generated layer says, curated rules.
    assert sector_for("asii") == SECTOR_MAP["ASII"]


def test_unknown_code_falls_back_to_lainnya() -> None:
    assert sector_for("ZZZZ") == FALLBACK_SECTOR


def test_lookup_is_case_insensitive() -> None:
    assert sector_for("bbca") == sector_for("BBCA")


def test_generated_layer_covers_the_broad_market() -> None:
    """The whole point of the generated layer: stop dumping most emiten in "Lainnya"."""
    assert len(generated_map()) > 500


def test_coverage_counts_each_layer_separately() -> None:
    codes = list(SECTOR_MAP)[:10] + ["ZZZZ", "ZZZY"]
    out = coverage(codes)

    assert out["total"] == 12
    assert out["curated"] == 10
    assert out["unmapped"] == 2
    assert out["mapped_pct"] == 83.3


def test_coverage_of_an_enormous_universe_is_not_mostly_unmapped() -> None:
    """Guard rail: a regression here means sector analysis went back to noise."""
    universe = set(SECTOR_MAP) | set(generated_map())
    out = coverage(universe)

    assert out["unmapped"] == 0
    assert out["mapped_pct"] == 100.0
