"""A young token whose first seconds could not be read is not cleared.

MUNK (AKJxxB9MLarQGpHwTKNRW3PTpP621g2A7dCHKy7qpump), 2026-10-06: the creator
held 0.26% and sold it after 55 s, the launch read was `unavailable` (over
1,000 transactions 52 minutes in), a large wallet dumped the price ~70%, and
the token scored GOOD 1. A gap in our own checks was shown as clean.
"""

from __future__ import annotations

import time

from plain_language import describe
from scoring import (
    YOUNG_TOKEN_MAX_AGE_SECONDS,
    launch_gap_blocks_clean_verdict,
    score_live_rugcheck_report,
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = time.time()


def _report(**over):
    base = {
        "mint": "AKJxxB9MLarQGpHwTKNRW3PTpP621g2A7dCHKy7qpump",
        "score": 1,
        "score_normalised": 1,
        "rugged": False,
        "totalHolders": 727,
        "totalMarketLiquidity": 25_000.0,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "Munk", "symbol": "MUNK", "mutable": False},
        "risks": [],
    }
    base.update(over)
    return base


def _position(age_seconds, share=0.258):
    created = NOW - age_seconds
    return {
        "status": "sold",
        "share_at_creation_pct": share,
        "peak_share_pct": share,
        "sold_pct_of_peak": 100.0,
        "sold_after_seconds": 55,
        "first_seen_at": created,
    }


UNAVAILABLE = {
    "status": "unavailable",
    "reason": "token has more than 1,000 transactions; its launch is beyond the readable window",
}


def test_munk_is_not_cleared():
    result = score_live_rugcheck_report(_report(), position=_position(52 * 60), snipe=UNAVAILABLE)
    assert result["label"] == "WARN"
    assert result["risk_score"] < 70
    assert "young_token_launch_not_readable" in result["risk_flags"]


def test_summary_says_why():
    result = score_live_rugcheck_report(_report(), position=_position(52 * 60), snipe=UNAVAILABLE)
    text = describe({**result, "launch_snipe": UNAVAILABLE})
    assert text["verdict_summary"].startswith("Too young to clear")
    assert text["verdict_basis"] == "REFUSAL"


def test_a_day_old_token_is_unaffected():
    result = score_live_rugcheck_report(
        _report(), position=_position(YOUNG_TOKEN_MAX_AGE_SECONDS + 60), snipe=UNAVAILABLE
    )
    assert result["label"] == "GOOD"


def test_a_clean_launch_read_still_clears():
    clean = {"status": "clean", "buyers_within_window": 1}
    result = score_live_rugcheck_report(_report(), position=_position(600), snipe=clean)
    assert result["label"] == "GOOD"


def test_no_read_attempted_is_not_a_gap():
    result = score_live_rugcheck_report(_report(), position=_position(600), snipe=None)
    assert result["label"] == "GOOD"


def test_unknown_age_is_not_treated_as_young():
    assert not launch_gap_blocks_clean_verdict(_report(), None, UNAVAILABLE)


def test_detected_at_is_used_when_position_has_no_time():
    detected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))
    assert launch_gap_blocks_clean_verdict(_report(detectedAt=detected), None, UNAVAILABLE)


def test_known_mint_is_never_withheld():
    result = score_live_rugcheck_report(
        _report(mint=USDC, tokenMeta={"name": "USD Coin", "symbol": "USDC", "mutable": False}),
        position=_position(600), snipe=UNAVAILABLE,
    )
    assert "young_token_launch_not_readable" not in result["risk_flags"]


def test_danger_is_left_alone():
    result = score_live_rugcheck_report(
        _report(), position=_position(600, share=35.5), snipe=UNAVAILABLE
    )
    assert result["label"] != "GOOD"
    assert "young_token_launch_not_readable" not in result["risk_flags"]


def test_rugcheck_nanosecond_timestamp_parses():
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - 3600)) + ".882955376Z"
    assert launch_gap_blocks_clean_verdict(_report(detectedAt=stamp), None, UNAVAILABLE)
