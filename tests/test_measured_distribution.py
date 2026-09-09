"""Read the holder table, do not just repeat what the report calls it.

A distribution finding is a claim about numbers that arrive in the same
document. We were acting on the claim without ever checking the numbers, and on
a curated mint that cost the token its identity suppression: wrapped BTC and
wrapped ETH sat at WARN because the largest holder was "unidentified", when the
largest holder is the Jupiter perps vault -- which is the mint and freeze
authority of JLP, a mint we had already curated by hand.

Two ways a finding fails here, and neither of them deletes it from the
response:

  - the concentration is not in the table at all;
  - it is there, and it sits in an account we can name.

Nothing is relaxed by measuring less. An absent holder table yields no
measurement and therefore contradicts nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoring import (
    KNOWN_PROTOCOL_VAULTS,
    measured_concentration,
    score_live_rugcheck_report,
    unidentified_concentration,
    unsupported_distribution_flags,
)

VAULT = "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49"
CURATED_WBTC = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"


def _report(holders, **overrides) -> dict:
    report = {
        "mint": CURATED_WBTC,
        "score": 50000,
        "score_normalised": 73,
        "totalHolders": 274_036,
        "totalMarketLiquidity": 1_000_000.0,
        "rugged": False,
        "token": {"mintAuthority": "BridgeAuthority", "freezeAuthority": None},
        "tokenMeta": {"name": "Wrapped BTC (Wormhole)", "symbol": "WBTC", "mutable": True},
        "risks": [{"name": "Mint Authority still enabled"},
                  {"name": "Single holder ownership"},
                  {"name": "High holder concentration"},
                  {"name": "Mutable metadata"}],
        "topHolders": holders,
    }
    report.update(overrides)
    return report


def _holders(*pairs):
    return [{"owner": owner, "pct": pct} for owner, pct in pairs]


# --- the case this was written for -----------------------------------------

def test_a_curated_mint_whose_concentration_is_a_named_vault_is_cleared():
    report = _report(_holders((VAULT, 60.4), ("SomeoneElse", 3.3), ("Another", 1.3)))
    result = score_live_rugcheck_report(report)
    assert result["label"] == "GOOD"
    assert "curated_mint_administrative_flags_only" in result["risk_flags"]
    assert "concentration_held_by_jupiter_perps_vault" in result["risk_flags"]


def test_the_findings_are_still_in_the_response():
    """Suppression is about what a verdict may rest on, never about hiding a
    fact. A reader must still see what RugCheck raised."""
    report = _report(_holders((VAULT, 60.4), ("SomeoneElse", 3.3)))
    flags = score_live_rugcheck_report(report)["risk_flags"]
    assert "single_holder_ownership" in flags
    assert "high_holder_concentration" in flags
    assert "mint_authority_still_enabled" in flags
    assert "distribution_findings_not_supported_by_holder_table" in flags


# --- what must still bite --------------------------------------------------

def test_an_unnamed_wallet_holding_the_same_share_is_not_cleared():
    """Identical concentration, one address we cannot name. This is the whole
    difference, and it must be the whole difference."""
    report = _report(_holders(("NobodyHasNamedThis", 60.4), ("SomeoneElse", 3.3)))
    result = score_live_rugcheck_report(report)
    assert result["label"] != "GOOD"
    assert "concentration_held_by_jupiter_perps_vault" not in result["risk_flags"]


def test_a_named_vault_does_not_cover_a_second_unnamed_holder():
    """Setting the vault aside must leave the rest of the table standing."""
    report = _report(_holders((VAULT, 40.0), ("UnnamedWhale", 45.0)))
    assert unidentified_concentration(report)["top1_pct"] == 45.0
    assert score_live_rugcheck_report(report)["label"] != "GOOD"


def test_a_report_with_no_holder_table_contradicts_nothing():
    """An absent measurement is not a good measurement. This is the same
    absent-signal-read-as-clean mistake the rest of this scanner keeps
    finding, and it would land here first."""
    report = _report([])
    assert measured_concentration(report)["top1_pct"] is None
    assert unsupported_distribution_flags(report, ["single_holder_ownership"]) == []
    assert score_live_rugcheck_report(report)["label"] != "GOOD"


def test_a_vault_cannot_clear_a_mint_nobody_curated():
    """Naming the holder answers "who holds it", not "who issued it". An
    uncurated mint keeps its unexplained authorities."""
    report = _report(_holders((VAULT, 60.4)), mint="NotOnTheCuratedList",
                     tokenMeta={"symbol": "NOPE", "name": "Uncurated", "mutable": True})
    assert score_live_rugcheck_report(report)["label"] != "GOOD"


def test_a_rugged_token_is_untouched_by_any_of_this():
    report = _report(_holders((VAULT, 60.4)), rugged=True)
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


# --- the measurement itself ------------------------------------------------

def test_a_top1_finding_below_the_threshold_is_unsupported():
    report = _report(_holders(("A", 20.8), ("B", 10.0), ("C", 5.0)))
    assert "single_holder_ownership" in unsupported_distribution_flags(
        report, ["single_holder_ownership"])


def test_a_top1_finding_above_the_threshold_stands():
    report = _report(_holders(("A", 47.2), ("B", 10.0)))
    assert unsupported_distribution_flags(report, ["single_holder_ownership"]) == []


def test_a_top10_finding_is_judged_on_the_top_ten_and_not_on_the_largest():
    """Ten holders at 8% each is 80% between them and 8% at the top."""
    report = _report(_holders(*[(f"H{i}", 8.0) for i in range(12)]))
    unsupported = unsupported_distribution_flags(
        report, ["single_holder_ownership", "top_10_holders_high_ownership"])
    assert "single_holder_ownership" in unsupported
    assert "top_10_holders_high_ownership" not in unsupported


def test_a_finding_we_cannot_measure_is_left_alone():
    """Holder correlation has no number in this report. Silence is not
    contradiction."""
    report = _report(_holders(("A", 2.0), ("B", 1.0)))
    assert unsupported_distribution_flags(report, ["high_holder_correlation"]) == []


def test_every_curated_vault_is_named_not_just_listed():
    """A bare address in a list is unreviewable. Each carries what it is."""
    assert KNOWN_PROTOCOL_VAULTS
    assert all(isinstance(name, str) and name.strip() for name in KNOWN_PROTOCOL_VAULTS.values())
    assert len(KNOWN_PROTOCOL_VAULTS) < 25
