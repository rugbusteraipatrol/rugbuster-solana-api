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

    if mint_active:
        risk += 10
        flags.append("mint_authority_active")
    if freeze_active:
        risk += 10
        flags.append("freeze_authority_active")
    if mint_active and freeze_active:
        risk = max(risk, 50)
    if token_meta.get("mutable") is True:
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
