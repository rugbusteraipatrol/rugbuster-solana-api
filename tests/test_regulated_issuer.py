"""A tokenized stock's issuer controls are expected, and still capped at WARN, not cleared."""

from scoring import score_live_rugcheck_report

BACKPACK = "2cVYpagTt7ZGc3mmTXBa7fAznUtx5DUu6aCq8uVDaf4a"
SHOP = "SH55hfaipFAbwT42nQYhRoM5o5t61QpkmJ6p62vXB3m"
RISKS = [
    {"name": "Mint Authority still enabled", "score": 50000},
    {"name": "Permanent Control Enabled", "score": 50000},
    {"name": "Freeze Authority still enabled", "score": 25000},
    {"name": "Low Liquidity", "score": 3000},
    {"name": "Mutable metadata", "score": 100},
]


def report(authority=BACKPACK, delegate=BACKPACK, risks=RISKS):
    return {
        "mint": SHOP,
        "score": 128101,
        "score_normalised": 81,
        "rugged": False,
        "token": {"mintAuthority": "CdiZcEvWcCs2hbUzW9x3ZpBftbPNwm2W3QrY5ZuyupK8", "freezeAuthority": authority},
        "token_extensions": {"permanentDelegate": {"delegate": delegate}},
        "tokenMeta": {"symbol": "SHOP", "name": "Shopify - Backpack Securities", "mutable": True},
        "risks": risks,
        "topHolders": [],
        "totalHolders": 1476,
        "totalMarketLiquidity": 100190,
    }


def sold(share=30.0):
    return {
        "status": "sold", "creator": "C" * 43, "share_at_creation_pct": share, "peak_share_pct": share,
        "current_share_pct": 0.0, "sold_pct_of_peak": 100.0, "sold_after_seconds": 20,
        "observed_at": "2026-10-05T10:00:00+00:00", "source": "solana_rpc",
    }


def test_backpack_stock_is_warn_not_danger():
    scored = score_live_rugcheck_report(report())
    assert scored["label"] == "WARN"
    assert "regulated_issuer_controls_capped_at_warn" in scored["risk_flags"]
    # the powers are still reported
    assert "permanent_control_enabled" in scored["risk_flags"]


def test_recognition_never_clears_a_token():
    assert score_live_rugcheck_report(report())["label"] != "GOOD"


def test_unknown_authority_stays_danger():
    other = "9" * 44
    scored = score_live_rugcheck_report(report(authority=other, delegate=other))
    assert scored["label"] == "DANGER"
    assert "regulated_issuer_recognised" not in scored["risk_flags"]


def test_freeze_and_delegate_must_both_be_the_issuer():
    scored = score_live_rugcheck_report(report(delegate="9" * 44))
    assert scored["label"] == "DANGER"


def test_a_creator_exit_still_goes_past_warn():
    scored = score_live_rugcheck_report(report(), position=sold())
    assert scored["label"] == "DANGER"
