"""What a wallet unlocks in the RugBuster app, read from chain.

Two things earn Pro: holding SKR (Solana Mobile's token) or holding a Seeker
Genesis Token (the NFT every Seeker phone owner gets). Both are plain reads:
no signature is checked here, so this only ever decides what the app lets a
wallet do, never anything paid out to it.

SKR is an SPL token, so one getTokenAccountsByOwner filtered by its mint says
how much a wallet holds. The Seeker Genesis Token is a Token-2022 mint whose
tokenGroupMember names the SGT group (and whose metadata pointer names the same
address), so the wallet's Token-2022 accounts are read and every 1-of-1 mint among
them is checked for both. Verified against a real SGT holder on 2026-10-06.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# https://solanamobile.com/skr
SKR_MINT = "SKRbvo6Gf7GondiT3BbTfuRDPqLWei4j2Qy2NPGZhW3"
# https://docs.solanamobile.com/marketing/engaging-seeker-users
SGT_GROUP = "GT22s89nU4iWFkNXj1Bw6uYhJJWDRPpShHt4Bk8f99Te"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Any SKR at all counts: the perk is for being part of the Seeker economy, not for size.
MIN_SKR = 1.0
MAX_MINTS_CHECKED = 100
TTL_SECONDS = 300

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _rpc(method: str, params: list[Any]) -> Any:
    url = os.getenv("SOLANA_RPC_URL", "").strip()
    if not url:
        raise RuntimeError("SOLANA_RPC_URL is not configured")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    with urlopen(Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=15) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def skr_balance(wallet: str) -> float:
    accounts = (_rpc("getTokenAccountsByOwner", [wallet, {"mint": SKR_MINT}, {"encoding": "jsonParsed"}]) or {}).get("value") or []
    total = 0.0
    for acc in accounts:
        amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount")
        if isinstance(amount, (int, float)):
            total += amount
    return total


def holds_seeker_genesis_token(wallet: str) -> bool:
    accounts = (_rpc("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_2022}, {"encoding": "jsonParsed"}]) or {}).get("value") or []
    mints = []
    for acc in accounts:
        info = acc["account"]["data"]["parsed"]["info"]
        if info["tokenAmount"].get("amount") == "1" and info["tokenAmount"].get("decimals") == 0:
            mints.append(info["mint"])
    for i in range(0, min(len(mints), MAX_MINTS_CHECKED), 100):
        chunk = mints[i:i + 100]
        for account in (_rpc("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}]) or {}).get("value") or []:
            if not account:
                continue
            extensions = (((account.get("data") or {}).get("parsed") or {}).get("info") or {}).get("extensions") or []
            # as the docs say: both the metadata pointer and the group membership must name the SGT
            metadata_ok = any(e.get("extension") == "metadataPointer" and (e.get("state") or {}).get("metadataAddress") == SGT_GROUP for e in extensions)
            member_ok = any(e.get("extension") == "tokenGroupMember" and (e.get("state") or {}).get("group") == SGT_GROUP for e in extensions)
            if metadata_ok and member_ok:
                return True
    return False


def perks_for(wallet: str) -> dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _cache.get(wallet)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    result: dict[str, Any] = {"wallet": wallet, "skr": 0.0, "seeker_genesis_token": False, "pro": False, "checked": True}
    try:
        result["skr"] = skr_balance(wallet)
        result["seeker_genesis_token"] = holds_seeker_genesis_token(wallet)
    except (HTTPError, URLError, TimeoutError, RuntimeError, KeyError, ValueError, OSError) as e:
        # an unread wallet is not a wallet without perks; say so instead of saying no
        result["checked"] = False
        result["error"] = str(e)[:120]
    result["pro"] = bool(result["skr"] >= MIN_SKR or result["seeker_genesis_token"])
    result["reason"] = (
        "Seeker Genesis Token" if result["seeker_genesis_token"]
        else f"{result['skr']:g} SKR" if result["skr"] >= MIN_SKR
        else None
    )
    with _lock:
        if result["checked"]:
            _cache[wallet] = (now, result)
            if len(_cache) > 5000:
                _cache.pop(next(iter(_cache)))
    return result
