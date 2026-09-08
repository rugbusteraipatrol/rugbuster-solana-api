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

def test_a_fresh_collector_row_is_still_served_normally(client_env):
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    with mock.patch.object(app, "fetch_latest_scan", return_value=_row(recent)):
        body = _get()
    assert body["label"] in {"GOOD", "WARN", "DANGER"}
    assert body["data_freshness"] == "FRESH"
    assert body["source"] == "postgres_cache"
    assert client_env.call_count == 0, "a fresh row must not trigger an upstream call"


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
