"""Three readings taken straight from the chain for every answer.

Why this exists. A scan used to list up to seven questions under
`not_established` -- mostly powers the mint's controller might still hold --
because the only source for them was a RugCheck report that the cached paths
do not carry. Each of those questions has a direct answer on chain, one or two
RPC calls away, so this module asks the chain instead of leaving them open:

- contract: mint and freeze authority, Token-2022 extensions that can take or
  lock a holder's tokens (permanent delegate, non-transferable, transfer fee,
  transfer hook, accounts frozen by default), and whether the metadata can
  still be changed and by whom.
- holders: how much of the supply the ten largest ordinary wallets hold, with
  program-owned accounts (bonding curve, AMM pool, vaults) counted apart.
- funding: who sent the creator wallet its first SOL, and how long before the
  token was created.

Informational only. Nothing here reads or writes a verdict or a score; the
contract readings feed the evidence split, which is additive by construction.
Every part fails on its own: an RPC error becomes `status: unavailable` for
that part and never an error for the answer.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable
from rpc_http import rpc_post

RPC_TIMEOUT_SECONDS = 4
CACHE_TTL_SECONDS = 600
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOP_HOLDERS = 10
SIGNATURE_PAGE = 1000

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

Call = Callable[[str, list[Any] | dict[str, Any]], Any]


def rpc_url() -> str | None:
    value = os.getenv("SOLANA_RPC_URL", "").strip()
    return value or None


def _rpc(url: str) -> Call:
    def call(method: str, params: list[Any] | dict[str, Any]) -> Any:
        return rpc_post(url, method, params, RPC_TIMEOUT_SECONDS)

    return call


def _pct(amount: Any, supply: Any) -> float | None:
    try:
        amount, supply = int(amount), int(supply)
    except (TypeError, ValueError):
        return None
    if supply <= 0:
        return None
    return round(amount * 100.0 / supply, 3)


# --- contract ---------------------------------------------------------------


def read_contract(call: Call, mint: str) -> dict[str, Any]:
    account = call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
    value = account.get("value") or {}
    parsed = ((value.get("data") or {}).get("parsed") or {}) if isinstance(value.get("data"), dict) else {}
    info = parsed.get("info") or {}
    if parsed.get("type") != "mint":
        return {"status": "unavailable", "reason": "not a token mint account"}

    extensions: dict[str, Any] = {
        "permanent_delegate": None,
        "non_transferable": False,
        "transfer_fee_bps": None,
        "transfer_hook_program": None,
        "default_account_frozen": False,
    }
    # Token-2022 carries its metadata in the mint itself; when it does, that is
    # the authority that can change name and image, whatever an indexer says.
    embedded_update: tuple[bool, str | None] | None = None
    for ext in info.get("extensions") or []:
        name, state = ext.get("extension"), ext.get("state") or {}
        if name == "permanentDelegate":
            extensions["permanent_delegate"] = state.get("delegate")
        elif name == "nonTransferable":
            extensions["non_transferable"] = True
        elif name == "transferFeeConfig":
            fee = (state.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
            extensions["transfer_fee_bps"] = fee if isinstance(fee, int) else None
        elif name == "transferHook":
            extensions["transfer_hook_program"] = state.get("programId")
        elif name == "defaultAccountState":
            extensions["default_account_frozen"] = state.get("accountState") == "frozen"
        elif name == "tokenMetadata":
            embedded_update = (True, state.get("updateAuthority"))

    update_authority, mutable = None, None
    try:
        asset = call("getAsset", {"id": mint}) or {}
        mutable = asset.get("mutable")
        for authority in asset.get("authorities") or []:
            if "full" in (authority.get("scopes") or []) or "metadata" in (authority.get("scopes") or []):
                update_authority = authority.get("address")
                break
    except Exception:  # noqa: BLE001 -- metadata is optional, the mint reading stands
        pass
    if embedded_update is not None:
        update_authority = embedded_update[1]
        mutable = update_authority is not None

    contract = {
        "status": "ok",
        "token_program": "token-2022" if value.get("owner") == TOKEN_2022_PROGRAM else "spl-token",
        "mint_authority": info.get("mintAuthority"),
        "freeze_authority": info.get("freezeAuthority"),
        "metadata_mutable": mutable,
        "update_authority": update_authority if mutable else None,
        "extensions": extensions,
        "supply": info.get("supply"),
        "decimals": info.get("decimals"),
    }
    # Raw account fields in the vocabulary evidence.authority_readings uses:
    # None means the chain says the power is not there.
    raw = {
        "mintAuthority": contract["mint_authority"],
        "freezeAuthority": contract["freeze_authority"],
        "permanentDelegate": extensions["permanent_delegate"],
        "nonTransferable": True if extensions["non_transferable"] else None,
    }
    if mutable is not None:
        raw["updateAuthority"] = update_authority if mutable else None
    contract["raw_fields"] = raw
    return contract


# --- holders ----------------------------------------------------------------


def _is_program_address(address: str | None) -> bool:
    """True for an address off the ed25519 curve: a PDA no key can sign for."""
    if not address:
        return False
    try:
        from solders.pubkey import Pubkey

        return not Pubkey.from_string(address).is_on_curve()
    except Exception:  # noqa: BLE001 -- unreadable address: keep calling it a wallet
        return False


def read_holders(call: Call, mint: str, supply: Any = None) -> dict[str, Any]:
    if supply is None:
        supply = ((call("getTokenSupply", [mint]) or {}).get("value") or {}).get("amount")
    try:
        largest = ((call("getTokenLargestAccounts", [mint]) or {}).get("value")) or []
    except RuntimeError as exc:
        if "Too many accounts" in str(exc):
            return {"status": "unavailable", "reason": "too many holders to rank in one call (a large, established token)"}
        raise
    if not largest:
        return {"status": "unavailable", "reason": "no token accounts returned"}

    token_accounts = [a["address"] for a in largest]
    parsed = (call("getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}]) or {}).get("value") or []
    owners = []
    for acc in parsed:
        info = ((((acc or {}).get("data") or {}).get("parsed") or {}).get("info") or {}) if acc else {}
        owners.append(info.get("owner"))
    distinct = sorted({o for o in owners if o})
    owner_programs: dict[str, str | None] = {}
    if distinct:
        infos = (call("getMultipleAccounts", [distinct, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]) or {}).get("value") or []
        owner_programs = {addr: (info or {}).get("owner") for addr, info in zip(distinct, infos)}

    rows = []
    for account, owner in zip(largest, owners):
        program = owner_programs.get(owner) if owner else None
        # A pool's vault authority (Raydium CPMM and AMM v4, LaunchLab) is a
        # program address with no data, so the system program owns it; it has
        # no private key, so only the pool program can move those tokens.
        is_wallet = program in (SYSTEM_PROGRAM, None) and not _is_program_address(owner)
        kind = "wallet" if is_wallet else "program"
        rows.append({"owner": owner, "pct": _pct(account.get("amount"), supply), "kind": kind})

    wallets = [r for r in rows if r["kind"] == "wallet" and r["pct"]]
    programs = [r for r in rows if r["kind"] == "program" and r["pct"] is not None]
    top = wallets[:TOP_HOLDERS]
    return {
        "status": "ok",
        "top10_wallet_pct": round(sum(r["pct"] for r in top), 2),
        "largest_wallet_pct": top[0]["pct"] if top else 0.0,
        "program_held_pct": round(sum(r["pct"] for r in programs), 2),
        "top_wallets": top,
        "note": (
            "Program-owned accounts (bonding curve, AMM pool, vaults) are counted "
            "apart from wallets. Largest 20 token accounts read."
        ),
    }


# --- funding ----------------------------------------------------------------


def read_funding(call: Call, creator: str | None, created_at: Any = None) -> dict[str, Any]:
    if not creator:
        return {"status": "unavailable", "reason": "creator not known"}
    signatures = call("getSignaturesForAddress", [creator, {"limit": SIGNATURE_PAGE}]) or []
    if not signatures:
        return {"status": "unavailable", "reason": "creator wallet has no history"}
    if len(signatures) >= SIGNATURE_PAGE:
        return {
            "status": "busy_wallet",
            "creator": creator,
            "note": "The creator wallet has 1,000+ transactions; its first funding is out of reach here.",
        }
    first = signatures[-1]
    tx = call("getTransaction", [first["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]) or {}
    funded_at = tx.get("blockTime") or first.get("blockTime")
    funder, lamports, method = None, 0, None

    instructions = list(((tx.get("transaction") or {}).get("message") or {}).get("instructions") or [])
    for inner in ((tx.get("meta") or {}).get("innerInstructions") or []):
        instructions.extend(inner.get("instructions") or [])
    for ix in instructions:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get("program") != "system":
            continue
        info = parsed.get("info") or {}
        kind = parsed.get("type")
        if kind in ("transfer", "transferWithSeed") and info.get("destination") == creator:
            funder, lamports, method = info.get("source"), int(info.get("lamports") or 0), "transfer"
        elif kind == "withdrawFromNonce" and info.get("destination") == creator:
            # The nonce authority decided where the SOL went; that is the funder.
            funder, lamports, method = info.get("nonceAuthority"), int(info.get("lamports") or 0), "durable_nonce_withdrawal"
        elif kind == "createAccount" and info.get("newAccount") == creator:
            funder, lamports, method = info.get("source"), int(info.get("lamports") or 0), "create_account"
        if funder:
            break

    if not funder:
        # Fall back to balances: whoever lost the most while the creator gained.
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])]
        pre, post = (tx.get("meta") or {}).get("preBalances") or [], (tx.get("meta") or {}).get("postBalances") or []
        deltas = {k: (b - a) for k, a, b in zip(keys, pre, post)}
        gained = deltas.get(creator, 0)
        losers = sorted(((d, k) for k, d in deltas.items() if k != creator and d < 0))
        if gained > 0 and losers:
            funder, lamports, method = losers[0][1], gained, "balance_change"

    if not funder:
        return {
            "status": "unavailable",
            "creator": creator,
            "reason": "no SOL reached the creator in its first transaction",
            "signature": first["signature"],
        }

    lead = None
    if isinstance(created_at, (int, float)) and isinstance(funded_at, (int, float)):
        lead = int(created_at - funded_at)
    funder_balance = None
    try:
        funder_balance = round(int((call("getBalance", [funder]) or {}).get("value") or 0) / 1e9, 2)
    except Exception:  # noqa: BLE001 -- the balance is context, not the finding
        pass
    return {
        "status": "ok",
        "creator": creator,
        "funder": funder,
        "amount_sol": round(lamports / 1e9, 4),
        "method": method,
        "funded_at": funded_at,
        "seconds_before_creation": lead,
        "funder_balance_sol": funder_balance,
        "signature": first["signature"],
    }


# --- all three ----------------------------------------------------------------


def run(mint: str, creator: str | None = None, created_at: Any = None, call: Call | None = None) -> dict[str, Any] | None:
    """The three readings for `mint`, cached for ten minutes. None without an RPC."""
    if call is None:
        url = rpc_url()
        if not url:
            return None
        call = _rpc(url)

    key = f"{mint}:{creator or ''}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

    errored = False

    def safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        nonlocal errored
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- one part failing must not fail the answer
            errored = True
            return {"status": "unavailable", "reason": str(exc)[:120]}

    # One after another, not in parallel: the same RPC also serves the launch
    # and creator reads, and a burst is what draws its rate limit.
    started = time.time()
    result = {
        "contract": safe(lambda: read_contract(call, mint)),
        "holders": safe(lambda: read_holders(call, mint)),
        "funding": safe(lambda: read_funding(call, creator, created_at)),
    }
    result["lookup_seconds"] = round(time.time() - started, 2)
    result["source"] = "solana_rpc"
    result["effect_on_verdict"] = "none -- informational"

    if not errored:  # a transient failure must not be served for ten minutes
        with _cache_lock:
            _cache[key] = (now, result)
            if len(_cache) > 2000:
                _cache.pop(next(iter(_cache)))
    return result
