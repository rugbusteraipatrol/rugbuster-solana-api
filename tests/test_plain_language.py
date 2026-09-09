"""A warning must say which kind of warning it is.

`WARN 69` and a list of machine flags does not tell a reader whether we found
something against the token or could not check it. Those are opposite answers.
Most of what keeps a token out of GOOD here is the second kind, and printing it
as an unexplained warning leaves the reader to invent a reason -- which will be
worse than the truth.

The one thing that must never happen is the reverse: a refusal to clear
softened into "nothing to worry about". A token whose holders are gone is
exactly where a reassuring sentence would do the most damage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plain_language import FINDING, GAP, REFUSAL, describe, not_established

REASSURANCE = "not evidence against the token"


def _payload(label: str, flags: list[str], **dimensions) -> dict:
    evidence = {
        "technical_controls": {"active_authorities": [], "unread_authorities": [],
                               "revoked_authorities": []},
        "issuer_identity": {"recognised": False},
        "distribution": {"concentration_signals": [], "owners_identified": False},
        "creator_history": {"status": "NOT_COLLECTED",
                            "confirmed_incidents": {"status": "NOT_COLLECTED"}},
    }
    for name, value in dimensions.items():
        evidence[name] = {**evidence.get(name, {}), **value}
    return {"label": label, "risk_flags": flags, "evidence": evidence}


# --- the sentence that must not appear where it does not belong ------------

def test_a_refusal_to_clear_is_never_softened():
    """This token has almost nothing left. Saying the gap is on our side would
    be the most comforting sentence available and the least supported."""
    result = describe(_payload("WARN", ["live_scan_cannot_clear_token",
                                        "too_few_holders_to_clear"]))
    assert result["verdict_basis"] == REFUSAL
    assert REASSURANCE not in result["verdict_summary"]


def test_a_rugged_token_is_never_softened():
    result = describe(_payload("DANGER", ["rugcheck_flagged_rugged"]))
    assert result["verdict_basis"] == FINDING
    assert REASSURANCE not in result["verdict_summary"]


def test_a_disclosure_only_warning_says_the_gap_is_ours():
    """The case this exists for: nothing was found, we just could not check
    who holds the authority."""
    result = describe(_payload(
        "WARN", ["mint_authority_active", "non_conclusive_signals_capped_at_warn"],
        technical_controls={"active_authorities": ["mint"], "unread_authorities": []},
    ))
    assert result["verdict_basis"] == GAP
    assert REASSURANCE in result["verdict_summary"]
    assert "who holds the mint authority" in result["not_established"]


# --- the list of what we do not know ---------------------------------------

def test_an_unread_authority_is_listed_and_not_dropped():
    """The mistake this scanner has made most often is letting an unchecked
    thing vanish from the answer."""
    gaps = not_established({
        "technical_controls": {"active_authorities": [], "unread_authorities": ["freeze", "update"]},
        "issuer_identity": {"recognised": True},
    })
    assert "whether the freeze authority is still held" in gaps
    assert "whether the metadata update authority is still held" in gaps


def test_a_recognised_issuer_stops_the_question_of_who_holds_the_authority():
    """Identity answers 'who holds it'. It does not remove the authority, which
    technical_controls still reports."""
    gaps = not_established({
        "technical_controls": {"active_authorities": ["mint"], "unread_authorities": []},
        "issuer_identity": {"recognised": True},
    })
    assert not any("who holds the mint authority" == gap for gap in gaps)
    assert "who issued this token" not in gaps


def test_concentration_we_have_named_is_not_listed_as_unknown():
    gaps = not_established({
        "distribution": {"concentration_signals": ["single_holder_ownership"],
                         "owners_identified": True},
        "issuer_identity": {"recognised": True},
    })
    assert "who the largest holders are" not in gaps


def test_concentration_we_have_not_named_is_listed():
    gaps = not_established({
        "distribution": {"concentration_signals": ["single_holder_ownership"],
                         "owners_identified": False},
        "issuer_identity": {"recognised": True},
    })
    assert "who the largest holders are" in gaps


def test_uncollected_deployer_history_is_stated_not_omitted():
    gaps = not_established({"creator_history": {"status": "NOT_COLLECTED",
                                                "confirmed_incidents": {"status": "NOT_COLLECTED"}}})
    assert "what this deployer's previous tokens did" in gaps


def test_the_list_has_no_repeats():
    gaps = not_established({
        "technical_controls": {"active_authorities": ["mint", "mint"], "unread_authorities": []},
        "issuer_identity": {"recognised": False},
    })
    assert len(gaps) == len(set(gaps))


# --- it may describe, never decide -----------------------------------------

def test_describing_a_verdict_cannot_change_it():
    payload = _payload("WARN", ["mint_authority_active"])
    before = dict(payload)
    result = describe(payload)
    assert payload == before
    assert set(result) == {"verdict_summary", "verdict_basis", "not_established"}


def test_no_evidence_does_not_come_out_as_nothing_unknown():
    """An empty list here would read as "everything was established". With no
    evidence at all, the opposite is true, and the answer has to say so."""
    result = describe({"label": "UNKNOWN", "risk_flags": []})
    assert result["verdict_summary"]
    assert "who issued this token" in result["not_established"]
