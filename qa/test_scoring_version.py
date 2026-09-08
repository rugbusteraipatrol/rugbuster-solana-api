"""Changing the scoring rules must change the scoring version.

`solana_live_cache` rows are served only when their stored `scoring_version`
matches the running one. That filter is the only thing stopping a verdict
computed by the previous formula from being served after a deploy -- so a
scoring change that leaves the version alone keeps the old rows valid for the
rest of their TTL. That is exactly what PR #4 did: it changed
`score_live_rugcheck_report` and left `SCORING_VERSION` at 2026.09.1.

This test fingerprints the functions that decide a verdict and pins the
fingerprint against the version. Editing any of them without bumping
`SCORING_VERSION` fails here, with the instruction in the failure message.

It deliberately hashes source text rather than behaviour: a behavioural test
only catches the cases someone thought to write, and the thing being guarded
against is precisely a change nobody thought about.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scoring  # noqa: E402

# Every function whose output can change a served verdict.
SCORING_SURFACE = (
    "derive_score",
    "score_scan_row",
    "score_live_rugcheck_report",
    "live_report_supports_clean_verdict",
    "rugcheck_to_risk",
)

# Bump together with SCORING_VERSION. Get the new value from the failure
# message; do not copy it from a passing run of an unreviewed change.
EXPECTED = {
    "version": "2026.09.1",
    "fingerprint": "5b2ac63fbb1371cc",
}


def scoring_fingerprint() -> str:
    parts = []
    for name in SCORING_SURFACE:
        function = getattr(scoring, name, None)
        if function is None:
            parts.append(f"{name}:MISSING")
            continue
        parts.append(f"{name}:{inspect.getsource(function)}")
    # Module-level constants the functions read are part of the rules too.
    for constant in sorted(
        n for n in dir(scoring) if n.isupper() and not n.startswith("_")
    ):
        parts.append(f"{constant}={getattr(scoring, constant)!r}")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()[:16]


def test_scoring_version_matches_the_scoring_rules():
    actual = scoring_fingerprint()
    expected = EXPECTED["fingerprint"]

    if expected is None:
        raise AssertionError(
            "Scoring fingerprint is not pinned yet.\n"
            f"  Set EXPECTED['fingerprint'] = {actual!r}\n"
            f"  alongside the current SCORING_VERSION {scoring.SCORING_VERSION!r}."
        )

    assert actual == expected, (
        "The scoring rules changed but SCORING_VERSION did not.\n"
        f"  SCORING_VERSION is still {scoring.SCORING_VERSION!r}.\n"
        f"  Expected fingerprint {expected!r}, got {actual!r}.\n"
        "\n"
        "Cached verdicts are served only when their stored scoring_version\n"
        "matches the running one, so leaving the version alone keeps rows\n"
        "scored by the previous formula valid for the rest of their TTL.\n"
        "\n"
        "Bump SCORING_VERSION, then update both values here in one commit."
    )


def test_the_pinned_version_is_the_running_one():
    assert scoring.SCORING_VERSION == EXPECTED["version"], (
        "SCORING_VERSION changed without updating this pin. Update EXPECTED "
        "with the new version and the new fingerprint together."
    )


def test_every_named_scoring_function_exists():
    """A renamed function must not silently drop out of the fingerprint."""
    missing = [name for name in SCORING_SURFACE if not hasattr(scoring, name)]
    assert not missing, (
        f"SCORING_SURFACE names functions that no longer exist: {missing}. "
        "A renamed scoring function stops being covered by the version pin."
    )
