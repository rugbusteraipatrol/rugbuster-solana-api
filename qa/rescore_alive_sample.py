#!/usr/bin/env python3
"""Re-score the documented active-token sample through the current live path.

This is a regression probe, not an accuracy estimate. The input set establishes
that the tokens were active when sampled; it does not independently prove that
every token is benign. Results therefore report labels and coverage without
calling WARN or DANGER a false positive.
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

DEFAULT_INPUT = Path(__file__).with_name("alive_token_rescore_2026-09-08.json")
DEFAULT_OUTPUT = Path(__file__).with_name("alive_token_rescore_live_2026-09-09.json")
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
USER_AGENT = "rugbuster-solana-alive-regression/1.0"


def fetch_report(mint: str, timeout: float) -> dict:
    request = urllib.request.Request(
        RUGCHECK_URL.format(mint=mint), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pause", type=float, default=0.25)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    source_rows = json.loads(args.input.read_text(encoding="utf-8"))
    results = []

    for index, source in enumerate(source_rows, start=1):
        mint = source["mint"]
        row = {
            "symbol": source.get("symbol"),
            "mint": mint,
            "previous_label": source.get("after"),
            "previous_score": source.get("after_score"),
        }
        try:
            report = fetch_report(mint, args.timeout)
            report.setdefault("mint", mint)
            scored = score_live_rugcheck_report(report)
            row.update(
                label=scored.get("label"),
                risk_score=scored.get("risk_score"),
                risk_flags=scored.get("risk_flags") or [],
                error=None,
            )
        except Exception as exc:
            row.update(label=None, risk_score=None, risk_flags=[], error=type(exc).__name__)
        results.append(row)
        print(
            f"[{index:02d}/{len(source_rows)}] {str(row['symbol']):12s} "
            f"{str(row['label']):7s} risk={str(row['risk_score']):3s} "
            f"error={row['error'] or '-'}"
        )
        time.sleep(args.pause)

    labels = Counter(row["label"] or "ERROR" for row in results)
    output = {
        "scoring_version": SCORING_VERSION,
        "input": str(args.input),
        "sample_semantics": (
            "Active-token regression sample; not independently adjudicated ground truth "
            "and not an accuracy or false-positive estimate."
        ),
        "counts": dict(sorted(labels.items())),
        "results": results,
    }
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"\nscoring_version={SCORING_VERSION} counts={dict(labels)}")
    print(f"saved={args.output}")
    return 1 if labels.get("ERROR") else 0


if __name__ == "__main__":
    raise SystemExit(main())
