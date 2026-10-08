"""Who created a token, read from its first transaction on chain.

Why this exists. Every creator check (position, funding, history, the first
seconds) starts from the creator's address, and until now that address came
only from the RugCheck report. For launches outside pump.fun the report can
leave it out: The Altruists (Raydium LaunchLab, 2026-10-08) reached $400k
while we answered "creator not in report"; on chain the creator had bundled
27% across four wallets and sold it all within 13 seconds.

How. Helius's `getTransactionsForAddress` returns a mint's history oldest
first, so the creation transaction is one call away however busy the token
is. The creator is the wallet that paid for and signed that transaction. We
accept it only when the transaction initialises the mint, so a later
transaction is never mistaken for the launch, and only when a launchpad made
it: a token like JUP was minted whole by its deployer and handed out, and
reading that deployer as a launchpad creator called JUP DANGER in the gate.

Network use: two RPC calls, and only when the report has no creator.
"""
from __future__ import annotations

from typing import Any, Callable

import chain_checks

Call = Callable[[str, list[Any] | dict[str, Any]], Any]

# Programs whose creation transaction makes the signer a launchpad creator.
# Each one was seen in a real creation transaction before being listed here.
LAUNCHPADS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",          # TOASTCAT
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",  # The Altruists
}


def _programs(tx: dict[str, Any]) -> set[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    found = {ix.get("programId") for ix in message.get("instructions") or []}
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        found |= {ix.get("programId") for ix in inner.get("instructions") or []}
    return {p for p in found if p}


def creation(mint: str, call: Call) -> dict[str, Any] | None:
    """The creation transaction's signature, time and signer, or None."""
    page = call("getTransactionsForAddress", [mint, {"sortOrder": "asc", "limit": 1, "transactionDetails": "signatures"}])
    first = ((page or {}).get("data") or [None])[0]
    if not first or first.get("err"):
        return None
    signature = first.get("signature")
    tx = call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    if not tx:
        return None
    logs = (tx.get("meta") or {}).get("logMessages") or []
    if not any("InitializeMint" in line for line in logs):
        return None
    launchpad = next((LAUNCHPADS[p] for p in _programs(tx) if p in LAUNCHPADS), None)
    if launchpad is None:
        return None
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    signer = next((k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")), None)
    if not signer:
        return None
    return {"creator": signer, "launchpad": launchpad, "creation_signature": signature, "created_at": tx.get("blockTime")}


def fill_creator(report: dict[str, Any], call: Call | None = None) -> dict[str, Any]:
    """Put the creator into a report that lacks one. Never raises."""
    if isinstance(report.get("creator"), str) and report["creator"]:
        return report
    mint = str(report.get("mint") or report.get("address") or "").strip()
    if not mint:
        return report
    if call is None:
        url = chain_checks.rpc_url()
        if url is None:
            return report
        call = chain_checks._rpc(url)
    try:
        found = creation(mint, call)
    except Exception:  # noqa: BLE001 -- the report stands as it was; the gap stays listed
        return report
    if found:
        report["creator"] = found["creator"]
        report["creator_source"] = {"source": "solana_rpc", "method": "signer of the creation transaction", **found}
    return report
