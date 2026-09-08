"""Stale evidence must not be served as a current verdict.

An independent review reproduced a `solana_scans` row dated 2025-01-01 being
returned as `GOOD 5` with zero live calls: `/score` reads the collector row
first and had no age limit on it. The one-hour TTL and `scoring_version` filter
protect a different path.

Reproducing a bug is not an acceptance test, so this file asserts the corrected
behaviour instead, and includes fresh-data controls -- if a valid recent
observation stopped being served, every "stale is withheld" test below would
still pass while the scanner had quietly stopped answering.

Both directions are covered. A stale GOOD and a stale DANGER are equally not
statements about the token today, and withholding only the reassuring one would
make our silence mean different things depending on which answer we preferred.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freshness import (  # noqa: E402
    CLOCK_SKEW_TOLERANCE,
    COLLECTOR_MAX_AGE,
    FRESH,
    INVALID,
    STALE,
    UNDATED,
    assess,
    is_servable_as_current,
    parse_timestamp,
    withhold_verdict,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def _at(**delta) -> datetime:
    return NOW - timedelta(**delta)


# --- controls: valid, recent data must still be served ---------------------

def test_a_recent_observation_is_fresh():
    state = assess(_at(hours=1), now=NOW)
    assert state["freshness"] == FRESH
    assert is_servable_as_current(state)
    assert state["age_seconds"] == 3600


def test_an_observation_from_seconds_ago_is_fresh():
    assert assess(_at(seconds=30), now=NOW)["freshness"] == FRESH


def test_the_boundary_itself_is_still_fresh():
    """Exactly at the limit is inside it; the cut must be defined, not implied."""
    assert assess(NOW - COLLECTOR_MAX_AGE, now=NOW)["freshness"] == FRESH


def test_one_second_past_the_boundary_is_stale():
    state = assess(NOW - COLLECTOR_MAX_AGE - timedelta(seconds=1), now=NOW)
    assert state["freshness"] == STALE
    assert not is_servable_as_current(state)


# --- the reproduced failure ------------------------------------------------

def test_the_reviewed_2025_row_is_stale():
    state = assess("2025-01-01T00:00:00Z", now=NOW)
    assert state["freshness"] == STALE
    assert not is_servable_as_current(state)
    assert state["age_seconds"] > 300 * 86400


# --- timestamps that cannot be trusted -------------------------------------

def test_a_missing_timestamp_is_undated_not_fresh():
    state = assess(None, now=NOW)
    assert state["freshness"] == UNDATED
    assert not is_servable_as_current(state)
    assert state["age_seconds"] is None


@pytest.mark.parametrize("value", ["", "   ", "not-a-date", "2026-13-45", 12345, [], {}])
def test_a_malformed_timestamp_is_undated_not_fresh(value):
    assert assess(value, now=NOW)["freshness"] == UNDATED


def test_a_future_timestamp_is_invalid_not_fresh():
    """A record from tomorrow is broken, and broken must not read as new."""
    state = assess(NOW + timedelta(days=1), now=NOW)
    assert state["freshness"] == INVALID
    assert not is_servable_as_current(state)


def test_small_clock_skew_is_tolerated_rather_than_called_invalid():
    state = assess(NOW + CLOCK_SKEW_TOLERANCE - timedelta(seconds=1), now=NOW)
    assert state["freshness"] == FRESH
    assert state["age_seconds"] == 0


def test_beyond_the_skew_tolerance_is_invalid():
    assert assess(NOW + CLOCK_SKEW_TOLERANCE + timedelta(minutes=1), now=NOW)["freshness"] == INVALID


# --- timezone handling -----------------------------------------------------

def test_a_naive_timestamp_is_read_as_utc():
    """Guessing a local zone here would shift every age by hours."""
    naive = datetime(2026, 9, 8, 11, 0, 0)
    assert assess(naive, now=NOW)["age_seconds"] == 3600


def test_a_non_utc_timestamp_is_converted_not_truncated():
    plus_two = datetime(2026, 9, 8, 13, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert assess(plus_two, now=NOW)["age_seconds"] == 3600


def test_the_z_suffix_is_accepted():
    assert parse_timestamp("2026-09-08T11:00:00Z") == datetime(
        2026, 9, 8, 11, 0, 0, tzinfo=timezone.utc
    )


def test_observed_at_is_reported_in_utc_iso_form():
    state = assess(_at(hours=2), now=NOW)
    assert state["observed_at"].endswith("+00:00")


# --- withholding preserves the evidence ------------------------------------

def _result(label="GOOD", score=5):
    return {"label": label, "risk_score": score, "risk_flags": ["a_flag"]}


@pytest.mark.parametrize("label,score", [("GOOD", 5), ("DANGER", 95), ("WARN", 50)])
def test_a_stale_verdict_of_any_label_is_withheld(label, score):
    state = assess("2025-01-01T00:00:00Z", now=NOW)
    out = withhold_verdict(_result(label, score), state)
    assert out["label"] == "UNKNOWN"
    assert out["risk_score"] is None


def test_the_earlier_reading_is_kept_not_discarded():
    state = assess("2025-01-01T00:00:00Z", now=NOW)
    out = withhold_verdict(_result("DANGER", 95), state)
    assert out["last_known_label"] == "DANGER"
    assert out["last_known_risk_score"] == 95


def test_the_reason_says_the_evidence_is_stale():
    state = assess("2025-01-01T00:00:00Z", now=NOW)
    out = withhold_verdict(_result(), state)
    assert "evidence_stale" in out["risk_flags"]
    assert "No current verdict" in out["note"]


def test_existing_flags_survive_withholding():
    state = assess(None, now=NOW)
    out = withhold_verdict(_result(), state)
    assert "a_flag" in out["risk_flags"]
    assert "evidence_undated" in out["risk_flags"]


def test_withholding_does_not_mutate_the_original_result():
    state = assess(None, now=NOW)
    original = _result("DANGER", 95)
    withhold_verdict(original, state)
    assert original["label"] == "DANGER"
    assert original["risk_score"] == 95
