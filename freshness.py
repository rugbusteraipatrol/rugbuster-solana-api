"""How old is the evidence behind an answer, and may it still be served?

`/score` reads the collector's `solana_scans` row first and returns it whenever
one exists, with no age limit. An independent review reproduced a row dated
2025-01-01 being served as `GOOD 5` with zero live calls. The one-hour TTL and
`scoring_version` filter live on `solana_live_cache`, a different path that is
only reached when no collector row exists at all.

Both paths can be stale in *evidence*, and both can be stale in *formula*.

An earlier version of this note claimed the collector path always recomputes
under current rules, so only its observation could age. That was wrong, and an
independent review caught it: `derive_score` uses a stored `risk_percent`
unchanged when the row carries one, and otherwise may map a stored label. So a
collector row can carry a verdict produced by whichever version wrote it, and
the running `scoring_version` did not necessarily produce the number being
served. `derive_score` now records which of the three it used --
`verdict_from_stored_risk_percent`, `verdict_recomputed_from_rugcheck_score` or
`verdict_from_stored_label` -- so provenance travels with the answer instead of
being assumed.

Two timestamps are therefore reported separately, because one row can be read
today and still describe last year:

    observed_at   when the underlying data was gathered
    fetched_at    when this response was produced

Reading an old row must never move `observed_at` forward.

A stale reading is not deleted -- it is the most recent thing we know and is
worth showing. It is simply not a current verdict, so the served label becomes
UNKNOWN while `last_known_label` keeps what was seen and when. That applies to
a stale DANGER exactly as to a stale GOOD: neither is something we can assert
about the token right now, and suppressing only the reassuring half would make
the scanner's silence mean different things in different directions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# How long a collector observation may be served as current.
COLLECTOR_MAX_AGE = timedelta(hours=24)

# The live cache's own SQL already excludes rows older than this, but it has no
# upper bound, so a row written with a future timestamp passes that filter.
# Assessing it here too keeps one definition of "current" for both paths.
LIVE_CACHE_MAX_AGE = timedelta(hours=1)

# A timestamp may sit this far in the future before it is treated as broken
# rather than as clock skew between the writer and this process.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

FRESH = "FRESH"
STALE = "STALE"
UNDATED = "UNDATED"   # no usable timestamp: age cannot be established
INVALID = "INVALID"   # a timestamp that cannot be true, e.g. far in the future

# Every state except FRESH means the age could not be established or is beyond
# the limit, and none of them may carry a verdict.
SERVABLE_AS_CURRENT = {FRESH}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    """Best-effort UTC datetime, or None when the value cannot be trusted."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Postgres and ISO 8601 both appear here; "Z" is not accepted by
        # fromisoformat before 3.11, so normalise it.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    # A naive timestamp is assumed UTC: the writers store UTC, and guessing a
    # local zone here would silently shift ages by hours.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def assess(observed_at: Any, max_age: timedelta = COLLECTOR_MAX_AGE,
           now: datetime | None = None) -> dict[str, Any]:
    """Classify one observation's freshness.

    Returns the state, the age in seconds where it could be computed, and the
    normalised `observed_at`. An unusable timestamp is never treated as recent:
    "we cannot tell how old this is" and "this is current" must not collapse
    into the same answer, which is the failure this module exists for.
    """
    reference = now or now_utc()
    parsed = parse_timestamp(observed_at)

    if parsed is None:
        return {
            "freshness": UNDATED,
            "observed_at": None,
            "age_seconds": None,
            "reason": "no usable observation timestamp on this record",
        }

    if parsed > reference + CLOCK_SKEW_TOLERANCE:
        return {
            "freshness": INVALID,
            "observed_at": parsed.isoformat(),
            "age_seconds": None,
            "reason": "observation timestamp is in the future",
        }

    age = reference - parsed
    age_seconds = max(0, int(age.total_seconds()))

    if age <= max_age:
        return {
            "freshness": FRESH,
            "observed_at": parsed.isoformat(),
            "age_seconds": age_seconds,
            "reason": "",
        }

    return {
        "freshness": STALE,
        "observed_at": parsed.isoformat(),
        "age_seconds": age_seconds,
        "reason": (
            f"observation is {age_seconds // 3600}h old; limit is "
            f"{int(max_age.total_seconds()) // 3600}h"
        ),
    }


def is_servable_as_current(state: dict[str, Any]) -> bool:
    return state.get("freshness") in SERVABLE_AS_CURRENT


def withhold_verdict(result: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Keep the historical reading, refuse to present it as a current verdict.

    The label seen at observation time is preserved under `last_known_label`
    rather than dropped: it is the most recent thing known about the token and
    a reader can act on it knowingly. What it may not do is occupy `label`,
    where an integrator would read it as what we assert today.
    """
    withheld = dict(result)
    withheld["last_known_label"] = result.get("label")
    withheld["last_known_risk_score"] = result.get("risk_score")
    withheld["label"] = "UNKNOWN"
    withheld["risk_score"] = None

    flags = list(withheld.get("risk_flags") or [])
    marker = f"evidence_{state.get('freshness', 'STALE').lower()}"
    if marker not in flags:
        flags.append(marker)
    withheld["risk_flags"] = flags

    withheld["note"] = (
        "No current verdict: " + (state.get("reason") or "evidence is not current")
        + ". last_known_label describes an earlier observation, not the token today."
    )
    return withheld
