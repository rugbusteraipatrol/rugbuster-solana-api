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
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"


_ED25519_P = 2**255 - 19
_ED25519_D = (-121665 * pow(121666, _ED25519_P - 2, _ED25519_P)) % _ED25519_P
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _on_ed25519_curve(address: str) -> bool:
    """Whether a keypair can exist for this address.

    An off-curve address is a program-derived one: no private key is possible.
    Account ownership does not answer this and I once used it as though it did.
    """
    number = 0
    for character in address:
        number = number * 58 + _B58.index(character)
    raw = number.to_bytes(32, "big")
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _ED25519_P:
        return False
    p, d = _ED25519_P, _ED25519_D
    y2 = y * y % p
    u, v = (y2 - 1) % p, (d * y2 + 1) % p
    x = (u * pow(v, 3, p) % p) * pow(u * pow(v, 7, p) % p, (p - 5) // 8, p) % p
    vxx = v * x * x % p
    return vxx == u or vxx == (-u) % p


def _rpc(method: str, params: list) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        SOLANA_RPC, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "rugbuster-vault-verification/1.0"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:  # noqa: S310
        return json.loads(response.read()).get("result") or {}


def check_role_and_control(address: str) -> dict:
    """What the address is, and who can move what it holds.

    Authority and holdings say what an address does. This asks what it *is*:
    a System-Program-owned account is a keypair somebody holds, and a keypair's
    holdings are a person's, not a protocol's. A program-owned account has no
    private key -- only that program's code can move it.

    The follow-up matters as much: a program can be upgradeable, and then the
    code governing the vault can be replaced by whoever holds that authority.
    Reported rather than judged, because "program-owned" reads as beyond reach
    and is not.
    """
    info = _rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}]).get("value") or {}
    owner = str(info.get("owner") or "")
    result = {
        "owner_program": owner,
        "has_private_key": owner == SYSTEM_PROGRAM,
        "program_upgrade_authority": None,
        "program_upgrade_authority_is_single_key": None,
    }
    # Ownership is not the same question as whether a keypair exists. An
    # off-curve address has no valid ed25519 public key, so none can. Reading
    # only the owner field made me report this vault's upgrade authority as "a
    # single private key" when it is off-curve and therefore a PDA.
    result["is_off_curve"] = not _on_ed25519_curve(address)
    result["has_private_key"] = result["is_off_curve"] is False and owner == SYSTEM_PROGRAM
    if not owner or result["has_private_key"]:
        return result

    program = _rpc("getAccountInfo", [owner, {"encoding": "jsonParsed"}]).get("value") or {}
    if str(program.get("owner") or "") != UPGRADEABLE_LOADER:
        return result
    data = program.get("data")
    program_data = ((data or {}).get("parsed") or {}).get("info", {}).get("programData")
    if not program_data:
        return result
    record = _rpc("getAccountInfo", [program_data, {"encoding": "jsonParsed"}]).get("value") or {}
    authority = ((record.get("data") or {}).get("parsed") or {}).get("info", {}).get("authority")
    result["program_upgrade_authority"] = authority
    if authority:
        holder = _rpc("getAccountInfo", [authority, {"encoding": "jsonParsed"}]).get("value") or {}
        result["program_upgrade_authority_is_single_key"] = (
            _on_ed25519_curve(authority) and str(holder.get("owner") or "") == SYSTEM_PROGRAM
        )
    return result


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
    for address, entry in KNOWN_PROTOCOL_VAULTS.items():
        name = entry["name"]
        derived_from = sorted(authorities.get(address) or [])
        held = sorted(holdings.get(address) or [])
        control = check_role_and_control(address)

        recorded_owner = entry.get("owner_program")
        control_ok = (
            control["has_private_key"] is False
            and control["owner_program"] == recorded_owner
        )
        if derived_from and held and control_ok:
            print(f"OK  {name}")
            print(f"      authority of: {', '.join(derived_from)}")
            print(f"      holds:        {', '.join(held)}")
            print(f"      owned by program: {control['owner_program']} (no private key)")
            authority = control["program_upgrade_authority"]
            if authority:
                shape = ("a single key" if control["program_upgrade_authority_is_single_key"]
                         else "off-curve, so a program-derived address")
                print(f"      !! that program is upgradeable by {authority} ({shape});")
                print(f"         the control chain ends there and we have not identified it")
            else:
                print(f"      program is not upgradeable by a named authority")
            continue

        unverified.append((address, name))
        if not control_ok:
            if control["has_private_key"]:
                print(f"!!  {name}: System-Program-owned -- this is a keypair somebody "
                      f"holds, and a wallet's holdings are not a protocol's")
            elif control["owner_program"] != recorded_owner:
                print(f"!!  {name}: owner program is {control['owner_program']}, "
                      f"recorded as {recorded_owner}")
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
