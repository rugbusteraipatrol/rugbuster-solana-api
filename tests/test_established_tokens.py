"""Administrative and distribution flags are disclosures, not rug proof.

The first version of this change gated the suppression on holder count and
liquidity, and folded concentrated ownership in with the administrative flags.
An independent review broke it in one case: an unverified mint with both
authorities live and concentrated ownership came out GOOD 20, on scale alone.
That counterexample is `test_the_reviewed_counterexample_is_no_longer_cleared`
below, and it is the reason the rule now turns on curated identity instead.

Relaxing a detector is the easy way to make a bad number go away, so most of
this file is the other half: proving the relaxation cannot swallow a real rug.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoring import (
    KNOWN_SOLANA_MINTS,
    administrative_flags_only,
    canonical_symbol_mint_mismatch,
    is_curated_canonical_mint,
    non_conclusive_flags_only,
    score_live_rugcheck_report,
)

# mSOL, curated by hand in KNOWN_SOLANA_MINTS.
CURATED = "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"


def _report(**overrides) -> dict:
    """A curated mint whose only raised risks are administrative."""
    report = {
        "mint": CURATED,
        "score": 50101,
        "score_normalised": 71,
        "totalHolders": 495_847,
        "totalMarketLiquidity": 3_638_431.0,
        "rugged": False,
        "token": {"mintAuthority": "AuthorityAddress", "freezeAuthority": None},
        "tokenMeta": {"name": "Marinade staked SOL", "symbol": "mSOL", "mutable": True},
        "risks": [{"name": "Mint Authority still enabled"}, {"name": "Mutable metadata"}],
    }
    report.update(overrides)
    return report


# --- the counterexample the review found -----------------------------------

def test_the_reviewed_counterexample_is_no_longer_cleared():
    """Unverified mint, both authorities live, concentrated ownership.

    Previously GOOD 20 on 50,000 holders and $1m liquidity alone.
    """
    unverified = {
        "mint": "ReviewSyntheticMint",
        "score": 50000,
        "score_normalised": 90,
        "totalHolders": 50_000,
        "totalMarketLiquidity": 1_000_000,
        "rugged": False,
        "token": {"mintAuthority": "Unverified", "freezeAuthority": "Unverified"},
        "risks": [{"name": "Single holder ownership"}, {"name": "Top 10 holders high ownership"}],
    }
    result = score_live_rugcheck_report(unverified)
    assert result["label"] != "GOOD"
    assert "curated_mint_administrative_flags_only" not in result["risk_flags"]
    assert result["label"] == "WARN"


def test_scale_alone_never_clears_a_token():
    """Size is not identity. A mint nobody curated stays unsuppressed however
    large it is."""
    huge = _report(mint="SomeUncuratedMint", totalHolders=9_000_000,
                   totalMarketLiquidity=2_000_000_000)
    assert score_live_rugcheck_report(huge)["label"] != "GOOD"


def test_concentration_is_warn_not_good_or_danger_on_a_curated_mint():
    """Identity explains an authority. It cannot explain away ownership nobody
    has identified."""
    report = _report(risks=_report()["risks"] + [{"name": "Single holder ownership"}])
    result = score_live_rugcheck_report(report)
    assert "curated_mint_administrative_flags_only" not in result["risk_flags"]
    assert result["label"] == "WARN"


# --- the false alarms this was written to fix ------------------------------

def test_a_curated_liquid_staking_token_is_no_longer_danger():
    result = score_live_rugcheck_report(_report())
    assert result["label"] == "GOOD"
    assert result["risk_score"] <= 20


def test_the_authority_flags_are_still_reported():
    """Suppression must not suppress the disclosure: a reader still sees the
    mint authority, and a consumer that cares can act on it."""
    flags = score_live_rugcheck_report(_report())["risk_flags"]
    assert "mint_authority_still_enabled" in flags
    assert "mutable_metadata" in flags
    assert "curated_mint_administrative_flags_only" in flags


# --- what must still bite --------------------------------------------------

def test_a_rugged_token_is_untouched_however_curated():
    result = score_live_rugcheck_report(_report(rugged=True))
    assert result["label"] == "DANGER"
    assert result["risk_score"] >= 90


def test_a_non_administrative_risk_keeps_the_original_verdict():
    """Thin liquidity is an economic finding, not a disclosure about admin."""
    report = _report(risks=_report()["risks"] + [{"name": "Low Liquidity"}])
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


def test_an_uncurated_mint_with_only_admin_flags_is_warn():
    result = score_live_rugcheck_report(_report(mint="NotOnTheList", tokenMeta={"symbol": "NEW"}))
    assert result["label"] == "WARN"
    assert "non_conclusive_signals_capped_at_warn" in result["risk_flags"]


def test_curation_is_by_mint_not_by_symbol():
    """A different mint claiming the same name must not inherit the entry."""
    impostor = _report(mint="FakeMintSameName")
    assert is_curated_canonical_mint(impostor) is False
    assert score_live_rugcheck_report(impostor)["label"] == "DANGER"


def test_protected_symbol_with_a_different_mint_requires_review():
    impostor = _report(mint="FakeMsolMint")
    result = score_live_rugcheck_report(impostor)
    assert canonical_symbol_mint_mismatch(impostor) is True
    assert result["label"] == "DANGER"
    assert "symbol_matches_curated_asset_but_mint_differs" in result["risk_flags"]


def test_the_curated_list_is_hand_written_and_small():
    """A list that grows automatically stops being a verified list."""
    assert 0 < len(KNOWN_SOLANA_MINTS) < 50


# --- the helpers -----------------------------------------------------------

def test_administrative_only_rejects_an_economic_risk():
    assert administrative_flags_only(["mint_authority_still_enabled", "mutable_metadata"])
    assert not administrative_flags_only(["mint_authority_still_enabled", "low_liquidity"])


def test_administrative_only_rejects_concentration():
    assert not administrative_flags_only(["mint_authority_still_enabled", "single_holder_ownership"])


def test_admin_plus_concentration_is_non_conclusive():
    assert non_conclusive_flags_only([
        "mint_authority_still_enabled", "single_holder_ownership"
    ])


def test_low_liquidity_is_not_non_conclusive():
    assert not non_conclusive_flags_only(["mint_authority_still_enabled", "low_liquidity"])


def test_curation_ignores_holders_and_liquidity():
    assert is_curated_canonical_mint({"mint": CURATED}) is True
    assert is_curated_canonical_mint({"mint": CURATED, "totalHolders": 0}) is True
    assert is_curated_canonical_mint({"totalHolders": 10**9, "totalMarketLiquidity": 10**12}) is False


def test_the_rule_only_lowers_a_score_and_never_raises_one():
    result = score_live_rugcheck_report(_report(score_normalised=3, risks=[]))
    assert result["risk_score"] < 20
    assert "curated_mint_administrative_flags_only" not in result["risk_flags"]


# --- what the new rules reach, stated so it cannot drift silently ----------
#
# Two rules landed together and both move a verdict on evidence that is weaker
# than the verdict sounds. Neither is asserted to be wrong here; they are
# pinned so that a later reader sees the reach and can argue with it.

def test_a_clean_token_sharing_a_curated_symbol_is_danger_on_that_fact_alone():
    """Solana tickers are not unique. A mint with no findings at all, no
    authorities, deep liquidity and a symbol that collides with a curated
    asset is called DANGER on the collision alone.

    The rule's own docstring says this establishes an address mismatch and not
    malicious intent -- DANGER is the strongest label we have, so the verdict
    says more than the evidence does. Recorded for review, not endorsed."""
    collision = {
        "mint": "SomeOtherLegitimateMint",
        "score": 100,
        "score_normalised": 2,
        "totalHolders": 200_000,
        "totalMarketLiquidity": 20_000_000,
        "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"symbol": "RAY", "name": "Unrelated project", "mutable": False},
        "risks": [],
    }
    result = score_live_rugcheck_report(collision)
    assert result["label"] == "DANGER"
    assert result["risk_flags"] == ["symbol_matches_curated_asset_but_mint_differs"]


def test_the_cap_does_not_reach_a_token_whose_symbol_collides():
    """The mismatch flag is outside the non-conclusive set, so a collision is
    not capped back down to WARN by the rule below it."""
    collision = {
        "mint": "AnotherMint", "score": 50000, "score_normalised": 95,
        "totalHolders": 40_000, "totalMarketLiquidity": 5_000_000, "rugged": False,
        "token": {"mintAuthority": "A", "freezeAuthority": "B"},
        "tokenMeta": {"symbol": "JLP", "name": "Not the real one", "mutable": True},
        "risks": [{"name": "Single holder ownership"}],
    }
    result = score_live_rugcheck_report(collision)
    assert result["label"] == "DANGER"
    assert "non_conclusive_signals_capped_at_warn" not in result["risk_flags"]


def test_a_token_with_no_symbol_is_outside_the_mismatch_rule():
    """The rule reads the reported symbol. A report that carries none cannot
    collide, so omitting the symbol is a way around it."""
    anonymous = {
        "mint": "NoSymbolMint", "score": 100, "score_normalised": 2,
        "totalHolders": 200_000, "totalMarketLiquidity": 20_000_000, "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "No symbol", "mutable": False}, "risks": [],
    }
    assert canonical_symbol_mint_mismatch(anonymous) is False


def test_the_cap_makes_the_live_path_disagree_with_the_stored_path():
    """Same evidence, two answers.

    The cap was added to `score_live_rugcheck_report` only. A stored row for
    the same token still yields DANGER, so which answer a caller receives now
    depends on whether the row was cached, not on the token."""
    from scoring import score_scan_row

    live_report = {
        "mint": "DivergentMint", "score": 50000, "score_normalised": 95,
        "totalHolders": 40_000, "totalMarketLiquidity": 5_000_000, "rugged": False,
        "token": {"mintAuthority": "A", "freezeAuthority": "B"},
        "tokenMeta": {"symbol": "NEWCOIN", "name": "New", "mutable": True},
        "risks": [{"name": "Single holder ownership"},
                  {"name": "Top 10 holders high ownership"}],
    }
    live = score_live_rugcheck_report(live_report)
    stored = score_scan_row({"mint": "DivergentMint", "risk_percent": 95, "label": "DANGER"})

    assert live["label"] == "WARN"
    assert stored["label"] == "DANGER"
