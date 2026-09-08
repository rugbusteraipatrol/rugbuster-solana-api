"""`/score` end to end: what a caller actually receives.

The unit tests in `test_freshness.py` pin the age logic. These pin the wiring,
which is where the original defect lived: the logic for expiry existed, on the
live-cache path, and the collector path simply never consulted it.

The named case from the independent review is `test_the_reviewed_stale_row_is_no_longer_served_as_good`.
Its probe asserted `label == "GOOD"` and `live_calls == 0`; both must now be false.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

MINT = "So11111111111111111111111111111111111111112"


def _row(created_at, label="GOOD", risk=5):
    return {
        "contract_address": MINT,
        "chain": "solana",
        "label": label,
        "full_record": {"risk_percent": risk},
        "created_at": created_at,
    }


def _get():
    return app.app.test_client().get("/score", query_string={"address": MINT}).get_json()


@pytest.fixture
def client_env():
    """No live upstream unless a test supplies one."""
    with mock.patch.object(app, "request_live_rugcheck", return_value=None) as live:
        yield live


# --- the reviewed failure, now corrected -----------------------------------

def test_the_reviewed_stale_row_is_no_longer_served_as_good(client_env):
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["last_known_label"] == "GOOD"
    assert body["data_freshness"] == "STALE"
    assert client_env.call_count == 1, "a stale row must at least attempt a refresh"


def test_a_stale_danger_is_withheld_too(client_env):
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z", "DANGER", 95)):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["last_known_label"] == "DANGER"


# --- control: fresh data must still be served ------------------------------

def _versioned_row(created_at, label="GOOD", risk=5):
    """A row whose stored number names the rules that produced it."""
    row = _row(created_at, label, risk)
    row["full_record"] = {**row["full_record"], "scoring_version": app.SCORING_VERSION}
    return row


def test_a_fresh_row_from_the_current_rules_is_served_normally(client_env):
    """The control: recent evidence AND a named, matching provenance.

    A recent date alone no longer qualifies. The stored number was produced by
    whichever rules wrote the row, and a fresh timestamp says nothing about
    which those were.
    """
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_versioned_row(recent)):
        body = _get()
    assert body["label"] in {"GOOD", "WARN", "DANGER"}
    assert body["data_freshness"] == "FRESH"
    assert body["verdict_provenance"] == "current_rules"
    assert body["source"] == "postgres_cache"
    assert client_env.call_count == 0, "a fresh, versioned row must not trigger an upstream call"


def test_a_row_from_a_different_scoring_version_is_not_served_as_current(client_env):
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    row = _row(recent)
    row["full_record"] = {**row["full_record"], "scoring_version": "1999.01.1"}
    with mock.patch.object(app, "fetch_latest_scan", return_value=row):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["source_scoring_version"] == "1999.01.1"


def test_an_unversioned_score_is_recomputed_when_the_evidence_allows_it(client_env):
    """Preferred over refusing: the raw signals are in hand, so apply current
    rules to them rather than inherit a number of unknown origin."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    row = _row(recent)
    row["full_record"] = {
        "risk_percent": 5,
        "rugcheck_score": 900,
        "totalHolders": 4000,
        "total_holders": 4000,
        "liquidity_usd": 250_000,
        "token_age_days": 200,
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=row):
        body = _get()
    assert body["verdict_provenance"] == "recomputed_from_raw_evidence"
    assert body["label"] != "UNKNOWN"
    assert "stored_score_ignored_unknown_provenance" in body["risk_flags"]
    assert client_env.call_count == 0


def test_an_unversioned_score_with_no_usable_evidence_is_withheld(client_env):
    """Recent date, no provenance, nothing to recompute from: refresh, then
    withhold. A fresh timestamp does not make an unattributable number current."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(recent)):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["last_known_label"] == "GOOD"
    assert body["data_freshness"] == "FRESH", "the evidence is current; the number's origin is not"
    assert body["verdict_provenance"] == "inherited_unknown_version"
    assert client_env.call_count == 1, "a refresh must be attempted before withholding"


def test_an_inherited_label_is_treated_like_an_inherited_number(client_env):
    """With no score and no evidence, derive_score maps the stored label -- the
    same inheritance, in a different field."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    row = _row(recent)
    row["full_record"] = {}
    with mock.patch.object(app, "fetch_latest_scan", return_value=row):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["verdict_provenance"] == "inherited_unknown_version"


# --- refreshing a stale row ------------------------------------------------

def test_a_stale_row_is_replaced_by_a_live_reading_when_one_is_available():
    report = {
        "mint": MINT,
        "score": 1,
        "score_normalised": 1,
        "totalHolders": 500000,
        "totalMarketLiquidity": 5_000_000,
        "rugged": False,
        "token": {},
        "tokenMeta": {"name": "Wrapped SOL", "symbol": "WSOL"},
        "risks": [],
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")), \
         mock.patch.object(app, "request_live_rugcheck", return_value=report), \
         mock.patch.object(app, "insert_live_cache"):
        body = _get()
    assert body["source"] == "live_rugcheck_refresh"
    assert body["data_freshness"] == "FRESH"
    assert body["label"] != "UNKNOWN"
    assert body["refreshed_stale_record_observed_at"].startswith("2025-01-01")


def test_a_refresh_that_times_out_withholds_rather_than_serving_the_old_verdict():
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")), \
         mock.patch.object(app, "request_live_rugcheck", side_effect=TimeoutError("upstream")):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["data_freshness"] == "STALE"


def test_an_empty_upstream_response_withholds_rather_than_clearing_the_token():
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")), \
         mock.patch.object(app, "request_live_rugcheck", return_value=None):
        body = _get()
    assert body["label"] == "UNKNOWN"


# --- timestamps that cannot be trusted -------------------------------------

@pytest.mark.parametrize("stamp", [None, "", "not-a-date"])
def test_an_undated_row_is_not_served_as_current(client_env, stamp):
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(stamp)):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["data_freshness"] == "UNDATED"


def test_a_future_dated_row_is_not_served_as_current(client_env):
    future = datetime.now(timezone.utc) + timedelta(days=2)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(future)):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["data_freshness"] == "INVALID"


# --- observed_at vs fetched_at ---------------------------------------------

def test_reading_an_old_row_does_not_give_it_a_fresh_observation_time(client_env):
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")):
        body = _get()
    assert body["observed_at"].startswith("2025-01-01")
    assert body["fetched_at"] > body["observed_at"]


def test_a_fresh_row_reports_both_times_distinctly(client_env):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(recent)):
        body = _get()
    assert body["observed_at"] < body["fetched_at"]
    assert 0 <= body["age_seconds"] <= 3600


# --- build identity travels with every answer ------------------------------

def test_every_response_carries_build_identity(client_env):
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(recent)):
        body = _get()
    assert body["scoring_version"] == app.SCORING_VERSION
    assert body["build_commit"]


def test_health_carries_build_identity():
    with mock.patch.object(app, "db_connect", mock.MagicMock()):
        body = app.app.test_client().get("/health").get_json()
    assert body["scoring_version"] == app.SCORING_VERSION
    assert "build_commit" in body


# --- defects found by independent review of 96d607f2 ------------------------

def test_a_stale_row_prefers_a_valid_live_cache_over_another_upstream_call():
    """The stale collector row does not go away, so every later request would
    otherwise re-fetch the same token. Reviewed as P1."""
    cached = {
        "contract_address": MINT,
        "risk_score": 30,
        "label": "GOOD",
        "rugcheck_score": 900,
        "risk_flags": ["mutable_metadata"],
        "token_name": "Cached",
        "token_symbol": "C",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached), \
         mock.patch.object(app, "request_live_rugcheck") as upstream:
        body = _get()
    assert body["source"] == "live_cache_refresh"
    assert body["label"] == "GOOD"
    assert upstream.call_count == 0, "a valid cached reading must be used before the network"


def test_repeated_requests_on_a_stale_row_do_not_repeat_the_upstream_call():
    cached = {
        "contract_address": MINT, "risk_score": 30, "label": "GOOD",
        "rugcheck_score": 900, "risk_flags": [], "token_name": "C", "token_symbol": "C",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row("2025-01-01T00:00:00Z")), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached), \
         mock.patch.object(app, "request_live_rugcheck") as upstream:
        _get(); _get(); _get()
    assert upstream.call_count == 0


def test_a_live_cache_hit_carries_identity_and_both_timestamps():
    """Reviewed as P2: only one success path had been covered."""
    cached = {
        "contract_address": MINT, "risk_score": 46, "label": "WARN",
        "rugcheck_score": 600, "risk_flags": ["mutable_metadata"],
        "token_name": "Cached", "token_symbol": "C",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=None), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached):
        body = _get()
    for field in ("build_commit", "scoring_version", "observed_at", "fetched_at", "data_freshness"):
        assert field in body, f"live-cache response is missing {field}"


def test_a_cache_miss_carries_identity():
    with mock.patch.object(app, "fetch_latest_scan", return_value=None), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=None), \
         mock.patch.object(app, "request_live_rugcheck", return_value=None):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["build_commit"] and body["scoring_version"]


def test_an_expired_live_cache_row_is_withheld_not_served():
    """The cache SQL has no upper time bound, so the row is aged here too."""
    cached = {
        "contract_address": MINT, "risk_score": 5, "label": "GOOD",
        "rugcheck_score": 1, "risk_flags": [], "token_name": "Old", "token_symbol": "O",
        "created_at": datetime.now(timezone.utc) - timedelta(days=30),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=None), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached):
        body = _get()
    assert body["label"] == "UNKNOWN"
    assert body["last_known_label"] == "GOOD"


def test_a_future_dated_live_cache_row_is_withheld():
    cached = {
        "contract_address": MINT, "risk_score": 5, "label": "GOOD",
        "rugcheck_score": 1, "risk_flags": [], "token_name": "F", "token_symbol": "F",
        "created_at": datetime.now(timezone.utc) + timedelta(days=1),
    }
    with mock.patch.object(app, "fetch_latest_scan", return_value=None), \
         mock.patch.object(app, "ensure_live_cache_schema"), \
         mock.patch.object(app, "fetch_live_cache", return_value=cached):
        body = _get()
    assert body["data_freshness"] == "INVALID"
    assert body["label"] == "UNKNOWN"


def test_a_served_verdict_always_names_where_its_number_came_from():
    """Reviewed correction, taken further: a flag saying the number was
    inherited does not make it current, so the response also carries the source
    version and the provenance of the verdict it is serving."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_versioned_row(recent, "DANGER", 85)):
        body = _get()
    assert body["verdict_provenance"] == "current_rules"
    assert body["source_scoring_version"] == app.SCORING_VERSION
    assert "verdict_from_stored_risk_percent" in body["risk_flags"]
