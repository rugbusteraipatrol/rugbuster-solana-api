"""Who made the copies: shared creators and shared funders, exchanges excluded.

MUNK, 2026-10-07: 28 tokens under one ticker, 23 creators; one wallet made
three of them, including the two most traded, and the only funder shared by
two creators was an exchange wallet holding over a million SOL.
"""

from __future__ import annotations

import copy_makers


def _mints(**volumes):
    return {mint: {"first_pair_at": 1000.0, "volume_24h": float(v)} for mint, v in volumes.items()}


def _reader(creators, funders):
    report = lambda mint: {"creator": creators.get(mint)}  # noqa: E731

    def funding(creator, created_at):
        entry = funders.get(creator)
        if not entry:
            return {"status": "busy_wallet"}
        wallet, balance = entry
        return {"status": "ok", "funder": wallet, "funder_balance_sol": balance}

    return report, funding


def setup_function(_):
    copy_makers._results.clear()
    copy_makers._running.clear()


def test_one_wallet_making_the_top_copies_is_a_shared_creator():
    mints = _mints(Top=64_000_000, Second=48_000_000, Third=12_000_000, Other=500)
    report, funding = _reader({"Top": "Cc6e", "Second": "Cc6e", "Third": "Cc6e", "Other": "Ga8R"}, {})
    out = copy_makers.analyse("MUNK", mints, report, funding)
    assert out["shared_creators"] == [{"wallet": "Cc6e", "count": 3, "mints": ["Top", "Second", "Third"],
                                       "includes_most_traded": True}]
    assert out["distinct_creators"] == 2


def test_an_exchange_funder_links_nothing():
    mints = _mints(A=10, B=9)
    report, funding = _reader({"A": "c1", "B": "c2"}, {"c1": ("5tzF", 1_085_397.3), "c2": ("5tzF", 1_085_397.3)})
    out = copy_makers.analyse("MUNK", mints, report, funding)
    assert out["shared_funders"] == [] and out["exchange_funded"] == 2


def test_one_funder_behind_different_creators_is_a_shared_funder():
    mints = _mints(A=10, B=9, C=8)
    report, funding = _reader({"A": "c1", "B": "c2", "C": "c3"}, {"c1": ("F1", 2.0), "c2": ("F1", 2.0), "c3": ("F9", 1.0)})
    out = copy_makers.analyse("MUNK", mints, report, funding)
    assert out["shared_funders"][0]["wallet"] == "F1"
    assert out["shared_funders"][0]["creators"] == ["c1", "c2"]


def test_one_creator_funded_once_is_not_also_a_funder_group():
    mints = _mints(A=10, B=9)
    report, funding = _reader({"A": "c1", "B": "c1"}, {"c1": ("F1", 2.0)})
    out = copy_makers.analyse("MUNK", mints, report, funding)
    assert out["shared_funders"] == [] and out["shared_creators"][0]["count"] == 2


def test_only_the_most_traded_copies_are_read():
    mints = _mints(**{f"m{i}": 100 - i for i in range(30)})
    seen = []
    report = lambda mint: seen.append(mint) or {"creator": mint}  # noqa: E731
    out = copy_makers.analyse("X", mints, report, lambda c, t: {})
    assert len(seen) == copy_makers.MAX_COPIES and out["copies_total"] == 30 and out["checked"] == 15


def test_unreadable_creators_are_counted_not_guessed():
    out = copy_makers.analyse("X", _mints(A=1, B=1), lambda m: None, lambda c, t: {})
    assert out["creators_read"] == 0 and out["shared_creators"] == []


def test_first_call_starts_the_check_and_answers_checking():
    jobs = []
    out = copy_makers.get_or_start("MUNK", mints=lambda s: _mints(A=1), report=lambda m: {"creator": "c"},
                                   funding=lambda c, t: {}, spawn=jobs.append)
    assert out["status"] == "checking" and len(jobs) == 1
    again = copy_makers.get_or_start("munk", mints=lambda s: {}, report=lambda m: {}, funding=lambda c, t: {},
                                     spawn=jobs.append)
    assert again["status"] == "checking" and len(jobs) == 1
    jobs[0]()
    done = copy_makers.get_or_start("MUNK", mints=lambda s: {}, report=lambda m: {}, funding=lambda c, t: {},
                                    spawn=jobs.append)
    assert done["status"] == "ok" and done["checked"] == 1


def test_a_failed_search_is_unavailable():
    jobs = []

    def boom(symbol):
        raise TimeoutError("slow")

    copy_makers.get_or_start("MUNK", mints=boom, report=lambda m: {}, funding=lambda c, t: {}, spawn=jobs.append)
    jobs[0]()
    assert copy_makers.peek("MUNK")["status"] == "unavailable"
