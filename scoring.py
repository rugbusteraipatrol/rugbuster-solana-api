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


def derive_score(record: dict[str, Any], row_label: str | None) -> tuple[int, float | None, list[str]]:
    """
    Derive risk transparently.

    Priority:
    1. Use precomputed risk_percent unchanged.
    2. Otherwise calibrate RugCheck, then apply documented signal boosts.
    3. If no numeric signal exists, map cached label. Unknown/malformed rows use
       WARN=55 so incomplete evidence never becomes a false GOOD.
    """
    flags = _existing_flags(record)
    precomputed = _first_number(record, "risk_percent")
    rugcheck_score = _rugcheck_score(record)
    rugcheck_usable = _rugcheck_reliable(record, rugcheck_score)

    if precomputed is not None:
        risk = _clamp(precomputed)
    else:
        label = str(row_label or record.get("label") or "").upper()
        if rugcheck_score is not None and rugcheck_usable:
            risk = rugcheck_to_risk(rugcheck_score)
        elif rugcheck_score is not None:
            # RugCheck returned a score, but this token has no real trading
            # history yet (see _rugcheck_reliable) -- that score is not a
            # confirmed low-risk assessment, so don't let it set a low floor.
            # Fall back to the same neutral WARN default used for missing
            # data, not the row's own (possibly already-wrong) stored label.
            risk = 55
            flags.append("rugcheck_score_unreliable_fresh_token")
        else:
            risk = LABEL_FALLBACKS.get(label, 55)

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


def score_scan_row(row: dict[str, Any]) -> dict[str, Any]:
    record = _load_record(row.get("full_record"))
    risk_score, rugcheck_score, flags = derive_score(record, row.get("label"))
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
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH (Wormhole)",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": "WBTC (Wormhole)",
}

# Below these, the token has essentially no economic life: the 17 on-chain
# confirmed pump.fun rugs all sat at 2-5 holders and under $4k liquidity,
# while a fresh-but-real token in the same sample had 924 holders and $8.8k.
MIN_HOLDERS_FOR_CLEAN_VERDICT = 50
MIN_LIQUIDITY_FOR_CLEAN_VERDICT = 5_000

# RugCheck's floor score. It means "our static checks found nothing", which on
# an unproven token is an absence of evidence, not evidence of safety.
RUGCHECK_FLOOR_SCORE = 10


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
