"""The live path must not clear a token it has no basis to clear.

Measured against ground truth on 2026-09-07: of 17 pump.fun tokens
independently confirmed as creator dumps by tracing balances on-chain, the
live path scored **16 as GOOD with risk 1**. The cached collector path caught
all 17 -- which is what the public GoPlus benchmark measured -- but any token
not already in cache goes through the live path, and that is precisely the
fresh-token population the benchmark said was fixed.

RugCheck was not wrong. Its static checks genuinely find nothing on a mint
with two holders. It was being asked a question it cannot answer, and a floor
score was read as a clean bill of health.
"""

from __future__ import annotations

import pytest

from scoring import (
    KNOWN_SOLANA_MINTS,
    MIN_HOLDERS_FOR_CLEAN_VERDICT,
    live_report_supports_clean_verdict,
    score_live_rugcheck_report,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _report(**over):
    """Shape mirrors a real RugCheck response; defaults are a healthy token."""
    base = {
        "mint": "SomeUnknownMint1111111111111111111111111111",
        "score": 1,
        "score_normalised": 1,
        "rugged": False,
        "totalHolders": 5000,
        "totalMarketLiquidity": 250_000.0,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "Test", "symbol": "TST", "mutable": False},
        "risks": [],
    }
    base.update(over)
    return base


# --- the measured failure: an empty mint with a floor score ---

@pytest.mark.parametrize("holders", [1, 2, 3, 5, 14])
def test_a_mint_with_almost_no_holders_is_never_cleared(holders):
    """Every one of the 17 confirmed rugs sat in this range."""
    result = score_live_rugcheck_report(_report(totalHolders=holders, totalMarketLiquidity=800.0))
    assert result["label"] != "GOOD"
    assert "live_scan_cannot_clear_token" in result["risk_flags"]


def test_withholding_does_not_manufacture_danger():
    """Absence of evidence is not evidence against the token -- WARN, not DANGER."""
    result = score_live_rugcheck_report(_report(totalHolders=2, totalMarketLiquidity=180.0))
    assert result["label"] == "WARN"
    assert result["risk_score"] < 70


def test_no_holder_or_liquidity_data_at_all_is_not_cleared():
    supported, reason = live_report_supports_clean_verdict(
        _report(totalHolders=0, totalMarketLiquidity=0)
    )
    assert supported is False
    assert reason == "rugcheck_returned_no_holder_or_liquidity_data"


def test_thin_liquidity_is_not_cleared():
    supported, reason = live_report_supports_clean_verdict(
        _report(totalHolders=5000, totalMarketLiquidity=900.0)
    )
    assert supported is False
    assert reason == "insufficient_liquidity_to_clear"


# --- the other direction: real tokens must still pass ---

def test_a_healthy_token_is_still_good():
    assert score_live_rugcheck_report(_report())["label"] == "GOOD"


def test_a_fresh_but_real_token_is_still_good():
    """Sampled live: 924 holders and $8.8k liquidity on a same-day pump.fun mint.
    The guard must separate 'new' from 'empty'."""
    result = score_live_rugcheck_report(_report(totalHolders=924, totalMarketLiquidity=8_813.0))
    assert result["label"] == "GOOD"


def test_a_real_rugcheck_score_is_trusted_and_not_second_guessed():
    """Above the floor, RugCheck has actually measured something."""
    supported, _ = live_report_supports_clean_verdict(
        _report(score=101, totalHolders=2, totalMarketLiquidity=0)
    )
    assert supported is True


# --- canonical mints ---

def test_usdc_keeps_its_authorities_without_being_penalised():
    """USDC holds mint and freeze authority by design: Circle must be able to
    issue, and to freeze under court order. Scoring that as a rug vector put
    USDC and USDT at risk 55 (WARN) on this path in production."""
    result = score_live_rugcheck_report(
        _report(
            mint=USDC,
            totalHolders=0,
            totalMarketLiquidity=0,
            token={"mintAuthority": "BJE5MMbq", "freezeAuthority": "7dGbd2QZ"},
            tokenMeta={"name": "USD Coin", "symbol": "USDC", "mutable": True},
        )
    )
    assert result["label"] == "GOOD"
    assert "known_canonical_solana_mint" in result["risk_flags"]
    assert "mint_authority_active" not in result["risk_flags"]


def test_an_unknown_mint_with_both_authorities_is_still_penalised():
    """The exemption is curation, not a hole -- it must not generalise."""
    result = score_live_rugcheck_report(
        _report(token={"mintAuthority": "X", "freezeAuthority": "Y"})
    )
    assert result["label"] != "GOOD"
    assert "mint_authority_active" in result["risk_flags"]


def test_a_known_mint_flagged_rugged_is_still_danger():
    """Curation clears expected authorities, never a live rug signal."""
    result = score_live_rugcheck_report(_report(mint=USDC, rugged=True))
    assert result["label"] == "DANGER"


def test_known_mint_list_is_not_accidentally_empty():
    assert USDC in KNOWN_SOLANA_MINTS
    assert len(KNOWN_SOLANA_MINTS) >= 5


def test_holder_threshold_is_above_the_observed_rug_range():
    """The 17 confirmed rugs topped out at 14 holders."""
    assert MIN_HOLDERS_FOR_CLEAN_VERDICT > 14
