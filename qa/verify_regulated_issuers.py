#!/usr/bin/env python3
"""Re-check that each regulated-issuer authority still controls its issuer's tokens.

For Backpack Securities: take every Solana mint Backpack lists as a stock
(symbol *.US) from its public asset list, read each mint on chain, and count how
many carry the curated address as both freeze authority and permanent delegate.
Anything short of all of them means the curation needs a look.

Reads only public data: Backpack's asset API and a Solana RPC.
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoring import KNOWN_REGULATED_ISSUER_AUTHORITIES  # noqa: E402

RPC = os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
BACKPACK = "2cVYpagTt7ZGc3mmTXBa7fAznUtx5DUu6aCq8uVDaf4a"


def get_json(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "RugBuster-qa"})
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def main() -> int:
    assert BACKPACK in KNOWN_REGULATED_ISSUER_AUTHORITIES
    assets = get_json("https://api.backpack.exchange/api/v1/assets")
    mints = [t["contractAddress"] for a in assets if str(a.get("symbol", "")).endswith(".US")
             for t in (a.get("tokens") or []) if t.get("blockchain") == "Solana" and t.get("contractAddress")]
    matched = 0
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        result = get_json(RPC, {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                                "params": [chunk, {"encoding": "jsonParsed"}]})
        for account in result["result"]["value"]:
            info = ((account or {}).get("data") or {}).get("parsed", {}).get("info", {})
            delegate = next((e["state"].get("delegate") for e in info.get("extensions", [])
                             if e.get("extension") == "permanentDelegate"), None)
            if info.get("freezeAuthority") == BACKPACK and delegate == BACKPACK:
                matched += 1
        time.sleep(1)
    print(f"Backpack Securities: {matched} of {len(mints)} listed stock mints carry {BACKPACK} "
          "as freeze authority and permanent delegate")
    return 0 if mints and matched == len(mints) else 1


if __name__ == "__main__":
    raise SystemExit(main())
