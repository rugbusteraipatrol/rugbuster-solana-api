import json
import re
from typing import Any


LABEL_FALLBACKS = {"GOOD": 15, "WARN": 55, "DANGER": 90}


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float) -> int:
    return max(0, min(100, round(value)))


def _linear(value: float, x0: float, x1: float, y0: float, y1: float) -> float:
    ratio = max(0.0, min(1.0, (value - x0) / (x1 - x0)))
    return y0 + ratio * (y1 - y0)


def rugcheck_to_risk(score: float) -> int:
    """Calibrate RugCheck's broad score range into a 0-100 risk percentage."""
    if score < 0:
        return 55
    if score < 100:
        return round(_linear(score, 0, 100, 5, 15))
    if score < 2000:
        return round(_linear(score, 100, 2000, 30, 50))
    if score < 5000:
        return round(_linear(score, 2000, 5000, 50, 65))
    if score < 12000:
        return round(_linear(score, 5000, 12000, 65, 78))
    if score < 72000:
        return round(_linear(score, 12000, 72000, 78, 90))
    return round(min(98, _linear(score, 72000, 162000, 90, 98)))


def _load_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _snake_case(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).strip().lower())
    return text.strip("_")


def _first_number(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(record.get(key))
        if value is not None:
            return value
    return None


def _text_blob(record: dict[str, Any]) -> str:
    return "\n".join(str(record.get(key) or "") for key in ("input", "output"))


def _rugcheck_score(record: dict[str, Any]) -> float | None:
    score = _first_number(record, "rugcheck_score")
    if score is not None:
        return score
    match = re.search(r"RugCheck Score:\s*(-?[0-9]+(?:\.[0-9]+)?)", _text_blob(record), re.I)
    return _number(match.group(1)) if match else None


def _creator_rug_rate(record: dict[str, Any]) -> float | None:
    value = _first_number(record, "creator_rug_rate")
    if value is not None:
        return value
    match = re.search(r"Creator (?:rug rate|History):[^\n]*?([0-9]+(?:\.[0-9]+)?)%", _text_blob(record), re.I)
    return _number(match.group(1)) if match else None


def _token_identity(record: dict[str, Any]) -> tuple[str | None, str | None]:
    name = record.get("token_name") or record.get("name")
    symbol = record.get("token_symbol") or record.get("symbol")
    if name or symbol:
        return name, symbol
    match = re.search(r"^Token:\s*(.+?)(?:\s+\(([^()]+)\))?\s*$", _text_blob(record), re.M)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip() if match.group(2) else None


def _concentration_risk(record: dict[str, Any]) -> str | None:
    value = record.get("v6_concentration_risk")
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _rugcheck_reliable(record: dict[str, Any], rugcheck_score: float | None) -> bool:
    """A RugCheck score can't reflect real risk before a token has any trading
    history -- the API appears to return a near-zero default in that window,
    which this scorer previously treated as a confirmed low-risk assessment.

    Proxy for "no trading history yet": top5 holder concentration >= 99% (i.e.
    still essentially just the creator's own allocation). Confirmed against a
    9-token benchmark sample of missed DANGER tokens: every one had
    rugcheck_score == 1 and v6_top5_holder_pct == 100.0. There is no token-age
    timestamp in this record shape to gate on directly, so this holder-based
    proxy is the closest available signal for "RugCheck hasn't seen real
    activity yet."
    """
    if rugcheck_score is None:
        return False
    if rugcheck_score >= 10:
        return True
    top5 = _first_number(record, "v6_top5_holder_pct")
    return not (top5 is not None and top5 >= 99)


def _existing_flags(record: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for key in ("risk_flags", "flags", "cia_flags"):
        value = record.get(key)
        if isinstance(value, list):
            flags.extend(_snake_case(item) for item in value if _snake_case(item))
        elif isinstance(value, str) and value.strip():
            flags.extend(
                normalized
                for item in value.split(",")
                if (normalized := _snake_case(item))
            )
    return flags


def derive_score(
    record: dict[str, Any],
    row_label: str | None,
    trust_stored_score: bool = True,
) -> tuple[int, float | None, list[str]]:
    """
    Derive risk transparently.

    Priority:
    1. Use precomputed risk_percent unchanged.
    2. Otherwise calibrate RugCheck, then apply documented signal boosts.
    3. If no numeric signal exists, map cached label. Unknown/malformed rows use
       WARN=55 so incomplete evidence never becomes a false GOOD.
    """
    flags = _existing_flags(record)
    # A stored number produced by rules we cannot name is not evidence about
    # the token under the rules running now. The caller decides whether to
    # trust it; when it does not, the raw signals below are used instead.
    precomputed = _first_number(record, "risk_percent") if trust_stored_score else None
    if not trust_stored_score:
        flags.append("stored_score_ignored_unknown_provenance")
    rugcheck_score = _rugcheck_score(record)
    rugcheck_usable = _rugcheck_reliable(record, rugcheck_score)

    if precomputed is not None:
        # The stored number is used unchanged, so this verdict was produced by
        # whichever version of the scorer wrote the row -- not by the version
        # running now. Recorded, because a response that carries the current
        # scoring_version while serving an inherited number misstates its own
        # provenance.
        risk = _clamp(precomputed)
        flags.append("verdict_from_stored_risk_percent")
    else:
        label = str(row_label or record.get("label") or "").upper()
        if rugcheck_score is not None and rugcheck_usable:
            risk = rugcheck_to_risk(rugcheck_score)
            flags.append("verdict_recomputed_from_rugcheck_score")
        elif rugcheck_score is not None:
            # RugCheck returned a score, but this token has no real trading
            # history yet (see _rugcheck_reliable) -- that score is not a
            # confirmed low-risk assessment, so don't let it set a low floor.
            # Fall back to the same neutral WARN default used for missing
            # data, not the row's own (possibly already-wrong) stored label.
            risk = 55
            flags.append("rugcheck_score_unreliable_fresh_token")
        else:
            # Mapped from a stored label: the weakest provenance of the three,
            # since the label itself came from an earlier run.
            risk = LABEL_FALLBACKS.get(label, 55)
            flags.append("verdict_from_stored_label")

        creator_rate = _creator_rug_rate(record)
        if creator_rate is not None and creator_rate >= 80:
            risk = max(risk, 85)
            flags.append("creator_rug_rate_high")
        elif creator_rate is not None and creator_rate >= 40:
            risk = max(risk, 70)
            flags.append("creator_rug_rate_elevated")

        text = _text_blob(record).lower()
        fake_lp_lock = bool(record.get("cia_fake_lp_lock")) or "fake lp lock" in text or "fake lock" in text
        if fake_lp_lock:
            risk = min(98, risk + 15)
            flags.append("fake_lp_lock")

        lp_locked_pct = _first_number(record, "lp_locked_pct", "lp_lock_pct")
        if lp_locked_pct is not None and 0 < lp_locked_pct < 50:
            risk = min(98, risk + 10)
            flags.append("short_lp_lock")

        sniped = bool(record.get("cia_sniped")) or bool(re.search(r"sniped in\s*-?[0-9]+ms", text))
        if sniped:
            risk = min(98, risk + 10)
            latency = _first_number(record, "cia_deployment_latency_ms")
            flags.append(f"sniped_in_{max(0, round(latency))}ms" if latency is not None else "sniped_at_launch")

    # Preserve important structured flags even when risk_percent was precomputed.
    if record.get("cia_fake_lp_lock"):
        flags.append("fake_lp_lock")
    if record.get("cia_sniped"):
        latency = _first_number(record, "cia_deployment_latency_ms")
        flags.append(f"sniped_in_{max(0, round(latency))}ms" if latency is not None else "sniped_at_launch")
    creator_rate = _creator_rug_rate(record)
    if creator_rate is not None and creator_rate >= 80:
        flags.append("creator_rug_rate_high")

    # A CRITICAL internal concentration signal is our own computed evidence --
    # it must never be silently outvoted by an external RugCheck score (or any
    # other path above) landing in the GOOD range. Floor to WARN so a real
    # CIA/V6 red flag can't slip through as "no major red flags detected".
    if _concentration_risk(record) == "CRITICAL" and risk < 35:
        risk = 55
        flags.append("concentration_critical_override")

    return _clamp(risk), rugcheck_score, sorted(set(flag for flag in flags if flag))


# The only field that names the scoring rules. It is deliberately alone.
#
# An earlier version also accepted `engine_version` and `data_contract_version`,
# and compared whichever it found against SCORING_VERSION by string equality.
# Those are different namespaces: a data-contract version describes the shape of
# a response and an engine version describes a different service. A row whose
# data-contract version happened to read "2026.09.3" was therefore served as
# though the current scoring rules had produced its number. Equality of two
# opaque strings from different namespaces establishes nothing.
#
# If an equivalence is ever established between namespaces it belongs in a
# documented mapping, not in a tuple that treats them as interchangeable.
SOURCE_SCORING_VERSION_FIELD = "scoring_version"


def stored_scoring_version(record: dict[str, Any]) -> str | None:
    """Which scoring rules produced the number in this record, if it says.

    Returns None when the record does not name them -- which is every collector
    row today. Nothing else in the record is read as a substitute.
    """
    value = record.get(SOURCE_SCORING_VERSION_FIELD)
    return str(value) if value else None


def has_recomputable_evidence(record: dict[str, Any]) -> bool:
    """Can a verdict be derived from this record's raw signals alone?

    If so, an untrustworthy stored number does not have to mean withholding: the
    evidence is present and the current rules can be applied to it. Only when it
    is absent does inheritance become the sole option, and then it is refused.
    """
    score = _rugcheck_score(record)
    return score is not None and _rugcheck_reliable(record, score)


def carries_rugcheck_report(record: dict[str, Any]) -> bool:
    """Does this stored record hold the upstream report itself?

    When it does, the row is not a number of unknown provenance -- it is the
    same evidence a live scan would fetch, and the current rules can be applied
    to it directly. That is the only way a cached answer and a fresh one can be
    guaranteed to agree.
    """
    if not isinstance(record, dict):
        return False
    if _number(record.get("score_normalised")) is not None:
        return True
    has_risks = isinstance(record.get("risks"), list)
    has_token = isinstance(record.get("token"), dict) or isinstance(record.get("tokenMeta"), dict)
    return has_risks and has_token


def score_scan_row(row: dict[str, Any], trust_stored_score: bool = True) -> dict[str, Any]:
    record = _load_record(row.get("full_record"))

    # Same evidence, same rules, same answer. Before this, the caps lived on the
    # live path alone, so a token could be WARN when scanned and DANGER when
    # read back from cache -- the verdict was a property of where the answer
    # came from rather than of the token.
    if carries_rugcheck_report(record):
        if not record.get("mint") and row.get("mint"):
            record = {**record, "mint": row["mint"]}
        result = score_live_rugcheck_report(record)
        result["risk_flags"] = sorted(
            set(result["risk_flags"]) | {"verdict_recomputed_from_stored_evidence"}
        )
        return result

    risk_score, rugcheck_score, flags = derive_score(
        record, row.get("label"), trust_stored_score=trust_stored_score
    )
    # The record holds no upstream report, so there is nothing to recompute
    # from. The ceilings still apply: whatever the number's provenance, it may
    # not claim more than the findings recorded alongside it support.
    risk_score, flags = apply_verdict_ceilings(
        risk_score, flags,
        rugged=bool(record.get("rugged")),
        curated=is_curated_canonical_mint({"mint": row.get("mint") or record.get("mint") or ""}),
    )
    flags = sorted(set(flag for flag in flags if flag))
    label = "GOOD" if risk_score < 35 else "WARN" if risk_score < 70 else "DANGER"
    token_name, token_symbol = _token_identity(record)
    return {
        "risk_score": risk_score,
        "label": label,
        "rugcheck_score": round(rugcheck_score) if rugcheck_score is not None else None,
        "risk_flags": flags,
        "token_name": token_name,
        "token_symbol": token_symbol,
    }


# Bump when a change alters what a verdict means. The live cache is scoped to
# this value, so a scoring change stops serving verdicts computed under the old
# rules instead of leaking them for the rest of the cache TTL.
SCORING_VERSION = "2026.09.9"


# Canonical Solana mints. RugCheck returns no holder or liquidity data at all
# for some of these (USDC comes back holders=0, liquidity=0, score=1), which is
# indistinguishable from an empty token by any measurement we can take. These
# are known by curation, not by the live API, exactly as is_known_chain_asset
# works on the AVAX path.
KNOWN_SOLANA_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "WSOL",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "WETH",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "WBTC",
    # Source-backed canonical mints: PayPal developer docs, Raydium docs,
    # Jito Foundation deployed-program docs, and jup-ag token categories.
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JITOSOL",
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4": "JLP",
}

# Below these, the token has essentially no economic life: the 17 on-chain
# confirmed pump.fun rugs all sat at 2-5 holders and under $4k liquidity,
# while a fresh-but-real token in the same sample had 924 holders and $8.8k.
MIN_HOLDERS_FOR_CLEAN_VERDICT = 50
MIN_LIQUIDITY_FOR_CLEAN_VERDICT = 5_000

# RugCheck's floor score. It means "our static checks found nothing", which on
# an unproven token is an absence of evidence, not evidence of safety.
RUGCHECK_FLOOR_SCORE = 10


# Risk items that describe how a token is *administered*, not whether its
# deployer is leaving. Retained mint authority is how a bridge issues wrapped
# assets and how a liquid-staking token credits rewards; retained freeze
# authority is how a regulated stablecoin complies with a court order; "top 10
# holders" on a token with three quarters of a million holders is pools and
# treasuries, not one wallet holding the exit.
#
# These are worth disclosing and are kept in the flags. They are not, on their
# own, evidence of a rug.
ADMINISTRATIVE_RISK_ITEMS = {
    # Our own flag names for the same administrative facts, which sit in the
    # flag list alongside RugCheck's wording.
    "mint_authority_active",
    "freeze_authority_active",
    "known_canonical_solana_mint",
    # RugCheck's wording.
    "mint_authority_still_enabled",
    "freeze_authority_still_enabled",
    "mutable_metadata",
    "missing_file_metadata",
}

# These findings can justify review, but do not by themselves establish a rug.
# They stay visible in the response and can keep a token out of GOOD. DANGER is
# reserved for a stronger signal or a combination outside this set.
DISTRIBUTION_RISK_ITEMS = {
    "high_holder_concentration",
    "high_holder_correlation",
    "high_market_cap_per_holder",
    "high_ownership",
    "low_amount_of_lp_providers",
    "single_holder_ownership",
    "top_10_holders_high_ownership",
}
NON_CONCLUSIVE_RISK_ITEMS = ADMINISTRATIVE_RISK_ITEMS | DISTRIBUTION_RISK_ITEMS
MAX_NON_CONCLUSIVE_RISK = 69

# A symbol collision says the address is not the curated asset a user could
# reasonably believe the symbol identifies. That is worth stopping on, and it is
# not proof of anything about the token: Solana tickers are not unique, and a
# project may legitimately use a name another project already uses.
#
# So it raises the token to WARN and no further. DANGER stays reserved for the
# case where something independent of the name is also wrong.
IDENTITY_MISMATCH_RISK = 60
IDENTITY_RISK_ITEMS = {
    "identity_mismatch",
    "symbol_matches_curated_asset_but_mint_differs",
}

# Flags that describe how a verdict was reached rather than what was found.
# They must not be read as findings: counting one as a signal would let the act
# of recording provenance change the verdict it records.
EVIDENCE_NEUTRAL_FLAGS = {
    "verdict_from_stored_risk_percent",
    "verdict_recomputed_from_rugcheck_score",
    "verdict_recomputed_from_stored_evidence",
    "verdict_from_stored_label",
    "stored_score_ignored_unknown_provenance",
    "rugcheck_score_unreliable_fresh_token",
    "concentration_critical_override",
    "curated_mint_administrative_flags_only",
    "non_conclusive_signals_capped_at_warn",
    "identity_mismatch_capped_at_warn",
    "live_scan_cannot_clear_token",
    "rugcheck_returned_no_holder_or_liquidity_data",
    "too_few_holders_to_clear",
    "insufficient_liquidity_to_clear",
    "distribution_findings_not_supported_by_holder_table",
}

# Annotations naming who holds a concentration. They record an identification,
# not a finding, so they are excluded by prefix.
IDENTIFIED_HOLDER_FLAG_PREFIX = "concentration_held_by_"


# Holder addresses we can name, and the three checks that let one in.
#
# The exemption is narrow and worth stating exactly: it says the concentration
# is *identified*, not that it is safe. A distribution finding asserts a large
# share sits with someone nobody has named. Naming the holder contradicts that
# assertion and nothing else. The share is still reported, and still large.
#
# Three conditions, and an entry needs all three:
#
#   1. It is the mint or freeze authority of a mint already on
#      KNOWN_SOLANA_MINTS -- a derivation from something curated by hand.
#   2. It is observed holding one of those mints. The list is read against
#      holder tables, so an address that only ever appears as an authority
#      cannot justify a holder exemption.
#   3. Its role and control are established on chain, not inferred from the
#      first two. An address can be an authority and a holder and still be a
#      person's wallet.
#
# Condition 2 was added after the Wormhole token bridge authority failed it --
# an authority that appears in no holder table at all. Condition 3 was added on
# review, correctly: authority plus holdings say what an address *does*, not
# what it *is* or who moves it.
#
# What condition 3 established for the one remaining entry, read from mainnet
# on 9 September 2026:
#
#   AVzP2Ge... is owned by program PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu,
#   not by the System Program. It is a program-derived address: no private key
#   exists for it, and only that program's code can move what it holds. That is
#   the role -- the Jupiter Perpetuals pool authority, holding perps collateral,
#   which is why it is the largest holder of both curated wrapped assets.
#
# And what it did not establish, which belongs in the same breath:
#
#   That program is upgradeable, and its ProgramData names upgrade authority
#   5myNNmEmPm3UAnJ2ggLEpnTFb9t9Gk8369wKw6n3uAKx.
#
#   I first reported that authority as "a single private key" because its
#   account owner is the System Program. That was wrong, and the correction
#   matters in the token's favour: the address is **off the ed25519 curve**, so
#   no keypair can exist for it. It is a PDA of some program I have not
#   identified. Account ownership and curve membership are different questions
#   and I answered the second with the first.
#
#   What is established: the vault has no private key, and neither does the
#   authority that can replace the program governing it. What is not: which
#   program that authority belongs to, and therefore who can actually trigger
#   an upgrade.
#
# So the exemption identifies and stops there. It does not reduce the
# concentration risk -- naming a holder answers who, not how much sits in one
# account, and the account is controlled by a program that can be replaced by
# something we cannot name.
#
# `qa/verify_protocol_vaults.py` re-checks all three against live data.
KNOWN_PROTOCOL_VAULTS = {
    "AVzP2GeRmqGphJsMxWoqjpUifPpCret7LqWhD8NWQK49": {
        "name": "Jupiter Perps pool authority",
        # 1. derivation
        "authority_of": ["JLP.mintAuthority", "JLP.freezeAuthority"],
        # 2. observed holdings, 9 September 2026
        "holds": ["WETH 64.3%", "WBTC 60.4%", "JLP 28.0%"],
        # 3. role and control, read from mainnet
        "owner_program": "PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu",
        "has_private_key": False,
        "program_upgrade_authority": "5myNNmEmPm3UAnJ2ggLEpnTFb9t9Gk8369wKw6n3uAKx",
        # Off the ed25519 curve, so no keypair exists for it either. Which
        # program derives it, and therefore who can actually trigger an
        # upgrade, is not established.
        "program_upgrade_authority_is_single_key": False,
        "program_upgrade_authority_is_off_curve": True,
    },
}

# Names only, for anything that just wants to print who a holder is.
PROTOCOL_VAULT_NAMES = {
    address: entry["name"] for address, entry in KNOWN_PROTOCOL_VAULTS.items()
}

# Concentration at or below these shares does not support the finding RugCheck
# raised. Deliberately generous: the point is to catch a claim the report's own
# holder table contradicts, not to argue about where concentration begins.
#
# HNT is the worked example. RugCheck raised "Single holder ownership" on a
# token whose largest holder holds 4.3% and whose top ten hold 26.8%, and we
# repeated that claim in our own response because we read the name of the flag
# and never the numbers sitting beside it in the same document.
MEASURED_TOP1_CONCENTRATION_MAX = 25.0
MEASURED_TOP10_CONCENTRATION_MAX = 50.0

# Which measurement answers which claim. A flag with no measurement here (for
# example holder correlation) is left alone: we cannot contradict what we
# cannot measure.
TOP1_DISTRIBUTION_ITEMS = {"single_holder_ownership", "high_ownership"}
TOP10_DISTRIBUTION_ITEMS = {
    "top_10_holders_high_ownership",
    "high_holder_concentration",
}


def _top_holders(report: dict[str, Any]) -> list[dict[str, Any]]:
    holders = report.get("topHolders")
    return [h for h in holders if isinstance(h, dict)] if isinstance(holders, list) else []


def measured_concentration(report: dict[str, Any]) -> dict[str, float | None]:
    """What the report's own holder table says, rather than what it claims.

    Returns None for a share the table cannot support -- an absent table is not
    a well-distributed token, and must not be read as one.
    """
    holders = _top_holders(report)
    if not holders:
        return {"top1_pct": None, "top10_pct": None, "identified_pct": None}
    shares = [_number(h.get("pct")) or 0.0 for h in holders]
    identified = sum(
        share for h, share in zip(holders, shares)
        if str(h.get("owner") or "") in KNOWN_PROTOCOL_VAULTS
    )
    return {
        # max, not shares[0]. The upstream table is normally sorted, and
        # relying on that made "the largest holder" mean "whoever the source
        # listed first" -- an assumption about someone else's output that
        # nothing here enforces.
        "top1_pct": max(shares),
        "top10_pct": sum(sorted(shares, reverse=True)[:10]),
        "identified_pct": identified,
    }


def unidentified_concentration(report: dict[str, Any]) -> dict[str, float | None]:
    """Concentration with the named accounts marked, not removed.

    This used to set named vaults aside and report what was left, which made
    identification reduce the risk. Review separated the two, and it was right
    to: naming a holder answers *who*, and the risk of a large share sitting in
    one account is not a question about its name. A protocol vault can be
    drained by whoever controls the protocol -- for the one entry on our list
    that is a program upgradeable by an authority we have not identified.

    So the figures returned are the measured ones. `identified_pct` says how
    much of the concentration we can attribute, and nothing subtracts it.
    """
    return measured_concentration(report)


def unsupported_distribution_flags(report: dict[str, Any], flags: list[str]) -> list[str]:
    """Distribution findings this report's own holder table does not support.

    Two ways a finding fails: the concentration is not there at all, or it is
    there and belongs to an account we can name. Either way the flag stays in
    the response -- what changes is whether a verdict may rest on it.
    """
    measured = unidentified_concentration(report)
    if measured["top1_pct"] is None:
        return []

    unsupported: list[str] = []
    for flag in set(flags):
        if flag in TOP1_DISTRIBUTION_ITEMS and measured["top1_pct"] <= MEASURED_TOP1_CONCENTRATION_MAX:
            unsupported.append(flag)
        elif flag in TOP10_DISTRIBUTION_ITEMS and measured["top10_pct"] <= MEASURED_TOP10_CONCENTRATION_MAX:
            unsupported.append(flag)
    return sorted(unsupported)


def findings_only(flags: list[str]) -> list[str]:
    """The flags that assert something about the token."""
    return [
        flag for flag in flags
        if flag not in EVIDENCE_NEUTRAL_FLAGS
        and not flag.startswith(IDENTIFIED_HOLDER_FLAG_PREFIX)
    ]


def independent_serious_signals(flags: list[str]) -> list[str]:
    """Findings that are neither a disclosure nor the name collision itself.

    This is what a DANGER verdict on a name collision has to rest on. Low
    liquidity, an unlocked LP, a snipe, a deployer with a history: each is a
    statement about the token that holds whatever it is called.
    """
    return sorted(
        flag for flag in findings_only(flags)
        if flag not in NON_CONCLUSIVE_RISK_ITEMS and flag not in IDENTITY_RISK_ITEMS
    )

# Concentration is deliberately NOT in that set. The first version of this
# change included it, on the reasoning that across three quarters of a million
# holders the top ten must be pools and bridges. That was an assumption and was
# never checked: nothing here establishes who any holder is. An independent
# review reproduced the consequence -- an unverified mint with both authorities
# live and concentrated ownership came out GOOD 20.
#
# Identity may explain an authority. It cannot explain away ownership nobody
# has identified.

# Holder-count and liquidity thresholds used to gate this. They are gone.
# Size is not issuer verification: a token can be large, widely held and still
# be controlled by someone nobody has identified, and treating scale as
# identity is what let the counterexample through. Suppression is now gated on
# membership of KNOWN_SOLANA_MINTS -- a curated list, verified by hand, with a
# named asset behind each entry.

# Where a curated mint with nothing but administrative flags lands. Below the
# GOOD threshold, but deliberately not zero: the authorities are real, they are
# still listed in the response, and a curated identity explains them rather
# than removing them.
CURATED_ADMINISTRATIVE_RISK = 20


def is_curated_canonical_mint(report: dict[str, Any]) -> bool:
    """Is this mint one we have identified by hand?

    Deliberately blind to holder count and liquidity. Whether an authority is
    expected depends on who holds it -- a bridge must mint, a regulated
    stablecoin must be able to freeze -- and that is a question about identity,
    which size cannot answer.
    """
    mint = str(report.get("mint") or report.get("address") or "").strip()
    return mint in KNOWN_SOLANA_MINTS


def administrative_flags_only(risk_items: list[str]) -> bool:
    """True when every risk RugCheck raised is about administration.

    An empty list is not enough on its own -- the caller pairs this with the
    established test, because "no risks found" on an unproven mint is the
    absence-of-evidence case `live_report_supports_clean_verdict` handles.
    """
    return all(item in ADMINISTRATIVE_RISK_ITEMS for item in risk_items)


def non_conclusive_flags_only(risk_items: list[str]) -> bool:
    """Whether every finding is a control or distribution disclosure.

    Provenance flags are not findings and are ignored here, so a row that
    records how its number was obtained does not thereby escape the cap.
    """
    findings = findings_only(list(risk_items))
    return bool(findings) and all(item in NON_CONCLUSIVE_RISK_ITEMS for item in findings)


def apply_verdict_ceilings(
    risk: int,
    flags: list[str],
    *,
    rugged: bool,
    curated: bool,
    unsupported: tuple[str, ...] | list[str] = (),
) -> tuple[int, list[str]]:
    """The rules that decide how far a verdict may go, applied in one place.

    Both paths call this. When they did not, the cap existed on the live path
    only and the same evidence returned WARN from a fresh scan and DANGER from
    a cached row -- which made the answer a property of our cache rather than
    of the token.
    """
    flags = list(flags)
    # A finding the evidence does not support is still reported, but a verdict
    # may not rest on it.
    judged = [flag for flag in findings_only(flags) if flag not in set(unsupported)]

    if not rugged and curated and administrative_flags_only(judged)             and risk > CURATED_ADMINISTRATIVE_RISK:
        risk = CURATED_ADMINISTRATIVE_RISK
        flags.append("curated_mint_administrative_flags_only")

    # Active authorities and concentrated ownership are material disclosures,
    # but without a stronger event or exploit signal they do not establish a
    # rug. Keep the token in WARN and preserve every underlying flag.
    if not rugged and non_conclusive_flags_only(judged) and risk > MAX_NON_CONCLUSIVE_RISK:
        risk = MAX_NON_CONCLUSIVE_RISK
        flags.append("non_conclusive_signals_capped_at_warn")

    # A name collision on its own stops at WARN. It is a reason to look, not a
    # finding about the token, and DANGER would claim more than it establishes.
    if not rugged and any(flag in IDENTITY_RISK_ITEMS for flag in flags)             and not independent_serious_signals(flags)             and risk > MAX_NON_CONCLUSIVE_RISK:
        risk = MAX_NON_CONCLUSIVE_RISK
        flags.append("identity_mismatch_capped_at_warn")

    return _clamp(risk), flags


def canonical_symbol_mint_mismatch(report: dict[str, Any]) -> bool:
    """A protected symbol was supplied with a different mint.

    This establishes an address mismatch, not malicious intent. It is still a
    high transaction-safety risk because the address is not the curated asset a
    user could reasonably believe the symbol identifies.
    """
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    token_meta = report.get("tokenMeta") if isinstance(report.get("tokenMeta"), dict) else {}
    symbol = str(token_meta.get("symbol") or token.get("symbol") or "").strip().upper()
    mint = str(report.get("mint") or report.get("address") or "").strip()
    expected = {known_mint for known_mint, known_symbol in KNOWN_SOLANA_MINTS.items()
                if known_symbol.upper() == symbol}
    return bool(symbol and expected and mint not in expected)


def live_report_supports_clean_verdict(report: dict[str, Any]) -> tuple[bool, str]:
    """Can a live RugCheck report alone justify calling a token GOOD?

    It cannot when the score sits at RugCheck's floor and the token shows no
    economic life. Measured against ground truth: 16 of 17 pump.fun tokens
    independently confirmed as creator dumps scored GOOD with risk 1 through
    this path, because the path trusted a floor score from a mint with one or
    two holders. RugCheck was not wrong -- its static checks genuinely find
    nothing on those mints -- it was being asked a question it cannot answer.

    Returns (supported, reason). A false does NOT mean the token is dangerous;
    it means this path has no basis to clear it.
    """
    score = _number(report.get("score"))
    if score is not None and score >= RUGCHECK_FLOOR_SCORE:
        return True, ""

    holders = _number(report.get("totalHolders"))
    liquidity = _number(report.get("totalMarketLiquidity"))

    if (holders is None or holders == 0) and (liquidity is None or liquidity == 0):
        return False, "rugcheck_returned_no_holder_or_liquidity_data"

    if holders is not None and holders < MIN_HOLDERS_FOR_CLEAN_VERDICT:
        return False, "too_few_holders_to_clear"

    if liquidity is not None and liquidity < MIN_LIQUIDITY_FOR_CLEAN_VERDICT:
        return False, "insufficient_liquidity_to_clear"

    return True, ""


def score_live_rugcheck_report(report: dict[str, Any]) -> dict[str, Any]:
    """Build a conservative baseline score from one live RugCheck report."""
    normalized = _number(report.get("score_normalised"))
    raw_score = _number(report.get("score"))
    risk = _clamp(normalized) if normalized is not None else (
        rugcheck_to_risk(raw_score) if raw_score is not None else 55
    )
    flags: list[str] = []

    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    token_meta = report.get("tokenMeta") if isinstance(report.get("tokenMeta"), dict) else {}
    mint_active = bool(token.get("mintAuthority"))
    freeze_active = bool(token.get("freezeAuthority"))

    # A regulated stablecoin keeps mint and freeze authority by design: Circle
    # must be able to issue and to freeze under court order. Scoring that as a
    # rug vector put USDC and USDT at risk 55 (WARN) on this path -- 1 +10 mint
    # +10 freeze, floored to 50 by the both-active rule, +5 mutable metadata.
    # The authorities are real; treating them as undisclosed risk on a
    # curated canonical mint is what was wrong.
    mint_key = str(report.get("mint") or report.get("address") or "").strip()
    is_known_mint = mint_key in KNOWN_SOLANA_MINTS
    if is_known_mint:
        mint_active = False
        freeze_active = False
        flags.append("known_canonical_solana_mint")

    if mint_active:
        risk += 10
        flags.append("mint_authority_active")
    if freeze_active:
        risk += 10
        flags.append("freeze_authority_active")
    if mint_active and freeze_active:
        risk = max(risk, 50)
    if token_meta.get("mutable") is True and not is_known_mint:
        risk += 5
        flags.append("mutable_metadata")

    for item in report.get("risks") or []:
        if isinstance(item, dict):
            value = item.get("name") or item.get("description")
        else:
            value = item
        normalized_flag = _snake_case(value)
        if normalized_flag:
            flags.append(normalized_flag)

    rugged = report.get("rugged") is True

    if canonical_symbol_mint_mismatch(report):
        # A floor, not a verdict. `apply_verdict_ceilings` decides whether this
        # may go past WARN, and it may only when something independent of the
        # name is also wrong.
        risk = max(risk, IDENTITY_MISMATCH_RISK)
        flags.append("identity_mismatch")
        flags.append("symbol_matches_curated_asset_but_mint_differs")

    # Curated suppression, the non-conclusive cap and the identity cap all live
    # in `apply_verdict_ceilings`, which the stored path calls too. Every flag
    # stays in the response either way: suppression means "these powers are
    # expected for this issuer", never "these powers are absent".
    # Read the holder table before judging the findings drawn from it. A
    # distribution finding the table contradicts, or one whose concentration
    # sits in an account we can name, is reported and not acted on.
    unsupported = unsupported_distribution_flags(report, flags)
    if unsupported:
        flags.append("distribution_findings_not_supported_by_holder_table")

    # Reported on its own terms. Naming a holder no longer excuses the
    # concentration, so this is only ever information -- which is what it
    # should have been.
    named = {
        PROTOCOL_VAULT_NAMES[str(holder.get("owner"))]
        for holder in _top_holders(report)
        if str(holder.get("owner") or "") in KNOWN_PROTOCOL_VAULTS
    }
    for vault in sorted(named):
        flags.append(f"concentration_held_by_{_snake_case(vault)}")

    risk, flags = apply_verdict_ceilings(
        _clamp(risk), flags, rugged=rugged,
        curated=is_curated_canonical_mint(report), unsupported=unsupported,
    )

    if rugged:
        risk = 98
        flags.append("rugcheck_flagged_rugged")

    risk_score = _clamp(risk)
    label = "GOOD" if risk_score < 35 else "WARN" if risk_score < 70 else "DANGER"

    # A floor score from a mint with no economic life is not a clean bill of
    # health. Withhold GOOD rather than inflate the score: this path has no
    # evidence against the token either, so manufacturing DANGER would be the
    # same error pointed the other way.
    if label == "GOOD":
        if not is_known_mint:
            supported, reason = live_report_supports_clean_verdict(report)
            if not supported:
                label = "WARN"
                risk_score = max(risk_score, 50)
                flags.append(reason)
                flags.append("live_scan_cannot_clear_token")

    if rugged:
        label = "DANGER"

    return {
        "risk_score": risk_score,
        "label": label,
        "rugcheck_score": round(raw_score) if raw_score is not None else None,
        "risk_flags": sorted(set(flags)),
        "token_name": token_meta.get("name") or token.get("name"),
        "token_symbol": token_meta.get("symbol") or token.get("symbol"),
    }
