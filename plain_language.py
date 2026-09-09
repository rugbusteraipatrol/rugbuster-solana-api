"""Say, in a sentence, what was found and what could not be established.

The evidence block already carries all of this. It carries it as six nested
dimensions with statuses, and the top of the response says `WARN 69` and a list
of machine flags -- so a reader sees a warning and has no way to tell whether
we found something against the token or simply could not check.

Those are completely different answers and the response has to say which. Most
of what keeps a token out of GOOD here is the second kind: an authority whose
holder nobody has identified, a deployer history this service does not collect.
That is a limit of what we looked at. Printing it as an unexplained warning
invites the reader to supply a reason we never gave them, and the reason they
supply will be worse than the truth.

Nothing here judges. Every sentence is a restatement of a field computed
elsewhere, so this module cannot move a verdict.
"""

from __future__ import annotations

from typing import Any

AUTHORITY_WORDS = {
    "mint": "mint authority",
    "freeze": "freeze authority",
    "update": "metadata update authority",
    "balance_mutable": "balance-mutation authority",
    "non_transferable": "transfer-restriction setting",
}


def _dimension(evidence: dict[str, Any], name: str) -> dict[str, Any]:
    value = (evidence or {}).get(name)
    return value if isinstance(value, dict) else {}


def not_established(evidence: dict[str, Any]) -> list[str]:
    """Plainly: the questions this answer does not settle.

    Absence is listed, never implied. A dimension we did not read must appear
    here rather than being silently dropped -- which is the single mistake this
    scanner has made most often.
    """
    gaps: list[str] = []

    controls = _dimension(evidence, "technical_controls")
    identity = _dimension(evidence, "issuer_identity")
    issuer_known = identity.get("recognised") is True

    for authority in controls.get("active_authorities") or []:
        word = AUTHORITY_WORDS.get(authority, f"{authority} authority")
        if not issuer_known:
            gaps.append(f"who holds the {word}")
    for authority in controls.get("unread_authorities") or []:
        word = AUTHORITY_WORDS.get(authority, f"{authority} authority")
        gaps.append(f"whether the {word} is still held")

    if not issuer_known:
        gaps.append("who issued this token")

    distribution = _dimension(evidence, "distribution")
    if distribution.get("concentration_signals"):
        if not distribution.get("owners_identified"):
            gaps.append("who the largest holders are")
        else:
            # Naming the holder answers who holds it, not who can move it. The
            # vault is program-owned, and that program is upgradeable by a key
            # we have not identified, so the question moves rather than closing.
            gaps.append(
                "who controls the program holding the identified concentration"
            )

    creator = _dimension(evidence, "creator_history")
    if str(creator.get("status") or "").upper() in {"NOT_COLLECTED", "NOT_QUERIED", "FETCH_FAILED", "UNKNOWN"}:
        gaps.append("what this deployer's previous tokens did")

    incidents = creator.get("confirmed_incidents")
    if isinstance(incidents, dict) and str(incidents.get("status") or "").upper() != "OK":
        gaps.append("whether this token or its deployer has a confirmed incident on record")

    seen: set[str] = set()
    return [gap for gap in gaps if not (gap in seen or seen.add(gap))]


# Three kinds of headline, and they must not be blurred:
#   FINDING  -- we found something about this token
#   REFUSAL  -- we decline to clear it, and that is our answer, not a gap
#   GAP      -- we could not check, which says nothing about the token
FINDING, REFUSAL, GAP = "FINDING", "REFUSAL", "GAP"


def _finding_sentence(label: str, flags: list[str]) -> tuple[str, str]:
    """The headline, and which of the three kinds it is."""
    if "rugcheck_flagged_rugged" in flags:
        return "The upstream report marks this token as already rugged.", FINDING
    if "live_scan_cannot_clear_token" in flags:
        return (
            "Nothing here supports calling this token safe: it has almost no "
            "holders or liquidity left, so a low upstream score says only that "
            "there is nothing left to measure.",
            REFUSAL,
        )
    if "identity_mismatch" in flags:
        return (
            "This address carries a well-known ticker but is not the asset that "
            "ticker names. That is an address mismatch, not proof of intent.",
            FINDING,
        )
    if "curated_mint_administrative_flags_only" in flags:
        return (
            "A hand-verified asset whose retained powers are expected for its "
            "issuer. The powers are real and are still listed below.",
            FINDING,
        )
    if "non_conclusive_signals_capped_at_warn" in flags:
        return (
            "Everything found here is a disclosure about who controls the token "
            "or who holds it. Nothing found shows the token being drained.",
            GAP,
        )
    if label == "DANGER":
        return "Findings against this token, listed below.", FINDING
    if label == "GOOD":
        return "Nothing found against this token in what was checked.", FINDING
    if label == "UNKNOWN":
        return "No verdict is being given for this token.", GAP
    return "Nothing conclusive was found either way.", GAP


def describe(payload: dict[str, Any]) -> dict[str, Any]:
    """A sentence and a list, both restating fields already computed."""
    label = str(payload.get("label") or "").upper()
    flags = [str(flag) for flag in (payload.get("risk_flags") or [])]
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}

    summary, kind = _finding_sentence(label, flags)
    gaps = not_established(evidence)

    # Only a GAP earns the reassuring clause. A refusal to clear must never be
    # softened with it: on a token whose holders are gone, "not evidence
    # against the token" is the sentence a reader would most like to hear and
    # the one least supported by what we know.
    if kind == GAP and gaps and label not in {"GOOD", "DANGER"}:
        summary += (
            " What is missing is knowledge on our side, not evidence against "
            "the token: see not_established."
        )

    return {"verdict_summary": summary, "verdict_basis": kind, "not_established": gaps}
