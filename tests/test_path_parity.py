"""The same evidence must give the same answer through either path.

A scanner that says WARN on a fresh scan and DANGER when the same report is
read back from cache is not reporting on the token; it is reporting on where
the answer came from. The caller cannot tell which they got, and neither number
is wrong in a way anyone can point at -- which makes it worse than a plain bug.

It happened here. The non-conclusive cap was added to the live path only, so a
token with active authorities and concentrated ownership came out WARN 69 live
and DANGER 90 from a stored row. Both paths now go through the same rules on
the same normalised evidence.

An earlier test of mine claimed to demonstrate this by comparing a full live
report against a stored row holding nothing but `risk_percent`. Those are not
the same evidence, so it showed less than it said. These pass one payload
through both paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from scoring import carries_rugcheck_report, score_live_rugcheck_report, score_scan_row


def _both_paths(report: dict) -> tuple[dict, dict]:
    """One payload, scanned live and read back as a cached row."""
    live = score_live_rugcheck_report(report)
    stored = score_scan_row({"mint": report.get("mint"), "label": "GOOD",
                             "full_record": json.dumps(report)})
    return live, stored


# Each case is evidence that at least one rule in the chain reacts to, so the
# parity claim is exercised and not merely true of an empty report.
PAYLOADS = {
    "curated_mint_with_administrative_flags": {
        "mint": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
        "score": 50101, "score_normalised": 71,
        "totalHolders": 495_847, "totalMarketLiquidity": 3_638_431.0, "rugged": False,
        "token": {"mintAuthority": "AuthorityAddress", "freezeAuthority": None},
        "tokenMeta": {"name": "Marinade staked SOL", "symbol": "mSOL", "mutable": True},
        "risks": [{"name": "Mint Authority still enabled"}, {"name": "Mutable metadata"}],
    },
    "authorities_and_concentration_hit_the_non_conclusive_cap": {
        "mint": "SomeLiveMint", "score": 50000, "score_normalised": 95,
        "totalHolders": 40_000, "totalMarketLiquidity": 5_000_000, "rugged": False,
        "token": {"mintAuthority": "A", "freezeAuthority": "B"},
        "tokenMeta": {"symbol": "NEWCOIN", "name": "New", "mutable": True},
        "risks": [{"name": "Single holder ownership"},
                  {"name": "Top 10 holders high ownership"}],
    },
    "symbol_collision_hits_the_identity_cap": {
        "mint": "SomeOtherLegitimateMint", "score": 100, "score_normalised": 2,
        "totalHolders": 200_000, "totalMarketLiquidity": 20_000_000, "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"symbol": "RAY", "name": "Unrelated project", "mutable": False},
        "risks": [],
    },
    "collision_plus_an_independent_finding_reaches_danger": {
        "mint": "SomeOtherLegitimateMint", "score": 50000, "score_normalised": 95,
        "totalHolders": 200_000, "totalMarketLiquidity": 20_000_000, "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"symbol": "RAY", "name": "Unrelated project", "mutable": False},
        "risks": [{"name": "Low Liquidity"}],
    },
    "a_dead_mint_cannot_be_cleared": {
        "mint": "23dxgqAtivdW9cZz7UDFAGXUtByz5sXhKRDENdbXpump",
        "score": 1, "score_normalised": 1,
        "totalHolders": 1, "totalMarketLiquidity": 0.0, "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"symbol": "DEAD", "name": "Dead", "mutable": False},
        "risks": [],
    },
    "upstream_says_rugged": {
        "mint": "RuggedMint", "score": 50000, "score_normalised": 40,
        "totalHolders": 900, "totalMarketLiquidity": 100_000.0, "rugged": True,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"symbol": "RUG", "name": "Rugged", "mutable": False},
        "risks": [{"name": "Low Liquidity"}],
    },
}


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_the_same_payload_scores_the_same_through_both_paths(name):
    live, stored = _both_paths(PAYLOADS[name])
    assert stored["risk_score"] == live["risk_score"]
    assert stored["label"] == live["label"]


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_the_cached_answer_reports_nothing_less_than_the_live_one(name):
    """Provenance may be added on the stored side. No finding may be lost."""
    live, stored = _both_paths(PAYLOADS[name])
    assert set(live["risk_flags"]) <= set(stored["risk_flags"])
    assert "verdict_recomputed_from_stored_evidence" in stored["risk_flags"]


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_a_stored_number_never_overrides_the_evidence_beside_it(name):
    """A row carrying the report *and* a stale `risk_percent` from an older
    ruleset answers from the evidence, not from the inherited number. This is
    the case the divergence actually reached in production."""
    row = {"mint": PAYLOADS[name].get("mint"), "label": "DANGER",
           "risk_percent": 97, "full_record": json.dumps(PAYLOADS[name])}
    assert score_scan_row(row)["risk_score"] == score_live_rugcheck_report(
        PAYLOADS[name])["risk_score"]


def test_a_row_without_the_report_is_not_mistaken_for_one():
    """Parity is claimed for the same evidence, not for a bare number. A row
    that carries no upstream report has nothing to recompute from and keeps the
    inheritance ladder -- so the boundary has to be recognisable."""
    assert carries_rugcheck_report({"risk_percent": 95, "label": "DANGER"}) is False
    assert carries_rugcheck_report({"input": "Token: X", "rugcheck_score": 4}) is False
    assert carries_rugcheck_report(PAYLOADS["upstream_says_rugged"]) is True


def test_the_ceilings_also_apply_to_a_row_with_no_upstream_report():
    """The inheritance path is not exempt. A stored number claiming DANGER,
    with nothing beside it but disclosures, is still held at WARN."""
    row = {"mint": "SomeMint", "label": "DANGER", "full_record": json.dumps({
        "risk_percent": 95,
        "risk_flags": ["single_holder_ownership", "top_10_holders_high_ownership"],
    })}
    result = score_scan_row(row)
    assert result["label"] == "WARN"
    assert "non_conclusive_signals_capped_at_warn" in result["risk_flags"]
