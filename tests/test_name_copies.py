"""Other tokens under the same ticker: counted, never scored.

MUNK, 2026-10-06: 27 Solana tokens carried the ticker within two days, from
unrelated creators. The reading says how many and which one trades most; it
never touches the verdict.
"""

from __future__ import annotations

import name_copies

NOW = 1_791_300_000.0
SELF = "AKJxxB9MLarQGpHwTKNRW3PTpP621g2A7dCHKy7qpump"


def _pair(mint, symbol="MUNK", hours_ago=2, volume=1000.0, chain="solana"):
    return {
        "chainId": chain,
        "baseToken": {"address": mint, "symbol": symbol},
        "pairCreatedAt": int((NOW - hours_ago * 3600) * 1000),
        "volume": {"h24": volume},
    }


def _fetch_of(pairs):
    calls = []

    def fetch(url):
        calls.append(url)
        return {"pairs": pairs}

    fetch.calls = calls
    return fetch


def setup_function(_):
    name_copies._cache.clear()


def test_counts_other_mints_under_the_ticker_in_the_last_day():
    pairs = [_pair(SELF, volume=200)] + [_pair(f"Copy{i}", volume=10 * i) for i in range(1, 6)]
    pairs.append(_pair("OldOne", hours_ago=60))
    out = name_copies.read(SELF, "MUNK", fetch=_fetch_of(pairs), now=NOW)
    assert out["status"] == "ok"
    assert out["same_symbol_last_24h"] == 5
    assert out["same_symbol_seen"] == 6
    assert out["this_token_listed"] is True


def test_reports_the_most_traded_mint_and_whether_it_is_this_one():
    pairs = [_pair(SELF, volume=50), _pair("Popular", volume=9_000_000)]
    out = name_copies.read(SELF, "MUNK", fetch=_fetch_of(pairs), now=NOW)
    assert out["most_traded"] == {"mint": "Popular", "volume_24h": 9_000_000.0, "is_this_token": False}


def test_two_pairs_of_one_mint_count_once_and_add_volume():
    pairs = [_pair("Copy", volume=5), _pair("Copy", volume=7, hours_ago=1)]
    out = name_copies.read(SELF, "MUNK", fetch=_fetch_of(pairs), now=NOW)
    assert out["same_symbol_last_24h"] == 1
    assert out["most_traded"]["volume_24h"] == 12.0


def test_other_chains_and_other_tickers_are_ignored():
    pairs = [_pair("EvmMunk", chain="base"), _pair("Munky", symbol="MUNKY"), _pair("Real")]
    out = name_copies.read(SELF, "munk", fetch=_fetch_of(pairs), now=NOW)
    assert out["same_symbol_seen"] == 1


def test_a_full_page_is_a_floor_not_a_count():
    pairs = [_pair(f"Copy{i}") for i in range(name_copies.SEARCH_PAGE)]
    out = name_copies.read(SELF, "MUNK", fetch=_fetch_of(pairs), now=NOW)
    assert out["results_capped"] is True


def test_a_failed_search_is_unavailable_not_an_error():
    def boom(url):
        raise TimeoutError("slow")

    out = name_copies.read(SELF, "MUNK", fetch=boom, now=NOW)
    assert out["status"] == "unavailable"


def test_no_ticker_means_no_search():
    fetch = _fetch_of([])
    out = name_copies.read(SELF, "", fetch=fetch, now=NOW)
    assert out["status"] == "unavailable" and fetch.calls == []


def test_one_search_per_ticker_inside_the_cache_window():
    fetch = _fetch_of([_pair("Copy")])
    name_copies.read(SELF, "MUNK", fetch=fetch, now=NOW)
    name_copies.read("Other", "munk", fetch=fetch, now=NOW + 60)
    assert len(fetch.calls) == 1
