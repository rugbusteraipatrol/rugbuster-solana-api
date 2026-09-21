"""Who bought in the first seconds: read from chain, floored at WARN, never
past WARN on its own, listed as a gap when unread."""
from __future__ import annotations

import pytest

import app as api
import launch_snipe as ls
from evidence import NOT_COLLECTED, OK, build_evidence
from plain_language import describe
from scoring import apply_launch_snipe, score_live_rugcheck_report

MINT = "D4UFmR618UtmQeRbFjWRH3EmLoXzQXQ5nAetfT8qpump"
CREATOR = "BUnrySvofCGbiStShnofpUEH2pBRdQhc3hfcY1vRWeua"
POOL = "5cNfnqiTPoolAccountXXXXXXXXXXXXXXXXXXXXXXXXX"
SUPPLY = 1_000_000_000.0


def snipe(status="sniped", n=4, bots=3, checked=4):
    return {"status": status, "buyers_within_window": n, "window_seconds": 5,
            "bot_wallets_among_checked": bots, "wallets_checked": checked, "creation_time": 1000,
            "observed_at": "2026-09-22T10:00:00+00:00", "source": "solana_rpc", "study": ls.STUDY}


# --- the floor ---------------------------------------------------------------

def test_a_sniped_launch_floors_at_warn():
    risk, flags = apply_launch_snipe(10, [], snipe())
    assert risk == 60
    assert flags == ["launch_sniped_by_bots"]


@pytest.mark.parametrize("status", ["clean", "unavailable", None])
def test_anything_else_changes_nothing(status):
    assert apply_launch_snipe(10, ["x"], {"status": status} if status else None) == (10, ["x"])


def test_on_its_own_a_snipe_cannot_carry_a_verdict_past_warn():
    report = {"mint": MINT, "creator": CREATOR, "score_normalised": 80,
              "token": {"supply": int(SUPPLY * 10**6), "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
              "tokenMeta": {"name": "x", "symbol": "X", "mutable": False},
              "risks": [{"name": "Top 10 holders high ownership"}], "totalHolders": 40, "totalMarketLiquidity": 2500}
    out = score_live_rugcheck_report(report, snipe=snipe())
    assert out["label"] == "WARN"
    assert "launch_sniped_by_bots" in out["risk_flags"]
    assert "non_conclusive_signals_capped_at_warn" in out["risk_flags"]


def test_with_a_creator_exit_the_verdict_may_be_danger():
    from tests.test_creator_position import position
    report = {"mint": MINT, "creator": CREATOR, "score_normalised": 10,
              "token": {"supply": int(SUPPLY * 10**6), "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
              "tokenMeta": {"name": "x", "symbol": "X", "mutable": False},
              "risks": [], "totalHolders": 300, "totalMarketLiquidity": 9000}
    out = score_live_rugcheck_report(report, position=position("sold", 15.0, sold_after=5), snipe=snipe())
    assert out["label"] == "DANGER"
    assert {"creator_sold_launch_allocation", "launch_sniped_by_bots"} <= set(out["risk_flags"])


# --- the chain read ----------------------------------------------------------

def fake_rpc(txs, wallet_sigs):
    """txs: list of (time, {owner: post_balance}) oldest first; wallet_sigs: wallet -> n signatures."""
    def call(method, params):
        if method == "getSignaturesForAddress":
            if params[0] == MINT:
                return [{"signature": "s%d" % i, "slot": i, "blockTime": t} for i, (t, _) in enumerate(txs)]
            return [{"signature": "w%d" % i} for i in range(wallet_sigs.get(params[0], 0))]
        if method == "getTransaction":
            i = int(params[0][1:]); _, post = txs[i]
            return {"meta": {"preTokenBalances": [], "postTokenBalances": [
                {"owner": o, "mint": MINT, "uiTokenAmount": {"uiAmount": b}} for o, b in post.items()]}}
        raise AssertionError(method)
    return call


def test_three_wallets_in_the_creation_second_is_sniped_and_bots_are_counted():
    txs = [(1000, {POOL: 9.9e8, CREATOR: 5e6, "botA": 2e6, "botB": 1e6, "human": 5e5}),
           (1003, {"botC": 3e6}),
           (1200, {"late": 1e6})]
    got = ls.read(MINT, CREATOR, SUPPLY, fake_rpc(txs, {"botA": 1000, "botB": 1000, "botC": 950, "human": 12}))
    assert got["status"] == "sniped"
    assert got["buyers_within_window"] == 4          # pool, creator and the late buyer excluded
    assert got["bot_wallets_among_checked"] == 3
    assert [b["wallet"] for b in got["first_buyers"]] == ["botA", "botB", "human", "botC"]
    assert got["first_buyers"][3]["seconds_after_creation"] == 3


def test_two_early_buyers_is_clean():
    txs = [(1000, {POOL: 9.9e8, "a": 1e6}), (1002, {"b": 1e6}), (1100, {"c": 1e6})]
    got = ls.read(MINT, CREATOR, SUPPLY, fake_rpc(txs, {"a": 3, "b": 3}))
    assert got["status"] == "clean"
    assert got["buyers_within_window"] == 2


def test_a_token_beyond_the_readable_window_is_unavailable():
    call = fake_rpc([(1000, {})] * 1000, {})
    got = ls.read(MINT, CREATOR, SUPPLY, call)
    assert got["status"] == "unavailable"
    assert "1,000 transactions" in got["reason"]


def test_failures_never_raise():
    def broken(method, params):
        raise TimeoutError("slow")
    assert ls.read(MINT, CREATOR, SUPPLY, broken)["status"] == "unavailable"
    assert ls.read("", CREATOR, SUPPLY, broken)["status"] == "unavailable"


def test_no_rpc_configured_is_unavailable(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    assert "SOLANA_RPC_URL" in ls.from_report({"mint": MINT, "token": {"supply": "1", "decimals": 0}})["reason"]


# --- evidence, sentence, endpoint ---------------------------------------------

def test_evidence_and_sentence():
    payload = {"label": "WARN", "risk_flags": ["launch_sniped_by_bots"], "launch_snipe": snipe()}
    payload["evidence"] = build_evidence(payload)
    assert payload["evidence"]["launch"]["status"] == OK
    told = describe(payload)
    assert told["verdict_basis"] == "FINDING"
    assert "4 wallets bought within 5 seconds" in told["verdict_summary"]
    assert "3 of the 4 checked are high-frequency bot wallets" in told["verdict_summary"]
    gap = {"label": "WARN", "risk_flags": [], "launch_snipe": {"status": "unavailable", "reason": "x"}}
    gap["evidence"] = build_evidence(gap)
    assert gap["evidence"]["launch"]["status"] == NOT_COLLECTED
    assert "who bought in the first seconds after creation" in describe(gap)["not_established"]


def test_the_creator_position_sentence_outranks_the_snipe_sentence():
    from tests.test_creator_position import position
    payload = {"label": "DANGER", "risk_flags": ["creator_sold_launch_allocation", "launch_sniped_by_bots"],
               "creator_position": position("sold", 9.6, sold_after=13), "launch_snipe": snipe()}
    payload["evidence"] = build_evidence(payload)
    assert "bought 9.6% of the supply" in describe(payload)["verdict_summary"]


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


def test_the_live_path_reads_the_launch_and_stores_it(monkeypatch, client):
    from tests.test_creator_position import live_report
    inserted = []
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: live_report())
    monkeypatch.setattr(api.creator_position_module, "from_report", lambda report: {"status": "none", "creator": CREATOR})
    monkeypatch.setattr(api.launch_snipe_module, "from_report", lambda report: snipe())
    monkeypatch.setattr(api, "insert_live_cache", lambda address, result, raw: inserted.append(result.copy()))
    body = client.get(f"/score?address={MINT}").get_json()
    assert body["label"] == "WARN"
    assert body["risk_score"] >= 60
    assert body["launch_snipe"]["status"] == "sniped"
    assert body["evidence"]["launch"]["status"] == OK
    assert "wallets bought within 5 seconds" in body["verdict_summary"]
    assert inserted[0]["launch_snipe"]["buyers_within_window"] == 4


def test_a_cached_live_row_carries_its_stored_launch_read():
    import json
    from datetime import datetime, timezone
    row = {"contract_address": MINT, "risk_score": 60, "label": "WARN", "rugcheck_score": 5,
           "risk_flags": ["launch_sniped_by_bots"], "token_name": "x", "token_symbol": "X",
           "created_at": datetime.now(timezone.utc), "creator_position": None, "launch_snipe": json.dumps(snipe())}
    body = api.live_cache_result(row, MINT)
    assert body["launch_snipe"]["status"] == "sniped"
    assert body["evidence"]["launch"]["status"] == OK
