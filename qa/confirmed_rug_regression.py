#!/usr/bin/env python3
"""Do the current live rules still bite on rugs that are proven on-chain?

A relaxation is the cheap way to make an uncomfortable number go away, so any
change that lowers a verdict has to be checked against tokens whose outcome is
not in dispute. These 17 are the published confirmed set from
`rugbuster-solana-goplus-benchmark`: creator bought, then offloaded at least
95% of peak holding, traced through Helius rather than taken from our own
database. GoPlus called all 17 safe; RugCheck's score caught 1.

This measures scoring only. It calls upstream RugCheck and scores locally, and
never touches a RugBuster service -- whether our API is up is a separate
question from whether its rules are right.

Read the result honestly: WARN here is a refusal to clear, not a detection.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scoring import SCORING_VERSION, score_live_rugcheck_report  # noqa: E402

# data/confirmed_17.json of rugbuster-solana-goplus-benchmark, on-chain confirmed.
CONFIRMED_RUGS = [
    "23dxgqAtivdW9cZz7UDFAGXUtByz5sXhKRDENdbXpump",
    "3FpBvhnAJAxjH25WqpYfoUuuB7Dy1UT72Aax4Na5pump",
    "3TByinjHpnNVZsH2miX9DwgTFB7JPm65yzDN2gSqpump",
    "3ZuZXv2g3TZofEogzM9rwEbVB5i1NwL8TqNCWH2jpump",
    "3n1Zy2pjN1WmKirKP31xvdZNC5QGEtisZpCP8taDpump",
    "6ML7tXmHEyESa2Rtj9FzqvQVA5DhsjPh4wpAvGgLpump",
    "6vpuYc1JzwsHvdf7gHQDby6o45mqsLe9QsJ5uDzgpump",
    "74nxamVTxGkqH2P4UCK6qmfBRcTGyKRAUnsDtBKEpump",
    "7p7ZMhihbDEoRmjTrriUMpM6jHE1jBhjXFG6iztPpump",
    "AShmQtSBCXX7ygHs9kv1Gkrfw5dqkfAidc9hZpowpump",
    "AyerU4udx5PF6ueZne2GuhgLDkqqQLuxzVPbS5sppump",
    "BiHqVnZibFk3po2JpTWnpeDy1ryTceQ88x1HM5TEpump",
    "CHMfiUmZvKwLdjo3nfZHQHp3RrbCBMAuRkkqnGPnpump",
    "CXRhJrBcCk1m83soxiGjY1AwiY6awAxhua6zPZMqpump",
    "ETWVkgQHsqnDWTmYg3hkhEPSfN49K7S42BhGPVZXpump",
    "HEoAb66vM87StCj3xyzDSCwVfPgx9ffp2yz1zqkgpump",
    "HoBvJJQxb9Dq2P2sePRkLEzAeZxvmG5QqKumy1PHpump",
]

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mints", type=Path, help="JSON list of mints; defaults to the built-in 17")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name(
        "confirmed_rug_regression.json"))
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    mints = json.loads(args.mints.read_text(encoding="utf-8")) if args.mints else CONFIRMED_RUGS
    results = []
    for index, mint in enumerate(mints, start=1):
        row: dict = {"mint": mint}
        try:
            request = urllib.request.Request(
                RUGCHECK_URL.format(mint=mint),
                headers={"User-Agent": "rugbuster-confirmed-rug-regression/1.0"},
            )
            with urllib.request.urlopen(request, timeout=args.timeout) as response:  # noqa: S310
                report = json.loads(response.read())
            report.setdefault("mint", mint)
            scored = score_live_rugcheck_report(report)
            row.update(label=scored.get("label"), risk_score=scored.get("risk_score"),
                       risk_flags=scored.get("risk_flags") or [],
                       upstream_score=report.get("score_normalised"),
                       upstream_rugged=report.get("rugged"), error=None)
        except Exception as exc:
            row.update(label=None, risk_score=None, risk_flags=[], error=type(exc).__name__)
        results.append(row)
        print(f"[{index:02d}/{len(mints)}] {mint[:10]}… {row.get('label')} "
              f"risk={row.get('risk_score')} error={row['error'] or '-'}")
        time.sleep(args.pause)

    labels = Counter(row["label"] or "ERROR" for row in results)
    cleared = [row["mint"] for row in results if row["label"] == "GOOD"]
    scored = [row for row in results if row["label"]]
    args.output.write_text(json.dumps({
        "scoring_version": SCORING_VERSION,
        "claim": f"{len(scored) - len(cleared)} of {len(scored)} not cleared as GOOD",
        "not_a_claim": (
            "Not 'detected'. Most of these land on WARN because the live path "
            "refuses to clear a mint with almost no holders left -- upstream "
            "RugCheck scores them near its floor, and declining to agree is not "
            "the same as identifying a rug. Quote the claim line, never a "
            "detection rate."
        ),
        "semantics": "On-chain confirmed rugs. GOOD on any of these is a scoring failure.",
        "counts": dict(sorted(labels.items())),
        "cleared_as_good": cleared,
        "results": results,
    }, indent=2) + "\n", encoding="utf-8")

    print(f"\nscoring_version={SCORING_VERSION} counts={dict(labels)}")
    print(f"{len(scored) - len(cleared)} of {len(scored)} not cleared as GOOD "
          "-- not a detection rate")
    return 1 if cleared else 0


if __name__ == "__main__":
    raise SystemExit(main())
