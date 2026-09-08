"""Which code produced this answer.

An independent review of 2026-09-08 could confirm a finding in the repository
but not that the finding described what production was serving: "Open status
does not prove the branch is not deployed. Production deployment provenance was
NOT checked." A green `/health` shows a process is answering; it says nothing
about which commit is answering.

Separately, `solana_live_cache` rows are filtered by `scoring_version`, so a
scoring change that does not bump that constant leaves rows scored under the
previous formula valid for the rest of their TTL. That happened in PR #4.

So every response carries the commit it came from and the scoring version that
produced it, and `qa/test_scoring_version.py` fails if the scoring rules change
without the version changing with them.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

UNKNOWN = "unknown"

# Platforms that inject the deployed commit. Railway sets the first.
COMMIT_ENV_VARS = (
    "RAILWAY_GIT_COMMIT_SHA",
    "SOURCE_COMMIT",
    "GIT_COMMIT",
    "HEROKU_SLUG_COMMIT",
    "VERCEL_GIT_COMMIT_SHA",
)


@lru_cache(maxsize=1)
def build_commit() -> str:
    """The commit this process is running, or "unknown".

    Reported rather than guessed: a wrong commit is worse than none, because
    the whole point is to be able to trust what a measurement was measuring.
    """
    for name in COMMIT_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value[:40]

    # Local runs and any deploy that ships the .git directory.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    return (result.stdout or "").strip()[:40] or UNKNOWN


def build_identity(scoring_version: str) -> dict[str, str]:
    """The identity block attached to every response."""
    return {
        "build_commit": build_commit(),
        "scoring_version": scoring_version,
    }
