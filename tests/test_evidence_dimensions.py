"""The dimensions must answer only their own question, in this service's terms.

Step 3, additive: it separates what the scanner claims without changing any
verdict. Most of what follows is about what must *not* happen -- one dimension
softening another, a missing reading defaulting to something reassuring, or an
absent capability being reported as an empty record rather than an absent one.

The Avalanche service has its own copy. They are deliberately not shared code:
`withhold_verdict` was written here, reused there against a differently-shaped
response, and blanked only the fields its author had in mind. Every dimension
below therefore reads Solana's own flag vocabulary.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence import (  # noqa: E402
    NOT_COLLECTED,
    OK,
    UNKNOWN,
    build_evidence,
    coverage,
    creator_history,
    distribution,
    issuer_identity,
    market,
    technical_controls,
)

MINT = "So11111111111111111111111111111111111111112"


def _payload(**overrides):
    payload = {
        "label": "WARN",
        "risk_score": 55,
        "risk_flags": [
            "mint_authority_active",
            "freeze_authority_active",
            "mutable_metadata",
            "single_holder_ownership",
            "verdict_recomputed_from_rugcheck_score",
        ],
        "source": "live_rugcheck",
        "data_freshness": "FRESH",
        "observed_at": "2026-09-08T12:00:00+00:00",
        "fetched_at": "2026-09-08T12:00:01+00:00",
        "age_seconds": 0,
    }
    payload.update(overrides)
    return payload


def _report(**overrides):
    report = {
        "mint": MINT,
        "totalHolders": 557_133,
        "totalMarketLiquidity": 20_652_531.0,
        "token": {"mintAuthority": "Auth", "freezeAuthority": None},
        "rugged": False,
    }
    report.update(overrides)
    return report


# --- the step's constraint -------------------------------------------------

VERDICT_FIELDS = ("label", "risk_score")


def test_building_evidence_changes_no_verdict_field():
    payload = _payload()
    before = {field: payload[field] for field in VERDICT_FIELDS}
    build_evidence(payload)
    assert {field: payload[field] for field in VERDICT_FIELDS} == before


def test_building_evidence_does_not_mutate_the_payload():
    payload = _payload()
    snapshot = repr(sorted(payload.items(), key=str))
    build_evidence(payload, _report())
    assert repr(sorted(payload.items(), key=str)) == snapshot


# --- technical controls ----------------------------------------------------

def test_authorities_are_read_from_this_services_own_flags():
    result = technical_controls(_payload())
    assert result["active_authorities"] == ["freeze", "mint"]


def test_rugcheck_wording_is_recognised_too():
    """The report's own risk names differ from ours and both must count."""
    payload = _payload(risk_flags=["mint_authority_still_enabled", "freeze_authority_still_enabled"])
    assert technical_controls(payload)["active_authorities"] == ["freeze", "mint"]


def test_authorities_are_reported_for_a_recognised_mint_too():
    """Identity contextualises an authority; it must not cancel it."""
    payload = _payload(risk_flags=["known_canonical_solana_mint", "mint_authority_still_enabled"])
    assert "mint" in technical_controls(payload)["active_authorities"]
    assert issuer_identity(payload)["recognised"] is True


def test_no_flags_and_no_account_is_unknown_not_clean():
    """Absence of a flag does not prove absence of an authority."""
    result = technical_controls(_payload(risk_flags=[]))
    assert result["status"] == UNKNOWN
    assert result["active_authorities"] == []
    assert set(result["unread_authorities"]) >= {"mint", "freeze"}


def test_reading_the_account_records_coverage_per_authority():
    """`read_from_account` was one boolean for the whole account, which is what
    let a token dict with only `decimals` report itself as checked. Coverage is
    per authority now."""
    result = technical_controls(_payload(risk_flags=[]), _report())
    assert result["active_authorities"] == ["mint"]
    assert result["revoked_authorities"] == ["freeze"]
    assert "update" in result["unread_authorities"]
    assert result["status"] == PARTIAL


# --- distribution ----------------------------------------------------------

def test_concentration_is_reported_separately_from_authorities():
    result = distribution(_payload())
    assert result["concentration_signals"] == ["single_holder_ownership"]
    assert "single_holder_ownership" not in str(technical_controls(_payload()))


def test_ownership_is_never_claimed_to_be_established():
    """The assumption that made PR #4 wrong: top holders taken to be pools."""
    result = distribution(_payload(), _report())
    assert result["owners_identified"] is False
    assert "does not make concentration benign" in result["note"]


def test_a_large_holder_count_does_not_clear_concentration():
    result = distribution(_payload(), _report(totalHolders=5_000_000))
    assert result["concentration_signals"], "concentration must survive a big holder count"


# --- market ----------------------------------------------------------------

def test_market_is_unknown_without_the_raw_report():
    """A cached verdict does not carry these figures, and must not invent them."""
    result = market(_payload())
    assert result["status"] == UNKNOWN
    assert result["liquidity_usd"] is None


def test_market_is_reported_when_the_report_is_present():
    result = market(_payload(), _report())
    assert result["liquidity_usd"] == 20_652_531.0
    assert result["source"] == "rugcheck"


# --- issuer identity -------------------------------------------------------

def test_an_unrecognised_mint_is_unknown_not_an_accusation():
    result = issuer_identity(_payload())
    assert result["status"] == UNKNOWN
    assert "not an accusation" in result["note"]


def test_size_is_not_identity():
    huge = _report(totalHolders=9_000_000, totalMarketLiquidity=10**9)
    assert issuer_identity(_payload(), huge)["recognised"] is False


# --- creator history: absent capability, stated as such --------------------

def test_creator_history_is_reported_as_uncollected_not_empty():
    """This service has no deployer lookup at all.

    A creator with a long record and one nobody has seen produce the same
    answer here. Reporting an empty record would present that as a clean one.
    """
    result = creator_history(_payload())
    assert result["status"] == NOT_COLLECTED
    assert result["prior_tokens_scanned_by_us"] is None
    assert result["confirmed_incidents"]["count"] is None


# --- coverage --------------------------------------------------------------

def test_coverage_carries_both_timestamps_and_provenance():
    result = coverage(_payload())
    assert result["observed_at"].startswith("2026-09-08")
    assert result["fetched_at"] > result["observed_at"]
    assert result["verdict_provenance"] == "verdict_recomputed_from_rugcheck_score"


def test_an_inherited_verdict_is_visible_in_coverage():
    payload = _payload(risk_flags=["verdict_from_stored_risk_percent"])
    assert coverage(payload)["verdict_provenance"] == "verdict_from_stored_risk_percent"


def test_missing_freshness_is_unknown():
    payload = _payload()
    del payload["data_freshness"]
    assert coverage(payload)["status"] == UNKNOWN


# --- the whole block -------------------------------------------------------

@pytest.mark.parametrize(
    "dimension",
    ["technical_controls", "distribution", "market", "issuer_identity", "creator_history", "coverage"],
)
def test_an_empty_payload_never_produces_a_reassuring_dimension(dimension):
    assert build_evidence({})[dimension]["status"] != OK


# --- wiring ----------------------------------------------------------------

def _get():
    import app

    return app.app.test_client().get("/score", query_string={"address": MINT}).get_json()


def test_the_endpoint_carries_evidence_without_moving_the_verdict():
    import app

    row = {
        "contract_address": MINT,
        "chain": "solana",
        "label": "WARN",
        # Versioned, so the row is served rather than withheld for unknown
        # provenance -- this test is about the evidence split, not the ladder.
        "full_record": {"risk_percent": 55, "scoring_version": app.SCORING_VERSION},
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=row):
        body = _get()

    assert body["label"] == "WARN"
    assert body["risk_score"] == 55
    assert set(body["evidence"]) == {
        "technical_controls", "distribution", "market", "issuer_identity",
        "creator_history", "coverage",
    }
    assert body["evidence"]["creator_history"]["status"] == NOT_COLLECTED
    assert body["evidence"]["coverage"]["data_freshness"] == "FRESH"


def test_a_withheld_response_still_explains_itself():
    """A withheld verdict is exactly when a reader needs the dimensions."""
    import app

    row = {
        "contract_address": MINT,
        "chain": "solana",
        "label": "GOOD",
        "full_record": {"risk_percent": 5},
        "created_at": "2025-01-01T00:00:00Z",
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=row), \
         mock.patch.object(app, "request_live_rugcheck", return_value=None):
        body = _get()

    assert body["label"] == "UNKNOWN"
    assert body["evidence"]["coverage"]["data_freshness"] == "STALE"


# --- authority presence: missing, explicit null and address are three things

from evidence import ABSENT, PARTIAL, PRESENT, authority_readings  # noqa: E402


def test_a_partial_token_object_does_not_verify_absence():
    """A token dict holding only `decimals` said nothing about any authority.
    Reporting the account as read because the dict was non-empty is how "we did
    not check" became "there is nothing there"."""
    result = technical_controls({"risk_flags": []}, {"token": {"decimals": 9}})
    assert result["status"] == UNKNOWN
    assert result["active_authorities"] == []
    assert set(result["unread_authorities"]) >= {"mint", "freeze"}


def test_an_explicit_raw_authority_survives_without_a_flag():
    """The address is in the account. No flag mentioning it does not erase it."""
    result = technical_controls(
        {"risk_flags": ["known_canonical_solana_mint"]},
        {"token": {"mintAuthority": "AuthorityA", "freezeAuthority": None}},
    )
    assert "mint" in result["active_authorities"]
    assert "freeze" in result["revoked_authorities"]


def test_explicit_null_reads_as_revoked_not_unknown():
    readings = authority_readings({"risk_flags": []}, {"token": {"mintAuthority": None}})
    assert readings["mint"] == ABSENT


def test_a_missing_field_reads_as_unknown_not_revoked():
    readings = authority_readings({"risk_flags": []}, {"token": {"decimals": 9}})
    assert readings["mint"] == UNKNOWN
    assert readings["freeze"] == UNKNOWN


def test_an_address_reads_as_present():
    readings = authority_readings({"risk_flags": []}, {"token": {"mintAuthority": "Abc"}})
    assert readings["mint"] == PRESENT


def test_a_flag_adds_positive_evidence_where_the_account_was_not_read():
    readings = authority_readings({"risk_flags": ["mint_authority_still_enabled"]}, None)
    assert readings["mint"] == PRESENT
    assert readings["freeze"] == UNKNOWN, "no flag is not a denial"


def test_a_flag_wins_over_a_contradicting_null():
    """A contradiction is not a reason to report the reassuring half."""
    readings = authority_readings(
        {"risk_flags": ["mint_authority_active"]}, {"token": {"mintAuthority": None}}
    )
    assert readings["mint"] == PRESENT


def test_partial_coverage_is_reported_as_partial():
    result = technical_controls({"risk_flags": []}, {"token": {"mintAuthority": "Abc"}})
    assert result["status"] == PARTIAL
    assert result["active_authorities"] == ["mint"]
    assert "freeze" in result["unread_authorities"]


def test_full_coverage_reports_ok():
    token = {"mintAuthority": None, "freezeAuthority": None, "updateAuthority": None}
    result = technical_controls(
        {"risk_flags": ["balance_mutable_authority", "non_transferable"]}, {"token": token}
    )
    assert result["status"] == OK
    assert result["unread_authorities"] == []


def test_an_unread_authority_is_never_listed_as_revoked():
    result = technical_controls({"risk_flags": []}, {"token": {"mintAuthority": "Abc"}})
    assert set(result["revoked_authorities"]).isdisjoint(result["unread_authorities"])


def test_coverage_reports_retrieval_and_observation_separately():
    """Same vocabulary as the Avalanche service, so one contract reads the same
    on both: observed_at is null when no upstream gives us one."""
    payload = _payload(
        observed_at=None,
        retrieved_at="2026-09-08T12:00:00+00:00",
        fetched_at="2026-09-08T12:00:01+00:00",
        observation_coverage="RETRIEVAL_TIME_ONLY",
        age_seconds=None,
    )
    result = coverage(payload)
    assert result["observed_at"] is None
    assert result["retrieved_at"].startswith("2026-09-08")
    assert result["observation_coverage"] == "RETRIEVAL_TIME_ONLY"
    assert result["age_seconds"] is None
