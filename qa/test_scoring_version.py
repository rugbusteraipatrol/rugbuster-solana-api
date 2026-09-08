"""Changing the scoring rules must change the scoring version.

`solana_live_cache` rows are served only when their stored `scoring_version`
matches the running one. That filter is the only thing stopping a verdict
computed by the previous formula from being served after a deploy -- so a
scoring change that leaves the version alone keeps the old rows valid for the
rest of their TTL. That is what PR #4 did: it changed
`score_live_rugcheck_report` and left `SCORING_VERSION` at 2026.09.1.

The first version of this file hashed a hand-written list of scoring functions.
An independent review broke it in one line: `rugcheck_to_risk` delegates to
`_linear`, `_linear` was not on the list, and changing it moved the verdict
while the fingerprint stood still. A curated list only covers what someone
remembered, and the risk here is precisely the change nobody thought about.

So the whole module is hashed instead. Every helper, constant and table in
`scoring.py` is inside the fingerprint whether or not anyone listed it. The
cost is that cosmetic edits -- a comment, a blank line -- also trip the test;
that is deliberate. Being asked "did this change a verdict?" on a comment edit
is cheap. Not being asked on a real one is what this exists to prevent.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scoring  # noqa: E402

SCORING_FILE = REPO_ROOT / "scoring.py"

# Bump together with SCORING_VERSION. Take the new value from the failure
# message after reviewing the diff, never from a passing run of an unreviewed
# change.
EXPECTED_VERSION = "2026.09.3"
EXPECTED_FINGERPRINT = "a8579c9ff92a7a8d"


def fingerprint_of(source: str) -> str:
    """Hash of the scoring rules, normalised for line endings only."""
    normalised = source.replace("\r\n", "\n").encode("utf-8")
    return hashlib.sha256(normalised).hexdigest()[:16]


def scoring_fingerprint() -> str:
    return fingerprint_of(SCORING_FILE.read_text(encoding="utf-8"))


def test_scoring_version_matches_the_scoring_rules():
    actual = scoring_fingerprint()

    if EXPECTED_FINGERPRINT == "PLACEHOLDER":
        raise AssertionError(
            "Scoring fingerprint is not pinned yet.\n"
            f"  Set EXPECTED_FINGERPRINT = {actual!r}\n"
            f"  alongside SCORING_VERSION {scoring.SCORING_VERSION!r}."
        )

    assert actual == EXPECTED_FINGERPRINT, (
        "scoring.py changed but SCORING_VERSION did not.\n"
        f"  SCORING_VERSION is still {scoring.SCORING_VERSION!r}.\n"
        f"  Expected fingerprint {EXPECTED_FINGERPRINT!r}, got {actual!r}.\n"
        "\n"
        "Cached verdicts are served only when their stored scoring_version\n"
        "matches the running one, so leaving the version alone keeps rows\n"
        "scored by the previous formula valid for the rest of their TTL.\n"
        "\n"
        "If the edit cannot change a verdict, update the fingerprint alone.\n"
        "If it can, bump SCORING_VERSION and update both, in one commit."
    )


def test_the_pinned_version_is_the_running_one():
    assert scoring.SCORING_VERSION == EXPECTED_VERSION, (
        "SCORING_VERSION changed without updating this pin. Update the version "
        "and the fingerprint together."
    )


def test_a_change_to_a_transitive_helper_is_detected(tmp_path):
    """The gap the review found: `rugcheck_to_risk` delegates to `_linear`.

    Mutates a copy on disk rather than the working tree, so the check is on
    file content -- which is what the fingerprint reads -- and no source file
    is touched. A runtime monkeypatch would not be detected by design, and
    should not be: the version pin is about what shipped, not what a test
    patched in memory.
    """
    source = SCORING_FILE.read_text(encoding="utf-8")
    assert "def _linear(" in source, "helper renamed; update this test"

    mutated = source.replace("def _linear(", "def _linear_renamed_by_mutation_test(", 1)
    assert mutated != source

    copy = tmp_path / "scoring.py"
    copy.write_text(mutated, encoding="utf-8")

    assert fingerprint_of(mutated) != scoring_fingerprint(), (
        "Changing a transitive helper left the fingerprint unchanged. The "
        "version pin does not cover the whole scoring surface."
    )


def test_a_change_to_a_threshold_constant_is_detected(tmp_path):
    source = SCORING_FILE.read_text(encoding="utf-8")
    assert "MIN_HOLDERS_FOR_CLEAN_VERDICT = 50" in source, "constant changed; update this test"
    mutated = source.replace("MIN_HOLDERS_FOR_CLEAN_VERDICT = 50", "MIN_HOLDERS_FOR_CLEAN_VERDICT = 25", 1)
    assert fingerprint_of(mutated) != scoring_fingerprint()


def test_the_fingerprint_is_stable_across_line_endings():
    """CRLF checkouts must not look like a scoring change."""
    source = SCORING_FILE.read_text(encoding="utf-8")
    assert fingerprint_of(source.replace("\n", "\r\n")) == fingerprint_of(source)
