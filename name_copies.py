"""Other Solana tokens launched under the same ticker in the last day.

Why this exists. On 2026-10-06 a user brought MUNK (AKJx...pump). By then 27
Solana tokens carried the ticker MUNK, all created in the previous two days,
from unrelated creators and funders: one name catches on and dozens of copies
follow within hours. A buyer who picks the wrong address has bought a copy,
and nothing in a contract check or a creator check says so.

What it reports. The DexScreener search for the ticker, kept to Solana pairs
whose base token has exactly that symbol, grouped by mint: how many other mints
under the same ticker had their first pair created in the last 24 hours, and
which mint under that ticker traded the most in the last 24 hours. The search
returns at most a page of pairs, so a full page is reported as a floor
(`results_capped`), never as an exact count.

Informational only. Nothing here reads or writes a verdict or a score. A busy
ticker says nothing about whether this token is honest; it says the address
matters. Any failure becomes `status: unavailable` and never an error for the
answer.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={query}"
TIMEOUT_SECONDS = 4
WINDOW_SECONDS = 24 * 3600
CACHE_TTL_SECONDS = 600
SEARCH_PAGE = 30  # DexScreener returns at most this many pairs per search

Fetch = Callable[[str], Any]

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "RugBuster/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "source": "dexscreener"}


def _search(symbol: str, fetch: Fetch, now: float) -> Any:
    key = symbol.upper()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    data = fetch(SEARCH_URL.format(query=urllib.parse.quote(symbol)))
    with _cache_lock:
        _cache[key] = (now, data)
        if len(_cache) > 2000:
            _cache.pop(next(iter(_cache)))
    return data


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read(mint: str, symbol: Any, fetch: Fetch | None = None, now: float | None = None) -> dict[str, Any]:
    """Never raises: a failed read is `unavailable`."""
    symbol = str(symbol or "").strip()
    if not symbol:
        return unavailable("no ticker to search")
    now = time.time() if now is None else now
    try:
        data = _search(symbol, fetch or _fetch, now)
    except Exception as error:  # noqa: BLE001 -- informational; never fail the answer
        return unavailable(f"search failed: {str(error)[:80]}")
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        return unavailable("search returned no pair list")

    mints: dict[str, dict[str, float]] = {}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken") or {}
        address = str(base.get("address") or "")
        if not address or str(base.get("symbol") or "").strip().upper() != symbol.upper():
            continue
        entry = mints.setdefault(address, {"first_pair_at": 0.0, "volume_24h": 0.0})
        created = _number(pair.get("pairCreatedAt")) / 1000.0
        if created and (not entry["first_pair_at"] or created < entry["first_pair_at"]):
            entry["first_pair_at"] = created
        entry["volume_24h"] += _number((pair.get("volume") or {}).get("h24"))

    others = {address: entry for address, entry in mints.items() if address != mint}
    recent = [address for address, entry in others.items()
              if entry["first_pair_at"] and now - entry["first_pair_at"] < WINDOW_SECONDS]
    top_mint, top = max(mints.items(), key=lambda item: item[1]["volume_24h"], default=(None, None))
    own = mints.get(mint)
    return {
        "status": "ok",
        "source": "dexscreener",
        "symbol": symbol,
        "window_hours": WINDOW_SECONDS // 3600,
        "same_symbol_last_24h": len(recent),
        "same_symbol_seen": len(others),
        "results_capped": len(pairs) >= SEARCH_PAGE,
        "this_token_listed": own is not None,
        "this_token_volume_24h": round(own["volume_24h"], 2) if own else None,
        "most_traded": (
            {"mint": top_mint, "volume_24h": round(top["volume_24h"], 2), "is_this_token": top_mint == mint}
            if top_mint and top and top["volume_24h"] > 0 else None
        ),
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
