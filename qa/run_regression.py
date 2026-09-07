#!/usr/bin/env python3
"""Replay golden_set_solana.yaml against both Solana scoring paths.

Two paths, because checking one hides failures in the other:

* **served**   -- GET /score on the deployed API. This is what a user gets,
                  but for any address the collector has already seen it is
                  answered from the Postgres cache.
* **live**     -- the live RugCheck report run through
                  ``scoring.score_live_rugcheck_report``. This is what a token
                  gets when it is NOT in cache, which is every token a user
                  brings that we have not scanned before.

On 2026-09-07 the served path returned DANGER for all 17 confirmed rugs while
the live path returned GOOD with risk 1 for 16 of them. A single-path gate
would have read green.

Exit code 0 = all gates green. Exit code 1 = at least one gate failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_GOLDEN = Path(__file__).with_name("golden_set_solana.yaml")
USER_AGENT = "rugbuster-solana-qa/1.0"


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


def served_label(endpoint: str, address: str, timeout: float = 40) -> tuple[str | None, str]:
    try:
        body = _get_json(f"{endpoint}?address={address}", timeout)
    except Exception as exc:
        return None, f"request failed: {type(exc).__name__}"
    return body.get("label"), str(body.get("source") or "")


def live_label(rugcheck_url: str, address: str, timeout: float = 30) -> tuple[str | None, str]:
    from scoring import score_live_rugcheck_report

    try:
        report = _get_json(rugcheck_url.format(address=address), timeout)
    except Exception as exc:
        return None, f"rugcheck failed: {type(exc).__name__}"
    report.setdefault("mint", address)
    result = score_live_rugcheck_report(report)
    return result.get("label"), f"risk={result.get('risk_score')}"


def evaluate(entry: dict, label: str | None, path_name: str) -> list[str]:
    problems = []
    if label is None:
        problems.append(f"{path_name}: no label returned")
        return problems
    expected = entry.get("expected_labels")
    forbidden = entry.get("forbidden_labels") or []
    if expected and label not in expected:
        problems.append(f"{path_name}: label={label} not in expected {expected}")
    if label in forbidden:
        problems.append(f"{path_name}: label={label} is forbidden {forbidden}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--served-only", action="store_true", help="skip the live-path check")
    parser.add_argument("--live-only", action="store_true", help="skip the served-endpoint check")
    parser.add_argument("--pause", type=float, default=1.2, help="seconds between upstream calls")
    args = parser.parse_args()

    doc = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    endpoint = doc["endpoint"]
    rugcheck_url = doc["rugcheck_api"]
    entries = doc["entries"]

    check_served = not args.live_only
    check_live = not args.served_only and doc.get("gates", {}).get("check_live_path", True)

    print(f"Golden set: {args.golden}")
    print(f"Entries: {len(entries)}   served={check_served}   live={check_live}")
    print()

    results = []
    for index, entry in enumerate(entries, start=1):
        problems: list[str] = []
        served = live = None
        served_note = live_note = ""

        if check_served:
            served, served_note = served_label(endpoint, entry["address"])
            problems += evaluate(entry, served, "served")
            time.sleep(args.pause)

        if check_live:
            live, live_note = live_label(rugcheck_url, entry["address"])
            problems += evaluate(entry, live, "live")
            time.sleep(args.pause)

        status = "PASS" if not problems else ("FAIL" if entry.get("gate") else "WARN")
        results.append({"entry": entry, "status": status, "problems": problems})
        marker = "  " if status == "PASS" else "X "
        print(
            f"{marker}[{index:02d}/{len(entries)}] {entry['category']:15s} {entry['symbol']:14s} "
            f"served={str(served):8s} ({served_note[:18]:18s}) live={str(live):8s} ({live_note[:12]})"
        )
        for problem in problems:
            print(f"        - {problem}")

    print()
    overall = True

    canonical = [r for r in results if r["entry"]["category"] == "canonical"]
    canonical_bad = [r for r in canonical if r["status"] == "FAIL"]
    overall &= not canonical_bad
    print(
        f"[{'PASS' if not canonical_bad else 'FAIL'}] S1 canonical mints read GOOD on every path: "
        f"{len(canonical) - len(canonical_bad)}/{len(canonical)}"
    )

    rugs = [r for r in results if r["entry"]["category"] == "confirmed_rug"]
    rugs_bad = [r for r in rugs if r["status"] == "FAIL"]
    overall &= not rugs_bad
    print(
        f"[{'PASS' if not rugs_bad else 'FAIL'}] S2 on-chain confirmed creator dumps never read GOOD "
        f"on any path: {len(rugs) - len(rugs_bad)}/{len(rugs)}"
    )
    for r in rugs_bad:
        print(f"        - FALSE GOOD: {r['entry']['symbol']} ({r['entry']['address']})")

    print()
    print("RESULT: GREEN - all gates pass" if overall else "RESULT: RED - one or more gates failed")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
