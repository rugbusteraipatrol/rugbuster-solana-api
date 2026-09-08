"""What the scanner actually saw, split into claims that can each be checked.

The Avalanche twin of this module exists for the same reason: a single score
answers "how worried should I be" while hiding which question it answered. What
the mint's authorities can do, what the market looks like, who the issuer is and
what the creator has done before move independently and mean different things.

**Derived from what this service actually has, not from the Avalanche shape.**
That distinction is not pedantry. `withhold_verdict` was written here and reused
there against a response carrying `rug_score` and `rug_status`; it blanked the
two fields its author had in mind and left the stale verdict readable in the
others. Copying field names across services is how that happened, so every
dimension below reads Solana's own vocabulary -- the flags this service emits
and the RugCheck report it fetches -- and reports UNKNOWN where this service
simply does not have the input.

The most consequential of those is `creator_history`. **This service has none.**
There is no deployer lookup on the Solana path at all, so the dimension reports
NOT_COLLECTED rather than an empty record. A deployer with a long history of
rugs and one nobody has ever seen produce the same answer here, and that fact
should be visible in the response instead of being inferable only by reading the
source.

Additive by construction: no verdict is read or written here, pinned by a test.
"""

from __future__ import annotations

from typing import Any

OK = "OK"
UNKNOWN = "UNKNOWN"
NOT_COLLECTED = "NOT_COLLECTED"

# Flags this service emits, or normalises out of a RugCheck report, that
# describe a power the mint's controller holds.
AUTHORITY_FLAGS = {
    "mint_authority_active": "mint",
    "mint_authority_still_enabled": "mint",
    "freeze_authority_active": "freeze",
    "freeze_authority_still_enabled": "freeze",
    "balance_mutable_authority": "balance_mutable",
    "non_transferable": "non_transferable",
}

# Flags about how supply is distributed. Kept apart from authorities because
# they answer a different question and, crucially, because "concentrated" is
# not made harmless by a recognised issuer.
DISTRIBUTION_FLAGS = {
    "single_holder_ownership",
    "top_10_holders_high_ownership",
    "high_holder_concentration",
    "high_ownership",
}

METADATA_FLAGS = {"mutable_metadata", "missing_file_metadata"}


def _flags(payload: dict[str, Any]) -> list[str]:
    flags = payload.get("risk_flags") or []
    if isinstance(flags, str):
        return []
    return [str(flag) for flag in flags]


def technical_controls(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Powers the mint's controller holds, whoever they are.

    Reported whether or not `issuer_identity` recognises the mint. A curated
    identity explains why an authority exists; it does not remove a holder's
    exposure to it. Scoring the two together is what produced DANGER on
    JitoSOL and Jupiter's own LP token while memecoins passed -- the flags were
    right, reading them as a rug verdict was not.
    """
    flags = _flags(payload)
    authorities = sorted({AUTHORITY_FLAGS[flag] for flag in flags if flag in AUTHORITY_FLAGS})
    metadata = sorted(flag for flag in flags if flag in METADATA_FLAGS)

    token = (report or {}).get("token") if isinstance((report or {}).get("token"), dict) else {}
    read_directly = bool(token)

    return {
        # A response built purely from flags cannot prove an authority is
        # absent -- only that no flag said so. Without the raw account, the
        # honest answer is UNKNOWN.
        "status": OK if read_directly else (OK if authorities else UNKNOWN),
        "active_authorities": authorities,
        "metadata_flags": metadata,
        "read_from_account": read_directly,
        "note": (
            "Powers held now. Legitimate for some asset classes -- a bridge must "
            "mint, a regulated stablecoin must be able to freeze -- and still "
            "exposure for a holder. issuer_identity never cancels this."
        ),
    }


def distribution(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """How supply is spread, and whether we established who holds it.

    Separate from `technical_controls` on purpose. Concentration was folded in
    with administrative flags once already, on the assumption that on a large
    token the top holders are pools and bridges. That was an assumption, never
    a check: no ownership was established anywhere. Until it is, concentration
    is reported and not explained away.
    """
    flags = _flags(payload)
    signals = sorted(flag for flag in flags if flag in DISTRIBUTION_FLAGS)
    holders = (report or {}).get("totalHolders")

    return {
        "status": OK if signals or holders is not None else UNKNOWN,
        "concentration_signals": signals,
        "total_holders": holders,
        "owners_identified": False,
        "note": (
            "Who the concentrated holders are has not been established. A high "
            "holder count does not make concentration benign."
        ),
    }


def market(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Liquidity as reported by the upstream source, or UNKNOWN."""
    liquidity = (report or {}).get("totalMarketLiquidity")
    holders = (report or {}).get("totalHolders")
    if liquidity is None and holders is None:
        return {
            "status": UNKNOWN,
            "liquidity_usd": None,
            "total_holders": None,
            "note": "No market figures on this path; the cached verdict does not carry them.",
        }
    return {
        "status": OK,
        "liquidity_usd": liquidity,
        "total_holders": holders,
        "source": "rugcheck",
        "note": "Reported by RugCheck, not independently verified on chain.",
    }


def issuer_identity(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Whether the mint is on our curated canonical list.

    Deliberately blind to holder count and liquidity. Size is not identity, and
    treating it as identity is precisely how an unverified mint with both
    authorities live and concentrated ownership reached a clean verdict in the
    frozen PR #4.
    """
    recognised = "known_canonical_solana_mint" in _flags(payload)
    if recognised:
        return {
            "status": OK,
            "recognised": True,
            "basis": "curated_list",
            "note": "On our curated list of canonical Solana mints.",
        }
    return {
        "status": UNKNOWN,
        "recognised": False,
        "basis": "curated_list",
        "note": (
            "Not on the curated list. The issuer is unestablished here, which is "
            "the common case and not an accusation."
        ),
    }


def creator_history(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Not collected on this service.

    There is no deployer lookup anywhere on the Solana path. A creator with a
    long record of rugs and one nobody has ever seen currently produce the same
    answer, and reporting an empty record would present that absence as a clean
    one. It is reported as uncollected instead, so the gap is visible in the
    response rather than only in the source.
    """
    return {
        "status": NOT_COLLECTED,
        "creator": None,
        "prior_tokens_scanned_by_us": None,
        "confirmed_incidents": {"count": None, "status": NOT_COLLECTED},
        "note": (
            "This service does not look up deployer history. Absent coverage, "
            "not an absence of incidents."
        ),
    }


def coverage(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """How current the evidence is, and where the verdict came from."""
    flags = _flags(payload)
    provenance = next(
        (flag for flag in flags if flag.startswith("verdict_")),
        None,
    )
    return {
        "status": OK if payload.get("data_freshness") else UNKNOWN,
        "data_freshness": payload.get("data_freshness"),
        "observed_at": payload.get("observed_at"),
        "fetched_at": payload.get("fetched_at"),
        "age_seconds": payload.get("age_seconds"),
        "source": payload.get("source"),
        "verdict_provenance": provenance,
        "note": (
            "observed_at is when the evidence was gathered; fetched_at when this "
            "response was built. verdict_provenance says whether the number was "
            "recomputed or inherited from a stored row."
        ),
    }


DIMENSIONS = {
    "technical_controls": technical_controls,
    "distribution": distribution,
    "market": market,
    "issuer_identity": issuer_identity,
    "creator_history": creator_history,
    "coverage": coverage,
}


def build_evidence(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """The six dimensions, each derived only from its own inputs.

    `report` is the raw upstream response where the caller has one. Without it
    the market and account-level readings are UNKNOWN rather than inferred from
    the verdict, because a cached row genuinely does not carry them.
    """
    return {name: builder(payload, report) for name, builder in DIMENSIONS.items()}
