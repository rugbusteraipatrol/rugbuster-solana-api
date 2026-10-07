"""Who made the copies: do several tokens under one ticker share a maker?

Why this exists. On 2026-10-07, 28 Solana tokens carried the ticker MUNK,
launched within two days. Counting them (name_copies) says the address
matters; it does not say whether the crowd is many people chasing one name or
a few wallets farming it. Read by hand, 23 different wallets created those 28,
and one wallet created three of them, including the two most traded.

What it reports, per ticker. For up to MAX_COPIES of the most traded mints
under the ticker: the wallet that created each (from the RugCheck report) and
the wallet that sent that creator its first SOL (chain_checks.read_funding).
Then two groupings, each with the mints behind it:
- shared_creators: one wallet created two or more of the copies;
- shared_funders: one wallet funded the creators of two or more copies.
A funder holding EXCHANGE_BALANCE_SOL or more is counted as an exchange or a
large hot wallet and links nothing: thousands of unrelated people withdraw
from the same exchange wallet.

How it runs. Reading 15 creators and their funding takes tens of seconds, so
it never runs inside a scan. The first scan of a ticker starts it in a
background thread and answers `checking`; the result is kept per ticker for
CACHE_TTL_SECONDS and every scan of any copy reuses it. GET /copies polls it.

Informational only: no verdict or score reads any of this.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Callable

MAX_COPIES = 15
EXCHANGE_BALANCE_SOL = 5_000.0
CACHE_TTL_SECONDS = 3600
MAX_RUNNING = 2

Report = Callable[[str], dict[str, Any] | None]          # mint -> RugCheck report
Funding = Callable[[str, float | None], dict[str, Any]]  # creator, created_at -> funding reading
Mints = Callable[[str], dict[str, dict[str, float]]]     # symbol -> {mint: {first_pair_at, volume_24h}}

_lock = threading.Lock()
_results: dict[str, tuple[float, dict[str, Any]]] = {}
_running: set[str] = set()


def analyse(symbol: str, mints: dict[str, dict[str, float]], report: Report, funding: Funding) -> dict[str, Any]:
    """The full reading for one ticker. Pure apart from the two readers passed in."""
    ranked = sorted(mints.items(), key=lambda item: -item[1].get("volume_24h", 0.0))
    checked = ranked[:MAX_COPIES]
    most_traded = ranked[0][0] if ranked and ranked[0][1].get("volume_24h", 0) > 0 else None

    by_creator: dict[str, list[str]] = defaultdict(list)
    by_funder: dict[str, list[str]] = defaultdict(list)
    creators_by_funder: dict[str, set[str]] = defaultdict(set)
    exchange_funded = unreadable = 0
    for mint, meta in checked:
        creator = None
        try:
            creator = (report(mint) or {}).get("creator")
        except Exception:  # noqa: BLE001
            creator = None
        if not creator:
            unreadable += 1
            continue
        by_creator[creator].append(mint)
        try:
            reading = funding(creator, meta.get("first_pair_at") or None) or {}
        except Exception:  # noqa: BLE001
            reading = {}
        funder = reading.get("funder") if reading.get("status") == "ok" else None
        if not funder:
            continue
        balance = reading.get("funder_balance_sol")
        if isinstance(balance, (int, float)) and balance >= EXCHANGE_BALANCE_SOL:
            exchange_funded += 1
            continue
        by_funder[funder].append(mint)
        creators_by_funder[funder].add(creator)

    shared_creators = sorted(
        ({"wallet": wallet, "count": len(found), "mints": found, "includes_most_traded": most_traded in found}
         for wallet, found in by_creator.items() if len(found) >= 2),
        key=lambda group: -group["count"],
    )
    # A funder only links copies made by different creators; one creator's own
    # tokens are already a shared_creators group.
    shared_funders = sorted(
        ({"wallet": wallet, "count": len(found), "creators": sorted(creators_by_funder[wallet]), "mints": found,
          "includes_most_traded": most_traded in found}
         for wallet, found in by_funder.items() if len(creators_by_funder[wallet]) >= 2),
        key=lambda group: -group["count"],
    )
    return {
        "status": "ok",
        "symbol": symbol,
        "copies_total": len(mints),
        "checked": len(checked),
        "creators_read": len(checked) - unreadable,
        "distinct_creators": len(by_creator),
        "shared_creators": shared_creators,
        "shared_funders": shared_funders,
        "exchange_funded": exchange_funded,
        "most_traded": most_traded,
        "computed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def peek(symbol: str, now: float | None = None) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    key = symbol.strip().upper()
    with _lock:
        hit = _results.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        if key in _running:
            return {"status": "checking", "symbol": symbol}
    return None


def get_or_start(symbol: str, mints: Mints, report: Report, funding: Funding,
                 spawn: Callable[[Callable[[], None]], None] | None = None) -> dict[str, Any]:
    """The cached reading, or `checking` after starting one in the background."""
    symbol = str(symbol or "").strip()
    if not symbol:
        return {"status": "unavailable", "reason": "no ticker"}
    found = peek(symbol)
    if found is not None:
        return found
    key = symbol.upper()
    with _lock:
        if key in _running:
            return {"status": "checking", "symbol": symbol}
        if len(_running) >= MAX_RUNNING:
            return {"status": "busy", "symbol": symbol, "reason": "other tickers are being checked; try again shortly"}
        _running.add(key)

    def job() -> None:
        try:
            result = analyse(symbol, mints(symbol), report, funding)
        except Exception as error:  # noqa: BLE001
            result = {"status": "unavailable", "symbol": symbol, "reason": str(error)[:120]}
        with _lock:
            _results[key] = (time.time(), result)
            _running.discard(key)
            if len(_results) > 2000:
                _results.pop(next(iter(_results)))

    (spawn or (lambda fn: threading.Thread(target=fn, name=f"copy-makers-{key}", daemon=True).start()))(job)
    return peek(symbol) or {"status": "checking", "symbol": symbol}
