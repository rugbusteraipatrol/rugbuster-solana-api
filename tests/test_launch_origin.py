"""The creator from the creation transaction, when the report has none.

The Altruists, 2026-10-08: a Raydium LaunchLab token whose report carried no
creator. Every creator check stopped at "creator or mint not in report" while
the creator had bundled 27% across four wallets and sold it in 13 seconds.
"""

import launch_origin

MINT = "DZQbBPFpTeeyGyvCwXxYQDZFGGHad2bY6sWZuviuX8MN"
CREATOR = "JDQKDrc1TQgBRvdFh56tkta5sYcDj1SoP52Eiu64rSrT"
SIG = "5tq1zYPUahCZaViLPB6WcreweyvenYdqV7gSr3WAKK4CCXhv7CBNTZPydYaxpokuUahnro8taVpKbQrGtm4Vfqhd"


LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def chain(logs=("Program log: Instruction: InitializeMint2",), err=None, signer=CREATOR, programs=(LAUNCHLAB, TOKEN)):
    seen = []

    def call(method, params):
        seen.append(method)
        if method == "getTransactionsForAddress":
            assert params[1]["sortOrder"] == "asc"
            return {"data": [{"signature": SIG, "err": err, "blockTime": 1791468217}]}
        if method == "getTransaction":
            return {"blockTime": 1791468217, "meta": {"logMessages": list(logs), "innerInstructions": []},
                    "transaction": {"message": {"instructions": [{"programId": p} for p in programs], "accountKeys": [
                        {"pubkey": signer, "signer": True}, {"pubkey": MINT, "signer": True}]}}}
        raise AssertionError(method)

    call.seen = seen
    return call


def test_creator_is_the_signer_of_the_creation_transaction():
    report = launch_origin.fill_creator({"mint": MINT}, call=chain())
    assert report["creator"] == CREATOR
    assert report["creator_source"]["creation_signature"] == SIG
    assert report["creator_source"]["source"] == "solana_rpc"
    assert report["creator_source"]["launchpad"] == "Raydium LaunchLab"


def test_a_token_minted_whole_by_its_deployer_gets_no_launchpad_creator():
    """JUP, 2026-10-08 gate: its deployer minted the supply and handed it out;
    read as a launchpad creator that "sold", JUP scored DANGER 75."""
    report = launch_origin.fill_creator({"mint": MINT}, call=chain(programs=(TOKEN, "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")))
    assert "creator" not in report


def test_a_report_with_a_creator_is_left_alone():
    call = chain()
    report = launch_origin.fill_creator({"mint": MINT, "creator": "Someone"}, call=call)
    assert report["creator"] == "Someone" and call.seen == []


def test_a_first_transaction_that_does_not_create_the_mint_is_not_trusted():
    report = launch_origin.fill_creator({"mint": MINT}, call=chain(logs=("Program log: Instruction: Transfer",)))
    assert "creator" not in report


def test_a_failed_lookup_leaves_the_gap():
    def broken(method, params):
        raise RuntimeError("method not found")

    report = launch_origin.fill_creator({"mint": MINT}, call=broken)
    assert "creator" not in report
