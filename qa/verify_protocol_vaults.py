#!/usr/bin/env python3
"""Re-derive every curated vault address from live reports.

A hand-maintained list of addresses is exactly the artifact that rots quietly:
nothing fails when an entry stops being true, it just starts clearing something
it should not. Two conditions, and an entry needs both:

  1. it is the mint or freeze authority of a mint already on
     KNOWN_SOLANA_MINTS -- a derivation, not a claim typed in on its own;
  2. it is observed holding one of those mints, because the list is read
     against holder tables and an authority is not a holder.

The second check was added after the first version of the list failed it. The
Wormhole token bridge authority is the mint authority of both curated Wormhole
assets and appears in no holder table at all, so listing it did nothing today
and would have exempted its concentration later on a derivation that says only
that it can mint.

Exit code 1 if any entry fails either check. Run it before trusting the list
and after any change to either list.
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

    # Which authority addresses the curated mints name, and which addresses
    # are actually seen holding them.
    authorities: dict[str, list[str]] = {}
    holdings: dict[str, list[str]] = {}
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
        for holder in report.get("topHolders") or []:
            owner = str((holder or {}).get("owner") or "")
            if owner:
                share = holder.get("pct")
                holdings.setdefault(owner, []).append(
                    f"{symbol} {share:.1f}%" if isinstance(share, (int, float)) else symbol
                )
        time.sleep(args.pause)

    unverified = []
    for address, name in KNOWN_PROTOCOL_VAULTS.items():
        derived_from = sorted(authorities.get(address) or [])
        held = sorted(holdings.get(address) or [])
        if derived_from and held:
            print(f"OK  {name}")
            print(f"      authority of: {', '.join(derived_from)}")
            print(f"      holds:        {', '.join(held)}")
            continue
        unverified.append((address, name))
        if not derived_from:
            print(f"!!  {name}: no curated mint names {address} as an authority")
        if not held:
            print(f"!!  {name}: never seen holding a curated mint -- an "
                  f"authority is not a holder, and this list exempts holders")

    print(f"\n{len(KNOWN_PROTOCOL_VAULTS) - len(unverified)} of "
          f"{len(KNOWN_PROTOCOL_VAULTS)} vault entries pass both checks")
    if unverified:
        print("An entry that fails either check must be removed or given a "
              "different, documented basis -- not left in on trust.")
    return 1 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
