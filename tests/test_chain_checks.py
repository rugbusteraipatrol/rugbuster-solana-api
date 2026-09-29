import app as api
import chain_checks
import evidence
from plain_language import not_established

MINT = "CqNJeUKi2feUBCbG2rARBQxh9BwUynoK5WrH6d8Bpump"
CREATOR = "DvKQUYssNHuc6Nk3kRrTQkercwBKPas4EKKD6F5hPL9p"
SYSTEM = chain_checks.SYSTEM_PROGRAM


def fake(responses, sequences=None):
    """An RPC stand-in answering by method, and by first param where it matters."""
    sequences = {k: iter(v) for k, v in (sequences or {}).items()}

    def call(method, params):
        if method in sequences:
            return next(sequences[method])
        first = params[0] if isinstance(params, list) and params and isinstance(params[0], str) else None
        value = responses.get((method, first), responses.get((method, None), KeyError(method)))
        if isinstance(value, Exception):
            raise RuntimeError(str(value))
        return value

    return call


def mint_account(extensions=None, mint_auth=None, freeze_auth=None, program=chain_checks.TOKEN_2022_PROGRAM):
    return {"value": {"owner": program, "data": {"parsed": {"type": "mint", "info": {
        "mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": "1000", "decimals": 6,
        "extensions": extensions or []}}}}}


# --- contract -------------------------------------------------------------------


def test_pump_token2022_metadata_in_mint_is_immutable():
    call = fake({
        ("getAccountInfo", MINT): mint_account([{"extension": "tokenMetadata", "state": {"updateAuthority": None}}]),
        ("getAsset", None): {"mutable": True, "authorities": []},
    })
    c = chain_checks.read_contract(call, MINT)
    assert c["status"] == "ok" and c["token_program"] == "token-2022"
    assert c["metadata_mutable"] is False and c["update_authority"] is None
    assert c["raw_fields"] == {"mintAuthority": None, "freezeAuthority": None, "permanentDelegate": None,
                               "nonTransferable": None, "updateAuthority": None}


def test_dangerous_extensions_are_read():
    call = fake({
        ("getAccountInfo", MINT): mint_account([
            {"extension": "permanentDelegate", "state": {"delegate": "Deleg111"}},
            {"extension": "nonTransferable", "state": {}},
            {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500}}},
        ], mint_auth="Auth111"),
        ("getAsset", None): KeyError("no das"),
    })
    c = chain_checks.read_contract(call, MINT)
    assert c["mint_authority"] == "Auth111"
    assert c["extensions"]["permanent_delegate"] == "Deleg111"
    assert c["extensions"]["non_transferable"] is True
    assert c["extensions"]["transfer_fee_bps"] == 500
    assert "updateAuthority" not in c["raw_fields"], "unknown mutability stays unread"


# --- holders --------------------------------------------------------------------


def test_program_accounts_are_counted_apart_from_wallets():
    token_accounts = {"value": [
        {"data": {"parsed": {"info": {"owner": "CurvePDA"}}}},
        {"data": {"parsed": {"info": {"owner": "Alice"}}}},
        {"data": {"parsed": {"info": {"owner": "Bob"}}}},
    ]}
    # owners are looked up sorted: Alice, Bob, CurvePDA
    owner_programs = {"value": [{"owner": SYSTEM}, {"owner": SYSTEM}, {"owner": "PumpProgram"}]}
    call = fake(
        {
            ("getTokenSupply", MINT): {"value": {"amount": "1000"}},
            ("getTokenLargestAccounts", MINT): {"value": [
                {"address": "Curve", "amount": "700"}, {"address": "W1", "amount": "200"}, {"address": "W2", "amount": "100"}]},
        },
        sequences={"getMultipleAccounts": [token_accounts, owner_programs]},
    )
    h = chain_checks.read_holders(call, MINT)
    assert h["program_held_pct"] == 70.0
    assert h["top10_wallet_pct"] == 30.0
    assert h["largest_wallet_pct"] == 20.0
    assert [r["owner"] for r in h["top_wallets"]] == ["Alice", "Bob"]


def test_huge_tokens_report_why_holders_are_unread():
    call = fake({
        ("getTokenSupply", MINT): {"value": {"amount": "1"}},
        ("getTokenLargestAccounts", MINT): KeyError("Too many accounts requested (10000000 pubkeys)"),
    })
    h = chain_checks.read_holders(call, MINT)
    assert h["status"] == "unavailable" and "too many holders" in h["reason"]


# --- funding --------------------------------------------------------------------


def funding_tx(instructions, keys=None, pre=None, post=None):
    return {"blockTime": 1000, "transaction": {"message": {"instructions": instructions, "accountKeys": keys or []}},
            "meta": {"innerInstructions": [], "preBalances": pre or [], "postBalances": post or []}}


def test_plain_transfer_funding():
    call = fake({
        ("getSignaturesForAddress", CREATOR): [{"signature": "new"}, {"signature": "first", "blockTime": 1000}],
        ("getTransaction", "first"): funding_tx([{"program": "system", "parsed": {"type": "transfer", "info": {
            "source": "Funder", "destination": CREATOR, "lamports": 2_000_000_000}}}]),
        ("getBalance", "Funder"): {"value": 5_000_000_000},
    })
    f = chain_checks.read_funding(call, CREATOR, created_at=1600)
    assert (f["status"], f["funder"], f["amount_sol"], f["method"]) == ("ok", "Funder", 2.0, "transfer")
    assert f["seconds_before_creation"] == 600 and f["funder_balance_sol"] == 5.0


def test_durable_nonce_funding_names_the_nonce_authority():
    call = fake({
        ("getSignaturesForAddress", CREATOR): [{"signature": "first"}],
        ("getTransaction", "first"): funding_tx([{"program": "system", "parsed": {"type": "withdrawFromNonce", "info": {
            "destination": CREATOR, "lamports": 30_000_000_000, "nonceAccount": "Nonce", "nonceAuthority": "Boss"}}}]),
        ("getBalance", "Boss"): KeyError("down"),
    })
    f = chain_checks.read_funding(call, CREATOR)
    assert (f["funder"], f["amount_sol"], f["method"]) == ("Boss", 30.0, "durable_nonce_withdrawal")
    assert f["funder_balance_sol"] is None


def test_balance_change_fallback():
    call = fake({
        ("getSignaturesForAddress", CREATOR): [{"signature": "first"}],
        ("getTransaction", "first"): funding_tx([], keys=["Payer", CREATOR, "Other"], pre=[10, 0, 5], post=[3, 7, 5]),
        ("getBalance", "Payer"): {"value": 0},
    })
    f = chain_checks.read_funding(call, CREATOR)
    assert (f["funder"], f["method"]) == ("Payer", "balance_change")


def test_busy_creator_wallet_is_said_plainly():
    call = fake({("getSignaturesForAddress", CREATOR): [{"signature": str(i)} for i in range(1000)]})
    assert chain_checks.read_funding(call, CREATOR)["status"] == "busy_wallet"


def test_one_part_failing_does_not_fail_the_others():
    call = fake({
        ("getAccountInfo", MINT): KeyError("rpc down"),
        ("getTokenSupply", MINT): {"value": {"amount": "1000"}},
        ("getTokenLargestAccounts", MINT): {"value": []},
        ("getSignaturesForAddress", CREATOR): [],
    })
    chain_checks._cache.clear()
    r = chain_checks.run(MINT, CREATOR, call=call)
    assert r["contract"]["status"] == "unavailable"
    assert r["holders"]["status"] == "unavailable" and r["funding"]["status"] == "unavailable"
    assert r["effect_on_verdict"].startswith("none")


# --- evidence -------------------------------------------------------------------


def test_chain_reading_closes_the_not_checked_items():
    base = {"ok": True, "label": "DANGER", "risk_flags": []}
    before = not_established(evidence.build_evidence(base, None))
    with_chain = dict(base, chain_checks={"contract": {"raw_fields": {
        "mintAuthority": None, "freezeAuthority": None, "updateAuthority": None,
        "permanentDelegate": None, "nonTransferable": None}}})
    after = not_established(evidence.build_evidence(with_chain, None))
    assert len(after) < len(before)
    assert not any("mint authority" in g or "freeze authority" in g for g in after)


def test_chain_reading_never_overrides_a_report():
    payload = {"ok": True, "label": "WARN", "risk_flags": [],
               "chain_checks": {"contract": {"raw_fields": {"mintAuthority": None}}}}
    report = {"token": {"mintAuthority": "SomeAuth"}}
    assert evidence.authority_readings(payload, report)["mint"] == evidence.PRESENT


def test_no_rpc_means_no_chain_checks(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    assert chain_checks.run(MINT, CREATOR) is None
    payload = {"label": "GOOD", "address": MINT}
    api.attach_chain_checks(payload)
    assert "chain_checks" not in payload
