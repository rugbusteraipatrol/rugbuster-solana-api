"""Pay once for a RugBuster Solana verdict over x402, from a local test wallet.

Run by a person, on purpose: it moves real USDC. Before paying it reads the
402 challenge and refuses unless the price is at most MAX_USDC and the money
goes to the expected address.

    python scripts/pay_x402_solana.py [mint]

The wallet is a JSON byte array at ~/.rugbuster/x402-test-wallet.json. It
needs USDC only; the Coinbase facilitator pays the Solana network fee.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import requests
from solders.keypair import Keypair
from x402 import x402ClientSync
from x402.http.clients import x402_requests
from x402.mechanisms.svm.exact.register import register_exact_svm_client
from x402.mechanisms.svm.signers import KeypairSigner

API = "https://rugbuster-solana-api-production.up.railway.app"
EXPECTED_PAY_TO = "FYQA1zFc4eUhRe6YNi8a9yQ4oskVgpm35uAFberHkboU"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MAX_USDC = 0.05
WALLET = os.path.expanduser(os.path.join("~", ".rugbuster", "x402-test-wallet.json"))
RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def main() -> int:
    mint = sys.argv[1] if len(sys.argv) > 1 else "CqNJeUKi2feUBCbG2rARBQxh9BwUynoK5WrH6d8Bpump"
    url = f"{API}/x402/score?address={mint}"

    challenge = requests.get(url, timeout=30)
    if challenge.status_code != 402:
        print(f"Expected 402 Payment Required, got {challenge.status_code}. Nothing paid.")
        return 1
    option = json.loads(base64.b64decode(challenge.headers["PAYMENT-REQUIRED"]))["accepts"][0]
    price = int(option["amount"]) / 1_000_000
    if option["payTo"] != EXPECTED_PAY_TO or option["asset"] != USDC_MINT or price > MAX_USDC:
        print(f"Refusing to pay: {price} to {option['payTo']} in {option['asset']}.")
        return 1
    print(f"Paying {price:.2f} USDC on Solana to {option['payTo']} ...")

    keypair = Keypair.from_bytes(bytes(json.load(open(WALLET))))
    client = x402ClientSync()
    register_exact_svm_client(client, KeypairSigner(keypair), networks=option["network"], rpc_url=RPC)
    response = x402_requests(client).get(url, timeout=90)

    body = response.json()
    print(f"Status {response.status_code}: {body.get('label')} {body.get('risk_score')}/100")
    if body.get("verdict_summary"):
        print(body["verdict_summary"])
    receipt = response.headers.get("PAYMENT-RESPONSE")
    if receipt:
        settled = json.loads(base64.b64decode(receipt))
        tx = settled.get("transaction")
        print(f"Paid by {settled.get('payer')}")
        print(f"Transaction: https://solscan.io/tx/{tx}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
