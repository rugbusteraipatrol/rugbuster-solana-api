"""An administrative flag is a disclosure. It is not a rug verdict.

Measured on 55 independently-sourced, demonstrably-traded Solana tokens, the
live path called 42% of them DANGER: JitoSOL 86, Jupiter's own JLP 100,
canonical WETH 74 -- while passing the memecoins, because a pump.fun launch
revokes exactly the authorities those tokens legitimately keep.

Relaxing a detector is the easy way to make a bad number go away, so most of
this file is the other half: proving the relaxation cannot swallow a real rug.
The rule requires an economic footprint four orders of magnitude above the
confirmed-rug population AND that every raised risk is administrative, and any
one of those failing must leave the old verdict untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoring import (
    ESTABLISHED_HOLDERS,
    ESTABLISHED_LIQUIDITY,
    administrative_flags_only,
    is_established_token,
    score_live_rugcheck_report,
)


def _report(**overrides) -> dict:
    """A JitoSOL-shaped report: huge holder base, authority flags only."""
    report = {
        "mint": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
        "score": 50101,
        "score_normalised": 71,
        "totalHolders": 557_133,
        "totalMarketLiquidity": 20_652_531.0,
        "rugged": False,
        "token": {"mintAuthority": "AuthorityAddress", "freezeAuthority": None},
        "tokenMeta": {"name": "Jito Staked SOL", "symbol": "JitoSOL", "mutable": True},
        "risks": [{"name": "Mint Authority still enabled"}, {"name": "Mutable metadata"}],
    }
    report.update(overrides)
    return report


# --- the false alarms this was written to fix ---

def test_liquid_staking_token_is_no_longer_danger():
    result = score_live_rugcheck_report(_report())
    assert result["label"] == "GOOD"
    assert result["risk_score"] <= 20


def test_the_authority_flags_are_still_reported():
    """Suppressing the verdict must not suppress the disclosure."""
    flags = score_live_rugcheck_report(_report())["risk_flags"]
    assert "mint_authority_still_enabled" in flags
    assert "mutable_metadata" in flags
    assert "established_token_administrative_flags_only" in flags


def test_concentration_flags_on_a_huge_holder_base_are_administrative():
    """"Top 10 holders" across 786k holders is pools and bridges, not an exit."""
    report = _report(
        risks=[
            {"name": "Mint Authority still enabled"},
            {"name": "Missing file metadata"},
            {"name": "Mutable metadata"},
            {"name": "Single holder ownership"},
            {"name": "Top 10 holders high ownership"},
        ],
        score_normalised=74,
        totalHolders=786_182,
        totalMarketLiquidity=10_255_021.0,
    )
    assert score_live_rugcheck_report(report)["label"] == "GOOD"


# --- what must still bite ---

def test_a_rugged_token_is_untouched_however_large():
    result = score_live_rugcheck_report(_report(rugged=True))
    assert result["label"] == "DANGER"
    assert result["risk_score"] >= 90


def test_a_non_administrative_risk_keeps_the_original_verdict():
    """Thin liquidity is an economic finding, not a disclosure about admin."""
    report = _report(risks=_report()["risks"] + [{"name": "Low Liquidity"}])
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


def test_a_small_token_with_the_same_flags_is_untouched():
    """SOLO: 11,653 holders and $138k. Below the bar, so nothing changes."""
    report = _report(totalHolders=11_653, totalMarketLiquidity=137_799.0, score_normalised=74)
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


def test_holders_alone_do_not_qualify_a_token():
    report = _report(totalMarketLiquidity=1_000.0)
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


def test_liquidity_alone_does_not_qualify_a_token():
    report = _report(totalHolders=12)
    assert score_live_rugcheck_report(report)["label"] == "DANGER"


def test_the_confirmed_rug_profile_cannot_reach_the_threshold():
    """The 17 confirmed pump.fun rugs sat at 2-5 holders and under $4k."""
    for holders, liquidity in [(2, 900.0), (5, 3_999.0), (43, 12_000.0)]:
        assert not is_established_token(
            {"totalHolders": holders, "totalMarketLiquidity": liquidity}
        )


def test_missing_footprint_data_does_not_qualify():
    """PYUSD comes back holders=0, liquidity=0. Absence is not a footprint."""
    assert not is_established_token({"totalHolders": 0, "totalMarketLiquidity": 0})
    assert not is_established_token({})


def test_thresholds_stay_far_above_the_rug_population():
    """Guards the constants themselves against being quietly lowered."""
    assert ESTABLISHED_HOLDERS >= 50_000
    assert ESTABLISHED_LIQUIDITY >= 1_000_000


# --- the helper ---

def test_administrative_only_rejects_an_economic_risk():
    assert administrative_flags_only(["mint_authority_still_enabled", "mutable_metadata"])
    assert not administrative_flags_only(["mint_authority_still_enabled", "low_liquidity"])


def test_the_cap_only_lowers_a_score_and_never_raises_one():
    """The rule may lower a verdict, never invent risk.

    The token below already scores under the cap, so the cap must not pull it
    up to 20. (The residual is this path's own long-standing +10/+5 additions
    for a live mint authority and mutable metadata, which predate this rule.)
    """
    result = score_live_rugcheck_report(_report(score_normalised=3, risks=[]))
    assert result["risk_score"] < 20
    assert "established_token_administrative_flags_only" not in result["risk_flags"]
