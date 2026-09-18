"""The creator's own position in the token: what they bought when they made it,
and whether they still hold it.

Why this exists. On pump.fun-style launches the creator's allocation is bought
in the transaction that creates the token, before anyone else can act. We
labelled 9,170 of our own scans by what happened on-chain afterwards (September
2026). Among traded tokens where the creator held at least 1% of supply
(n = 2,785), the creator sold 95% or more of it within 30 days in 97.6% of
cases, and within the first hour in 88.9%. RugBuster's rules were calling almost
half of those tokens GOOD. The position is visible at creation, so it belongs
on the live path.

What this module does not do: it reads only the creator's own wallet. Bundled
side wallets are not traced here, so `status == "none"` means the creator's
wallet never held the token, not that nobody connected to them did.

Network use: one or two `getSignaturesForAddress` calls on the creator's
associated token account (legacy SPL first, then Token-2022, since nearly all
current launches are Token-2022), then `getTransaction` for the first and last
signatures. Four to six RPC calls, bounded by `RPC_TIMEOUT_SECONDS` each.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

from solders.pubkey import Pubkey

ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
TOKEN_PROGRAMS = (
    ("spl", Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")),
    ("token2022", Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")),
)
RPC_TIMEOUT_SECONDS = 4
MAX_TRANSACTIONS = 4          # first two and last two signatures
SOLD_THRESHOLD = 0.95         # "sold" means at least this share of the peak is gone

# The study this rests on. The numbers are quoted in every response so a reader
# can weigh the flag without trusting the label.
BASE_RATE = {
    "n": 2785,
    "exit_within_1h": 0.889,
    "exit_within_30d": 0.976,
    "population": "traded tokens where the creator held >= 1% of supply, May-Aug 2026",
    "study": "rugbuster-solana-outcomes (publication pending)",
}
MATERIAL_SHARE_PCT = 5.0
MINOR_SHARE_PCT = 1.0


def rpc_url() -> str | None:
    value = os.getenv("SOLANA_RPC_URL", "").strip()
    return value or None


def associated_token_account(owner: str, mint: str, program: Pubkey) -> str:
    seeds = [bytes(Pubkey.from_string(owner)), bytes(program), bytes(Pubkey.from_string(mint))]
    return str(Pubkey.find_program_address(seeds, ATA_PROGRAM)[0])


def _rpc_call(url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=RPC_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    if "error" in payload:
        raise RuntimeError(str(payload["error"])[:120])
    return payload.get("result")


def _creator_balance(tx: dict[str, Any], creator: str, mint: str) -> float | None:
    """The creator's balance of `mint` after this transaction, or None if the
    transaction says nothing about it. A balance present before and absent
    after means the account was closed, which is a balance of zero."""
    meta = tx.get("meta") if isinstance(tx, dict) else None
    if not isinstance(meta, dict):
        return None
    for entry in meta.get("postTokenBalances") or []:
        if entry.get("owner") == creator and entry.get("mint") == mint:
            amount = (entry.get("uiTokenAmount") or {}).get("uiAmount")
            return float(amount or 0.0)
    for entry in meta.get("preTokenBalances") or []:
        if entry.get("owner") == creator and entry.get("mint") == mint:
            return 0.0
    return None


def unavailable(reason: str, creator: str | None = None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "creator": creator,
        "reason": reason,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": "solana_rpc",
    }


def lookup(
    creator: str | None,
    mint: str,
    supply: float | None,
    call: Callable[[str, list[Any]], Any] | None = None,
) -> dict[str, Any]:
    """Read the creator's position from chain. Never raises: a failed lookup is
    reported as `unavailable`, which scores as nothing and is listed as a gap."""
    if not creator or not mint:
        return unavailable("creator or mint not in report", creator)
    if not supply or supply <= 0:
        return unavailable("token supply not in report", creator)
    if call is None:
        url = rpc_url()
        if url is None:
            return unavailable("SOLANA_RPC_URL not configured", creator)
        call = lambda method, params: _rpc_call(url, method, params)  # noqa: E731

    try:
        signatures: list[dict[str, Any]] = []
        program_used = None
        for name, program in TOKEN_PROGRAMS:
            account = associated_token_account(creator, mint, program)
            found = call("getSignaturesForAddress", [account, {"limit": 1000}]) or []
            if found:
                signatures, program_used = found, name
                break
        observed_at = datetime.now(timezone.utc).isoformat()
        if not signatures:
            return {
                "status": "none",
                "creator": creator,
                "share_at_creation_pct": 0.0,
                "current_share_pct": 0.0,
                "observed_at": observed_at,
                "source": "solana_rpc",
                "note": "The creator's own wallet never held this token. Side wallets are not traced here.",
            }
        ordered = sorted((s for s in signatures if not s.get("err")), key=lambda s: s.get("slot", 0))
        if not ordered:
            return unavailable("only failed transactions on the creator's token account", creator)
        picked = ordered if len(ordered) <= MAX_TRANSACTIONS else ordered[:2] + ordered[-2:]
        points: list[tuple[int | None, float]] = []
        for signature in picked:
            tx = call(
                "getTransaction",
                [signature["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            balance = _creator_balance(tx or {}, creator, mint)
            if balance is not None:
                points.append((signature.get("blockTime"), balance))
        if not points:
            return unavailable("creator balance not readable from transactions", creator)
        first_time, first_balance = points[0]
        peak = max(balance for _, balance in points)
        last_time, last_balance = points[-1]
        if peak <= 0:
            status = "none"
        elif last_balance <= peak * (1 - SOLD_THRESHOLD):
            status = "sold"
        else:
            status = "holding"
        sold_after = (last_time - first_time) if (status == "sold" and first_time and last_time) else None
        return {
            "status": status,
            "creator": creator,
            "token_program": program_used,
            "share_at_creation_pct": round(100.0 * first_balance / supply, 3),
            "peak_share_pct": round(100.0 * peak / supply, 3),
            "current_share_pct": round(100.0 * last_balance / supply, 3),
            "sold_pct_of_peak": round(100.0 * (peak - last_balance) / peak, 1) if peak > 0 else None,
            "first_seen_at": first_time,
            "last_change_at": last_time,
            "sold_after_seconds": sold_after,
            "transactions_on_record": len(ordered),
            "observed_at": observed_at,
            "source": "solana_rpc",
            "base_rate": BASE_RATE,
        }
    except Exception as error:  # network, parsing, timeouts: a gap, never a crash
        return unavailable(f"rpc lookup failed: {str(error)[:80]}", creator)


def from_report(report: dict[str, Any], call: Callable[[str, list[Any]], Any] | None = None) -> dict[str, Any]:
    """Read creator, mint and supply out of a RugCheck report and look the position up."""
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    creator = report.get("creator")
    mint = str(report.get("mint") or report.get("address") or "").strip()
    supply_raw = token.get("supply")
    decimals = token.get("decimals")
    supply = None
    try:
        if supply_raw is not None and decimals is not None:
            supply = float(supply_raw) / (10 ** int(decimals))
    except (TypeError, ValueError):
        supply = None
    started = time.monotonic()
    position = lookup(creator if isinstance(creator, str) else None, mint, supply, call)
    position["lookup_seconds"] = round(time.monotonic() - started, 2)
    return position
