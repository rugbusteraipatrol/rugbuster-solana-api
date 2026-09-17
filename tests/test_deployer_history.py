"""A refresh must not throw away what the collector knew about the deployer.

The 17 creator-exit-confirmed pump.fun mints sat in `solana_scans` as DANGER
with `v6_serial_rug_count` and `creator_rug_rate` recorded beside the verdict.
Once those rows aged past the freshness limit, `/score` refreshed them from a
live RugCheck report -- which knows nothing about the creator -- and answered
WARN with the note "what this deployer's previous tokens did" listed as not
established. The knowledge was in the row the service had just read.

These pin that the history now travels across the refresh, on both branches,
under the same floors `derive_score` applies to a stored row; that it is
reported as a finding rather than a gap; and that a row with no history is
unchanged, because an absence must stay an absence.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from evidence import NOT_COLLECTED, OK, creator_history  # noqa: E402
from plain_language import FINDING, REFUSAL, describe  # noqa: E402
from scoring import (  # noqa: E402
    apply_deployer_history,
    deployer_history_from_record,
    score_live_rugcheck_report,
)

MINT = "CXRhJrBcCk1m83soxiGjY1AwiY6awAxhua6zPZMqpump"
CREATOR = "126kHRLgCJeGqU8Ye7CkhmqnVxTrgb7dY1DQbgNsLD7x"

# The shape the collector writes, trimmed to the fields that matter here.
COLLECTOR_RECORD = {
    "mint": MINT,
    "chain": "SOLANA",
    "label": "DANGER",
    "risk_percent": 85,
    "creator": CREATOR,
    "creator_rug_rate": 100.0,
    "v6_is_serial_rugger": True,
    "v6_serial_rug_count": 3,
    "cia_funding_hops": 0,
}

# A dead pump.fun mint as RugCheck reports it: floor score, two holders, no
# liquidity. On its own this path refuses to clear it and stops at WARN.
DEAD_MINT_REPORT = {
    "mint": MINT,
    "score": 1,
    "score_normalised": 1,
    "totalHolders": 2,
    "totalMarketLiquidity": 0,
    "rugged": False,
    "token": {},
    "tokenMeta": {"name": "AHEGAO", "symbol": "AHEGAO"},
    "risks": [],
}


def _stale_row(record=COLLECTOR_RECORD):
    return {
        "contract_address": MINT,
        "chain": "solana",
        "label": record.get("label", "DANGER"),
        "full_record": record,
        "created_at": "2026-07-17T22:43:13Z",
    }


def _get():
    return app.app.test_client().get("/score", query_string={"address": MINT}).get_json()


# --- reading the history out of a row ---------------------------------------

def test_history_is_read_from_the_collector_record():
    """count=3 on a DANGER row: the collector counted this token too, so two
    earlier ones are on record."""
    history = deployer_history_from_record(COLLECTOR_RECORD)
    assert history == {
        "creator": CREATOR,
        "prior_rugs_on_record": 2,
        "creator_rug_rate": 100.0,
        "funding_hops": 0,
        "serial_rugger": True,
    }


def test_a_verdict_may_not_cite_itself_as_its_own_history():
    """count=1 on a DANGER row is this token and nothing earlier. Seven of the
    seventeen benchmark rows look exactly like this; their DANGER came from a
    funding-chain signal, which is not a prior rug and gets no floor here."""
    history = deployer_history_from_record({
        "creator": CREATOR, "label": "DANGER", "v6_serial_rug_count": 1,
        "creator_rug_rate": 0.0, "cia_funding_hops": 2,
    })
    assert history["prior_rugs_on_record"] == 0
    assert history["serial_rugger"] is False
    assert history["funding_hops"] == 2
    risk, flags = apply_deployer_history(50, [], history)
    assert (risk, flags) == (50, [])


def test_a_warn_row_does_not_subtract_itself():
    history = deployer_history_from_record({"creator": CREATOR, "label": "WARN", "v6_serial_rug_count": 1})
    assert history["prior_rugs_on_record"] == 1


def test_a_row_with_nothing_about_the_creator_yields_no_history():
    """An absence must stay an absence, not become an empty record."""
    assert deployer_history_from_record({"risk_percent": 5}) is None
    assert deployer_history_from_record({}) is None
    assert deployer_history_from_record("not a record") is None


# --- the floors, shared with the stored path --------------------------------

def test_history_floors_match_the_stored_path_thresholds():
    risk, flags = apply_deployer_history(1, [], {"creator_rug_rate": 100.0, "prior_rugs_on_record": 3})
    assert risk == 85
    assert {"creator_rug_rate_high", "creator_history_of_rugged_tokens"} <= set(flags)

    risk, flags = apply_deployer_history(1, [], {"creator_rug_rate": 50.0, "prior_rugs_on_record": 0})
    assert risk == 70
    assert "creator_rug_rate_elevated" in flags
    assert "creator_history_of_rugged_tokens" not in flags


def test_history_is_a_floor_and_never_lowers_a_verdict():
    risk, _ = apply_deployer_history(95, [], {"creator_rug_rate": 100.0, "prior_rugs_on_record": 3})
    assert risk == 95


def test_no_history_leaves_the_score_alone():
    assert apply_deployer_history(42, ["x"], None) == (42, ["x"])


# --- the live scorer ---------------------------------------------------------

def test_a_dead_mint_alone_is_a_refusal_at_warn():
    scored = score_live_rugcheck_report(DEAD_MINT_REPORT)
    assert scored["label"] == "WARN"
    assert "live_scan_cannot_clear_token" in scored["risk_flags"]


def test_the_same_report_with_a_history_of_rugs_is_danger():
    history = deployer_history_from_record(COLLECTOR_RECORD)
    scored = score_live_rugcheck_report(DEAD_MINT_REPORT, history=history)
    assert scored["label"] == "DANGER"
    assert scored["risk_score"] >= 85
    assert "creator_history_of_rugged_tokens" in scored["risk_flags"]
    # The refusal is not reached: there is now a finding to rest on.
    assert "live_scan_cannot_clear_token" not in scored["risk_flags"]


# --- what the reader is told -------------------------------------------------

def test_the_evidence_dimension_reports_the_history_as_collected():
    payload = {"label": "DANGER", "risk_flags": [], "deployer_history": {
        "creator": CREATOR, "prior_rugs_on_record": 3, "creator_rug_rate": 100.0,
        "funding_hops": 0, "serial_rugger": True, "source": "collector_record",
        "observed_at": "2026-07-17T22:43:13+00:00",
    }}
    result = creator_history(payload)
    assert result["status"] == OK
    assert result["creator"] == CREATOR
    assert result["prior_rugs_on_record"] == 3
    assert result["confirmed_incidents"] == {"count": 3, "status": OK}


def test_without_a_history_block_the_dimension_stays_uncollected():
    assert creator_history({"label": "WARN", "risk_flags": []})["status"] == NOT_COLLECTED


def test_the_headline_is_a_finding_about_the_deployer_not_a_refusal():
    payload = {
        "label": "DANGER",
        "risk_flags": ["creator_history_of_rugged_tokens", "creator_rug_rate_high"],
        "deployer_history": {"prior_rugs_on_record": 3, "creator_rug_rate": 100.0},
        "evidence": {"creator_history": {"status": OK, "confirmed_incidents": {"count": 3, "status": OK}}},
    }
    out = describe(payload)
    assert out["verdict_basis"] == FINDING
    assert "3 earlier tokens by this creator rugged" in out["verdict_summary"]
    # `prior_rugs_on_record` is already net of the token itself; the sentence
    # repeats the number and does not subtract again.
    assert "what this deployer's previous tokens did" not in out["not_established"]


def test_a_refusal_without_history_is_still_a_refusal():
    out = describe({"label": "WARN", "risk_flags": ["live_scan_cannot_clear_token"], "evidence": {}})
    assert out["verdict_basis"] == REFUSAL


# --- /score end to end: the stale row, refreshed -----------------------------

def test_a_stale_danger_row_refreshed_from_upstream_keeps_its_deployer_history():
    with mock.patch.object(app, "fetch_latest_scan", return_value=_stale_row()), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=None), \
         mock.patch.object(app, "request_live_rugcheck", return_value=DEAD_MINT_REPORT), \
         mock.patch.object(app, "insert_live_cache"):
        body = _get()
    assert body["source"] == "live_rugcheck_refresh"
    assert body["label"] == "DANGER"
    assert "creator_history_of_rugged_tokens" in body["risk_flags"]
    assert body["deployer_history"]["creator"] == CREATOR
    assert body["deployer_history"]["prior_rugs_on_record"] == 2
    assert body["deployer_history"]["observed_at"].startswith("2026-07-17")
    assert body["evidence"]["creator_history"]["status"] == OK
    assert body["verdict_basis"] == FINDING
    assert "what this deployer's previous tokens did" not in body["not_established"]


def test_the_cached_live_branch_applies_the_same_history():
    """A live row cached from a direct fetch was scored with no history in
    hand. Served as the refresh of a stale collector row, it gets the same
    floors as the network branch would, so the two cannot disagree."""
    cached = {
        "contract_address": MINT, "risk_score": 50, "label": "WARN",
        "rugcheck_score": 1, "risk_flags": ["live_scan_cannot_clear_token", "too_few_holders_to_clear"],
        "token_name": "AHEGAO", "token_symbol": "AHEGAO",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=_stale_row()), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached), \
         mock.patch.object(app, "request_live_rugcheck") as upstream:
        body = _get()
    assert upstream.call_count == 0
    assert body["source"] == "live_cache_refresh"
    assert body["label"] == "DANGER"
    assert body["risk_score"] >= 85
    assert body["evidence"]["creator_history"]["status"] == OK
    assert body["verdict_basis"] == FINDING


def test_a_stale_row_with_no_creator_data_refreshes_exactly_as_before():
    """The control: no history in the row, so the refusal stands and the
    dimension stays uncollected."""
    plain = {"risk_percent": 85, "label": "DANGER"}
    with mock.patch.object(app, "fetch_latest_scan", return_value=_stale_row(plain)), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=None), \
         mock.patch.object(app, "request_live_rugcheck", return_value=DEAD_MINT_REPORT), \
         mock.patch.object(app, "insert_live_cache"):
        body = _get()
    assert body["label"] == "WARN"
    assert "live_scan_cannot_clear_token" in body["risk_flags"]
    assert "deployer_history" not in body
    assert body["evidence"]["creator_history"]["status"] == NOT_COLLECTED
    assert body["verdict_basis"] == REFUSAL


def test_a_current_row_carries_the_history_block_for_the_evidence_split():
    """No verdict change here -- derive_score already floors a stored row on
    creator_rug_rate -- but the evidence split must say the history exists."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    row = _stale_row({**COLLECTOR_RECORD, "scoring_version": app.SCORING_VERSION})
    row["created_at"] = recent
    with mock.patch.object(app, "fetch_latest_scan", return_value=row), \
         mock.patch.object(app, "request_live_rugcheck") as upstream:
        body = _get()
    assert upstream.call_count == 0
    assert body["source"] == "postgres_cache"
    assert body["label"] == "DANGER"
    assert body["evidence"]["creator_history"]["status"] == OK
    assert body["evidence"]["creator_history"]["prior_rugs_on_record"] == 2
