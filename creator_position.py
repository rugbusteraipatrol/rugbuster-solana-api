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
from urllib.request import Request, urlopen  # noqa: F401 -- kept for callers that patch it

from rpc_http import rpc_post

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
    "study": "https://gitlab.com/rugbuster/rugbuster-solana-outcomes",
}
MATERIAL_SHARE_PCT = 5.0
MINOR_SHARE_PCT = 1.0

# A creator who mints the whole supply to themselves and then opens a pool with
# it has not sold anything: the tokens went into liquidity they control. Read as
# a sale, that made Cetus (minted 100%, Orca pool 6 s later) and ELBARTO (Meteora
# pool 10 s later) DANGER with "bought 100% at creation, sold it" (2026-10-09).
# The drop is a deposit when the transaction runs one of these AMM programs with
# a pool or liquidity instruction and no swap.
AMM_PROGRAMS = frozenset({
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpools
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",   # Meteora DAMM v2
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DAMM v1
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # Meteora DLMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
})
LIQUIDITY_WORDS = ("initializepool", "initializecustomizablepool", "initializelbpair", "initialize2",
                   "openposition", "increaseliquidity", "addliquidity", "deposit")


def rpc_url() -> str | None:
    value = os.getenv("SOLANA_RPC_URL", "").strip()
    return value or None


def associated_token_account(owner: str, mint: str, program: Pubkey) -> str:
    seeds = [bytes(Pubkey.from_string(owner)), bytes(program), bytes(Pubkey.from_string(mint))]
    return str(Pubkey.find_program_address(seeds, ATA_PROGRAM)[0])


def _rpc_call(url: str, method: str, params: list[Any]) -> Any:
    return rpc_post(url, method, params, RPC_TIMEOUT_SECONDS)


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


def is_liquidity_deposit(tx: dict[str, Any]) -> bool:
    """True when this transaction put tokens into an AMM pool rather than
    selling them: an AMM program runs a pool or liquidity instruction and
    nothing in it is a swap."""
    meta = tx.get("meta") if isinstance(tx, dict) else None
    logs = [str(line) for line in ((meta or {}).get("logMessages") or [])]
    invoked = {line.split()[1] for line in logs if line.startswith("Program ") and " invoke [" in line}
    if not invoked & AMM_PROGRAMS:
        return False
    lowered = [line.lower() for line in logs]
    if any("instruction: swap" in line or "swapbase" in line for line in lowered):
        return False
    return any(word in line for line in lowered for word in LIQUIDITY_WORDS)


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
        truncated = len(ordered) > MAX_TRANSACTIONS
        picked = ordered if not truncated else ordered[:2] + ordered[-2:]
        # (blockTime, balance, index in the full history, signature) -- the index
        # tells us whether unread transactions sit between two readings; the
        # signature lets a reader open the transaction behind each finding.
        points: list[tuple[int | None, float, int, str]] = []
        index_of = {s["signature"]: i for i, s in enumerate(ordered)}
        transactions: dict[str, dict[str, Any]] = {}
        for signature in picked:
            tx = call(
                "getTransaction",
                [signature["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
            )
            transactions[signature["signature"]] = tx or {}
            balance = _creator_balance(tx or {}, creator, mint)
            if balance is not None:
                points.append((signature.get("blockTime"), balance, index_of[signature["signature"]], signature["signature"]))
        if not points:
            return unavailable("creator balance not readable from transactions", creator)
        first_time, first_balance, _, first_signature = points[0]
        peak = max(point[1] for point in points)
        last_time, last_balance, _, _ = points[-1]
        if peak <= 0:
            status = "none"
        elif last_balance <= peak * (1 - SOLD_THRESHOLD):
            status = "sold"
        else:
            status = "holding"
        # When the exit happened: the first reading after the peak that is below
        # the threshold -- not the last activity on the account, which can be a
        # dust transfer or an account close days later. If unread transactions
        # sit between that reading and the one before it, the exit happened
        # somewhere in that gap and the time is an upper bound.
        sold_after, sold_after_is_upper_bound, exit_signature, drop_signature = None, False, None, None
        if status == "sold" and first_time:
            peak_at = max(range(len(points)), key=lambda i: points[i][1])
            for i in range(peak_at + 1, len(points)):
                t, balance, idx, sig = points[i]
                if balance <= peak * (1 - SOLD_THRESHOLD):
                    sold_after = (t - first_time) if t else None
                    sold_after_is_upper_bound = idx - points[i - 1][2] > 1
                    # only when this reading is the exit itself, not a later one
                    exit_signature = None if sold_after_is_upper_bound else sig
                    drop_signature = sig
                    break
        if status == "sold" and drop_signature and is_liquidity_deposit(transactions.get(drop_signature, {})):
            return {
                "status": "pooled",
                "creator": creator,
                "token_program": program_used,
                "share_at_creation_pct": round(100.0 * first_balance / supply, 3),
                "peak_share_pct": round(100.0 * peak / supply, 3),
                "current_share_pct": round(100.0 * last_balance / supply, 3),
                "pooled_after_seconds": sold_after,
                "pool_signature": drop_signature,
                "first_seen_at": first_time,
                "transactions_on_record": len(ordered),
                "observed_at": observed_at,
                "source": "solana_rpc",
                "note": (
                    "The creator put the supply into a liquidity pool themselves. That is not "
                    "a sale; unless that liquidity is locked or burned, they can take it back."
                ),
            }
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
            "sold_after_is_upper_bound": sold_after_is_upper_bound,
            "first_buy_signature": first_signature,
            "exit_signature": exit_signature,
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
