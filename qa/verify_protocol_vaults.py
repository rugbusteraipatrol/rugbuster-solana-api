#!/usr/bin/env python3
"""Re-derive every curated vault address from live reports.

A hand-maintained list of addresses is exactly the artifact that rots quietly:
nothing fails when an entry stops being true, it just starts clearing something
it should not. Each entry in KNOWN_PROTOCOL_VAULTS earns its place by being the
mint or freeze authority of a mint already on KNOWN_SOLANA_MINTS, and this
re-checks that against the chain rather than against the comment beside it.

Exit code 1 if any entry can no longer be derived. Run it before trusting the
list, and after any change to either list.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scoring import KNOWN_PROTOCOL_VAULTS, KNOWN_SOLANA_MINTS  # noqa: E402

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    # Which authority addresses the curated mints actually name today.
    authorities: dict[str, list[str]] = {}
    for mint, symbol in KNOWN_SOLANA_MINTS.items():
        try:
            request = urllib.request.Request(
                RUGCHECK_URL.format(mint=mint),
                headers={"User-Agent": "rugbuster-vault-verification/1.0"},
            )
            with urllib.request.urlopen(request, timeout=args.timeout) as response:  # noqa: S310
                report = json.loads(response.read())
        except Exception as exc:
            print(f"  ! {symbol}: could not read report ({type(exc).__name__})")
            time.sleep(args.pause)
            continue
        token = report.get("token") if isinstance(report.get("token"), dict) else {}
        for field in ("mintAuthority", "freezeAuthority"):
            address = token.get(field)
            if address:
                authorities.setdefault(str(address), []).append(f"{symbol}.{field}")
        time.sleep(args.pause)

    unverified = []
    for address, name in KNOWN_PROTOCOL_VAULTS.items():
        derived_from = authorities.get(address)
        if derived_from:
            print(f"OK  {name}: authority of {', '.join(sorted(derived_from))}")
        else:
            unverified.append((address, name))
            print(f"!!  {name}: no curated mint names {address} as an authority")

    print(f"\n{len(KNOWN_PROTOCOL_VAULTS) - len(unverified)} of "
          f"{len(KNOWN_PROTOCOL_VAULTS)} vault entries re-derived")
    if unverified:
        print("An entry that cannot be derived must be removed or given a "
              "different, documented basis -- not left in on trust.")
    return 1 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
