"""The creator's own position: read from chain, floored into the verdict,
listed as a gap when unread.

The cases mirror the study this rests on (see creator_position.py): a
creator who bought at creation and still holds, one who already sold, one
whose share is too small to matter, and one whose wallet never held the token.
"""
from __future__ import annotations

import json

import pytest

import app as api
import creator_position as cp
from evidence import NOT_COLLECTED, OK, build_evidence
from plain_language import describe
from scoring import apply_creator_position, label_for_risk, score_live_rugcheck_report

MINT = "D4UFmR618UtmQeRbFjWRH3EmLoXzQXQ5nAetfT8qpump"
CREATOR = "BUnrySvofCGbiStShnofpUEH2pBRdQhc3hfcY1vRWeua"
SUPPLY = 1_000_000_000.0


def position(status="holding", share=12.0, sold_pct=None, sold_after=None):
    peak = share
    current = 0.0 if status == "sold" else share
    return {
        "status": status,
        "creator": CREATOR,
        "share_at_creation_pct": share,
        "peak_share_pct": peak,
        "current_share_pct": current,
        "sold_pct_of_peak": sold_pct if sold_pct is not None else (100.0 if status == "sold" else 0.0),
        "sold_after_seconds": sold_after,
        "observed_at": "2026-09-18T10:00:00+00:00",
        "source": "solana_rpc",
        "base_rate": cp.BASE_RATE,
    }


# --- the floors -------------------------------------------------------------

def test_a_material_holding_is_a_danger_floor():
    risk, flags = apply_creator_position(20, [], position("holding", 12.0))
    assert risk == 75 and label_for_risk(risk) == "DANGER"
    assert {"creator_bought_at_creation", "creator_holds_launch_allocation"} <= set(flags)


def test_a_material_exit_is_the_same_floor_with_its_own_flag():
    risk, flags = apply_creator_position(20, [], position("sold", 9.6, sold_after=13))
    assert risk == 75
    assert "creator_sold_launch_allocation" in flags
    assert "creator_holds_launch_allocation" not in flags


def test_a_minor_share_is_a_warn_floor():
    risk, flags = apply_creator_position(20, [], position("holding", 2.5))
    assert risk == 60 and label_for_risk(risk) == "WARN"


def test_a_negligible_share_is_reported_and_not_scored():
    risk, flags = apply_creator_position(20, [], position("sold", 0.4))
    assert risk == 20
    assert flags == ["creator_launch_allocation_negligible"]


@pytest.mark.parametrize("status", ["none", "unavailable"])
def test_no_position_changes_nothing(status):
    risk, flags = apply_creator_position(20, ["x"], {"status": status, "creator": CREATOR})
    assert (risk, flags) == (20, ["x"])
    assert apply_creator_position(20, ["x"], None) == (20, ["x"])


def test_floors_never_lower_a_verdict():
    risk, _ = apply_creator_position(90, [], position("holding", 50.0))
    assert risk == 90


def test_the_position_carries_a_live_verdict_past_the_non_conclusive_cap():
    # Only disclosures in the report: on its own this stops at WARN.
    report = {
        "mint": MINT, "creator": CREATOR, "score_normalised": 10,
        "token": {"supply": int(SUPPLY * 10**6), "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "x", "symbol": "X", "mutable": False},
        "risks": [{"name": "Top 10 holders high ownership"}],
        "totalHolders": 40, "totalMarketLiquidity": 2500,
    }
    without = score_live_rugcheck_report(report)
    with_position = score_live_rugcheck_report(report, position=position("sold", 15.2, sold_after=5))
    assert without["label"] != "DANGER"
    assert with_position["label"] == "DANGER"
    assert with_position["risk_score"] >= 75
    assert "creator_sold_launch_allocation" in with_position["risk_flags"]


# --- the chain read ----------------------------------------------------------

def fake_rpc(balances_by_sig: dict[str, tuple[int, float | None]], program="token2022"):
    """A stand-in RPC: signatures exist only on one program's ATA, and each
    transaction reports the creator's post-balance (None = account closed)."""
    ata = cp.associated_token_account(CREATOR, MINT, dict(cp.TOKEN_PROGRAMS)[program])
    calls: list[tuple[str, list]] = []

    def call(method, params):
        calls.append((method, params))
        if method == "getSignaturesForAddress":
            if params[0] != ata:
                return []
            return [{"signature": sig, "slot": i, "blockTime": t}
                    for i, (sig, (t, _)) in enumerate(balances_by_sig.items())]
        if method == "getTransaction":
            t, balance = balances_by_sig[params[0]]
            if balance is None:
                return {"meta": {"preTokenBalances": [{"owner": CREATOR, "mint": MINT}], "postTokenBalances": []}}
            return {"meta": {"postTokenBalances": [
                {"owner": CREATOR, "mint": MINT, "uiTokenAmount": {"uiAmount": balance}}]}}
        raise AssertionError(method)

    call.calls = calls
    return call


def test_an_instant_exit_is_read_as_sold_with_its_timing():
    call = fake_rpc({"create": (1000, 120_000_000.0), "sell": (1046, 0.0), "close": (1050, None)})
    got = cp.lookup(CREATOR, MINT, SUPPLY, call)
    assert got["status"] == "sold"
    assert got["share_at_creation_pct"] == 12.0
    assert got["current_share_pct"] == 0.0
    assert got["sold_pct_of_peak"] == 100.0
    assert got["sold_after_seconds"] == 46  # the sale, not the account close at 1050
    assert got["token_program"] == "token2022"
    assert got["base_rate"]["n"] == 2785
    # both programs' accounts are asked before giving up, only one answered
    assert [m for m, _ in call.calls].count("getSignaturesForAddress") == 2


def test_a_creator_still_holding_is_read_as_holding():
    call = fake_rpc({"create": (1000, 50_000_000.0), "buy": (1200, 55_000_000.0)}, program="spl")
    got = cp.lookup(CREATOR, MINT, SUPPLY, call)
    assert got["status"] == "holding"
    assert got["peak_share_pct"] == 5.5
    assert got["sold_after_seconds"] is None


def test_a_wallet_that_never_held_is_none_not_a_gap():
    got = cp.lookup(CREATOR, MINT, SUPPLY, fake_rpc({}))
    assert got["status"] == "none"
    assert got["share_at_creation_pct"] == 0.0


def test_a_failed_lookup_is_unavailable_and_never_raises():
    def broken(method, params):
        raise TimeoutError("slow")
    got = cp.lookup(CREATOR, MINT, SUPPLY, broken)
    assert got["status"] == "unavailable"
    assert "rpc lookup failed" in got["reason"]
    assert cp.lookup(None, MINT, SUPPLY, broken)["status"] == "unavailable"
    assert cp.lookup(CREATOR, MINT, None, broken)["status"] == "unavailable"


def test_no_rpc_configured_is_unavailable(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    got = cp.from_report({"mint": MINT, "creator": CREATOR, "token": {"supply": "1000", "decimals": 0}})
    assert got["status"] == "unavailable"
    assert "SOLANA_RPC_URL" in got["reason"]


def test_from_report_reads_supply_and_decimals():
    seen = {}

    def call(method, params):
        seen[method] = params
        return []
    got = cp.from_report({"mint": MINT, "creator": CREATOR, "token": {"supply": "2000000000000000", "decimals": 6}}, call)
    assert got["status"] == "none"
    assert "lookup_seconds" in got


# --- evidence and the sentence ----------------------------------------------

def test_the_evidence_dimension_reports_the_position_or_the_gap():
    ok = build_evidence({"label": "DANGER", "risk_flags": [], "creator_position": position("sold", 9.6, sold_after=13)})
    assert ok["creator_position"]["status"] == OK
    assert ok["creator_position"]["position"] == "sold"
    assert ok["creator_position"]["base_rate"]["exit_within_1h"] == 0.889
    gap = build_evidence({"label": "WARN", "risk_flags": [], "creator_position": {"status": "unavailable", "reason": "x"}})
    assert gap["creator_position"]["status"] == NOT_COLLECTED
    assert "what the creator did with their own allocation" in describe(
        {"label": "WARN", "risk_flags": [], "evidence": gap})["not_established"]


def test_the_headline_says_what_the_creator_did():
    payload = {"label": "DANGER", "risk_flags": ["creator_bought_at_creation", "creator_sold_launch_allocation"],
               "creator_position": position("sold", 9.6, sold_after=13)}
    payload["evidence"] = build_evidence(payload)
    told = describe(payload)
    assert told["verdict_basis"] == "FINDING"
    assert "bought 9.6% of the supply" in told["verdict_summary"]
    assert "sold 100% of it 13 seconds later" in told["verdict_summary"]
    assert "89 of every 100 creators" in told["verdict_summary"]
    holding = dict(payload, risk_flags=["creator_bought_at_creation", "creator_holds_launch_allocation"],
                   creator_position=position("holding", 12.0))
    assert "still holds it" in describe(holding)["verdict_summary"]


# --- the endpoint -------------------------------------------------------------

def live_report():
    return {
        "mint": MINT, "creator": CREATOR, "score_normalised": 5,
        "token": {"supply": int(SUPPLY * 10**6), "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "attention", "symbol": "att", "mutable": False},
        "risks": [], "totalHolders": 300, "totalMarketLiquidity": 9000,
        "topHolders": [{"owner": "a", "pct": 3.0}],
    }


@pytest.fixture()
def client():
    api.app.config["TESTING"] = True
    return api.app.test_client()


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(api, "ensure_live_cache_schema", lambda: None)
    monkeypatch.setattr(api, "fetch_live_cache", lambda _address: None)
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "RUGCHECK_MIN_GAP_SECONDS", 0)


def test_the_live_path_reads_the_position_and_stores_it(monkeypatch, client):
    inserted = []
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: live_report())
    monkeypatch.setattr(api.creator_position_module, "from_report", lambda report: position("sold", 35.5, sold_after=22))
    monkeypatch.setattr(api, "insert_live_cache", lambda address, result, raw: inserted.append(result.copy()))

    body = client.get(f"/score?address={MINT}").get_json()
    assert body["label"] == "DANGER"
    assert body["risk_score"] >= 75
    assert body["creator_position"]["status"] == "sold"
    assert body["evidence"]["creator_position"]["status"] == OK
    assert body["verdict_basis"] == "FINDING"
    assert "bought 35.5% of the supply" in body["verdict_summary"]
    assert inserted[0]["creator_position"]["share_at_creation_pct"] == 35.5


def test_an_unread_position_leaves_the_verdict_alone_and_lists_the_gap(monkeypatch, client):
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: live_report())
    monkeypatch.setattr(api.creator_position_module, "from_report",
                        lambda report: {"status": "unavailable", "creator": CREATOR, "reason": "rpc down"})
    monkeypatch.setattr(api, "insert_live_cache", lambda *a: None)
    body = client.get(f"/score?address={MINT}").get_json()
    assert "creator_sold_launch_allocation" not in body["risk_flags"]
    assert body["evidence"]["creator_position"]["status"] == NOT_COLLECTED
    assert "what the creator did with their own allocation" in body["not_established"]


def test_a_cached_live_row_carries_its_stored_position():
    from datetime import datetime, timezone
    row = {"contract_address": MINT, "risk_score": 75, "label": "DANGER", "rugcheck_score": 5,
           "risk_flags": ["creator_sold_launch_allocation"], "token_name": "x", "token_symbol": "X",
           "created_at": datetime.now(timezone.utc), "creator_position": json.dumps(position("sold", 9.6, sold_after=13))}
    body = api.live_cache_result(row, MINT)
    assert body["creator_position"]["status"] == "sold"
    assert body["evidence"]["creator_position"]["status"] == OK
    assert "bought 9.6%" in body["verdict_summary"]


def test_the_exit_time_is_the_sale_not_the_last_account_activity():
    # Sold 3 s after creation; a dust transfer and the close come 13 days later.
    call = fake_rpc({"create": (1000, 112_000_000.0), "sell": (1003, 0.0),
                     "dust": (1000 + 13 * 86400, 0.0), "close": (1000 + 13 * 86400 + 5, None)})
    got = cp.lookup(CREATOR, MINT, SUPPLY, call)
    assert got["status"] == "sold"
    assert got["sold_after_seconds"] == 3
    assert got["sold_after_is_upper_bound"] is False


def test_an_exit_hidden_between_unread_transactions_is_an_upper_bound():
    # Ten transactions; only the first two and last two are read. The creator
    # still held after the second, and had sold by the ninth.
    hist = {"t%d" % i: (1000 + i * 3600, 50_000_000.0 if i < 5 else 0.0) for i in range(10)}
    got = cp.lookup(CREATOR, MINT, SUPPLY, fake_rpc(hist))
    assert got["status"] == "sold"
    assert got["sold_after_seconds"] == 8 * 3600
    assert got["sold_after_is_upper_bound"] is True
    payload = {"label": "DANGER", "risk_flags": ["creator_bought_at_creation", "creator_sold_launch_allocation"],
               "creator_position": got}
    payload["evidence"] = build_evidence(payload)
    assert "sold 100% of it within 8 hours." in describe(payload)["verdict_summary"]
