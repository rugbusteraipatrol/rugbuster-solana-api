"""Who bought in the first seconds after the token was created.

Why this exists. In the outcome study (rugbuster-solana-outcomes, September
2026) we looked at 300 traded launches where the creator's own wallet never
held the token. On 163 of them, three or more wallets bought within 5 seconds
of creation; 670 of the 1,226 early buyers were wallets with over 1,000
transactions. Those launches are taken by bots before a person can act, and
the first sellers are already positioned when later buyers arrive.

What this module reads, on the live path: the token's earliest transactions
(one `getSignaturesForAddress` on the mint, then `getTransaction` for the first
few), the distinct wallets that first hold the token within the window, and
for each of those a signature count to tell a high-frequency bot wallet from
an ordinary one. Between 2 and about 14 RPC calls.

Limit: `getSignaturesForAddress` returns the newest 1,000 signatures. A token
with more history than that cannot have its launch read this way, and the
result says so (`unavailable`) rather than guessing.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen  # noqa: F401 -- kept for callers that patch it

from rpc_http import rpc_post

RPC_TIMEOUT_SECONDS = 4
WINDOW_SECONDS = 5            # "the first seconds": same window as the study
MIN_BUYERS_FOR_SNIPED = 3     # same threshold as the study (163 of 300)
BUSY_WALLET_SIGNATURES = 900  # a page of 1,000 minus failed ones: a bot, not a person
MAX_EARLY_TRANSACTIONS = 8    # how far into the launch we read
MAX_WALLETS_CHECKED = 5       # how many early buyers get a signature count
POOL_SHARE_OF_SUPPLY = 0.5    # an account holding this much is the curve/pool, not a buyer

STUDY = {
    "launches_checked": 300,
    "launches_with_3plus_buyers_in_5s": 163,
    "early_buyers": 1226,
    "early_buyers_that_are_bot_wallets": 670,
    "population": "traded launches whose creator wallet never held the token, May-Aug 2026",
    "study": "https://gitlab.com/rugbuster/rugbuster-solana-outcomes",
}


def rpc_url() -> str | None:
    value = os.getenv("SOLANA_RPC_URL", "").strip()
    return value or None


def _rpc_call(url: str, method: str, params: list[Any]) -> Any:
    return rpc_post(url, method, params, RPC_TIMEOUT_SECONDS)


def _balances(tx: dict[str, Any], mint: str) -> tuple[dict[str, float], dict[str, float]]:
    meta = (tx or {}).get("meta") or {}

    def table(key: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for entry in meta.get(key) or []:
            if entry.get("mint") == mint and entry.get("owner"):
                out[entry["owner"]] = float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
        return out

    return table("preTokenBalances"), table("postTokenBalances")


def unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "solana_rpc",
    }


def read(
    mint: str,
    creator: str | None,
    supply: float | None,
    call: Callable[[str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    """Read the launch. Never raises: a failed read is `unavailable`, which
    scores as nothing and is listed as a gap."""
    if not mint:
        return unavailable("mint not in report")
    if call is None:
        url = rpc_url()
        if url is None:
            return unavailable("SOLANA_RPC_URL not configured")
        call = lambda method, params: _rpc_call(url, method, params)  # noqa: E731
    started = time.monotonic()
    try:
        signatures = call("getSignaturesForAddress", [mint, {"limit": 1000}]) or []
        if len(signatures) >= 1000:
            return unavailable("token has more than 1,000 transactions; its launch is beyond the readable window")
        ordered = sorted((s for s in signatures if not s.get("err")), key=lambda s: s.get("slot", 0))
        if not ordered:
            return unavailable("no transactions found for this mint")
        creation_time = ordered[0].get("blockTime")
        if not creation_time:
            return unavailable("creation time not readable")
        pool_threshold = (supply or 0) * POOL_SHARE_OF_SUPPLY if supply else None
        first_holders: dict[str, dict[str, Any]] = {}
        for signature in ordered[:MAX_EARLY_TRANSACTIONS]:
            when = signature.get("blockTime")
            if when is None or when - creation_time > WINDOW_SECONDS:
                break
            tx = call(
                "getTransaction",
                [signature["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            pre, post = _balances(tx or {}, mint)
            for owner, balance in post.items():
                if balance <= 0 or pre.get(owner, 0.0) > 0 or owner == creator or owner in first_holders:
                    continue
                if pool_threshold and balance >= pool_threshold:
                    continue  # the bonding curve or pool account, not a buyer
                first_holders[owner] = {"wallet": owner, "seconds_after_creation": when - creation_time,
                                        "share_pct": round(100.0 * balance / supply, 3) if supply else None,
                                        "signature": signature["signature"]}
        buyers = list(first_holders.values())
        for buyer in buyers[:MAX_WALLETS_CHECKED]:
            history = call("getSignaturesForAddress", [buyer["wallet"], {"limit": 1000}]) or []
            buyer["transactions_on_record"] = len(history)
            buyer["kind"] = "bot" if len(history) >= BUSY_WALLET_SIGNATURES else "wallet"
        bots = sum(1 for b in buyers if b.get("kind") == "bot")
        sniped = len(buyers) >= MIN_BUYERS_FOR_SNIPED
        return {
            "status": "sniped" if sniped else "clean",
            "buyers_within_window": len(buyers),
            "window_seconds": WINDOW_SECONDS,
            "bot_wallets_among_checked": bots,
            "wallets_checked": min(len(buyers), MAX_WALLETS_CHECKED),
            "first_buyers": buyers,
            "creation_time": creation_time,
            "creation_signature": ordered[0].get("signature"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "solana_rpc",
            "lookup_seconds": round(time.monotonic() - started, 2),
            "study": STUDY,
        }
    except Exception as error:  # network, parsing, timeouts: a gap, never a crash
        return unavailable(f"rpc lookup failed: {str(error)[:80]}")


def from_report(report: dict[str, Any], call: Callable[[str, list[Any]], Any] | None = None) -> dict[str, Any]:
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    mint = str(report.get("mint") or report.get("address") or "").strip()
    creator = report.get("creator") if isinstance(report.get("creator"), str) else None
    supply = None
    try:
        if token.get("supply") is not None and token.get("decimals") is not None:
            supply = float(token["supply"]) / (10 ** int(token["decimals"]))
    except (TypeError, ValueError):
        supply = None
    return read(mint, creator, supply, call)
