import base64
import json

import pytest
from flask import Flask, jsonify
from x402.schemas import SupportedResponse
from x402.schemas.responses import SupportedKind
from x402.server import x402ResourceServerSync
from x402.mechanisms.svm.exact import ExactSvmServerScheme

import app as api
import x402_solana


PAY_TO = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
FEE_PAYER = "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT = "CqNJeUKi2feUBCbG2rARBQxh9BwUynoK5WrH6d8Bpump"


def settings(**overrides):
    values = dict(
        enabled=True,
        network=x402_solana.SOLANA_MAINNET,
        price="$0.01",
        pay_to=PAY_TO,
        resource_url="https://rugbuster-solana-api-production.up.railway.app",
        cdp_api_key_id="key-id",
        cdp_api_key_secret="key-secret",
    )
    values.update(overrides)
    return x402_solana.X402Settings(**values)


class FakeFacilitator:
    """Stands in for the CDP facilitator; never called for verify/settle here."""

    def get_supported(self):
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2,
                    scheme="exact",
                    network=x402_solana.SOLANA_MAINNET,
                    extra={"feePayer": FEE_PAYER},
                )
            ]
        )

    def verify(self, *args, **kwargs):  # pragma: no cover - no payment is sent
        raise AssertionError("verify must not run without a payment header")

    def settle(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("settle must not run without a payment header")


def fake_server():
    server = x402ResourceServerSync(FakeFacilitator())
    server.register(x402_solana.SOLANA_MAINNET, ExactSvmServerScheme())
    return server


# --- settings --------------------------------------------------------------


def test_disabled_settings_need_nothing():
    x402_solana.X402Settings(enabled=False).validate()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"pay_to": "0x3208F5DfE9B2010bBe711033f39fF7Ee83039AAf"}, "Solana address"),
        ({"network": "eip155:8453"}, "Solana mainnet or devnet"),
        ({"price": "0.01"}, "$0.01"),
        ({"resource_url": "http://example.com"}, "HTTPS"),
        ({"cdp_api_key_secret": ""}, "CDP_API_KEY"),
    ],
)
def test_enabled_settings_are_validated(overrides, message):
    with pytest.raises(ValueError, match=message.replace("$", r"\$")):
        settings(**overrides).validate()


def test_from_env_defaults_to_solana_mainnet(monkeypatch):
    monkeypatch.delenv("RUGBUSTER_X402_NETWORK", raising=False)
    monkeypatch.setenv("RUGBUSTER_X402_ENABLED", "true")
    loaded = x402_solana.X402Settings.from_env()
    assert loaded.enabled is True
    assert loaded.network == x402_solana.SOLANA_MAINNET


def test_public_status_never_mentions_keys():
    status = settings().public_status()
    assert status["route"] == "GET /x402/score"
    assert status["asset"] == "USDC"
    assert "key" not in json.dumps(status).lower()


# --- what gets charged -----------------------------------------------------


@pytest.mark.parametrize("label", ["DANGER", "WARN", "GOOD"])
def test_a_verdict_is_chargeable(label):
    assert x402_solana.is_chargeable({"ok": True, "label": label})


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "label": "UNKNOWN"},
        {"ok": False, "label": "DANGER"},
        {"ok": True},
    ],
)
def test_no_verdict_is_not_chargeable(payload):
    assert not x402_solana.is_chargeable(payload)


# --- the 402 challenge -----------------------------------------------------


def test_unpaid_request_gets_a_solana_challenge():
    flask_app = Flask(__name__)

    @flask_app.get("/x402/score")
    def paid():  # pragma: no cover - the middleware answers first
        return jsonify({"ok": True, "label": "DANGER"})

    @flask_app.get("/score")
    def free():
        return jsonify({"ok": True, "label": "DANGER"})

    x402_solana.install(flask_app, settings(), server=fake_server())
    client = flask_app.test_client()

    response = client.get(f"/x402/score?address={MINT}")
    assert response.status_code == 402
    encoded = response.headers.get("PAYMENT-REQUIRED")
    assert encoded, "the challenge travels in the PAYMENT-REQUIRED header"
    challenge = json.loads(base64.b64decode(encoded))
    option = challenge["accepts"][0]
    assert option["scheme"] == "exact"
    assert option["network"] == x402_solana.SOLANA_MAINNET
    assert option["payTo"] == PAY_TO
    assert option["asset"] == USDC_MINT
    assert option["amount"] == "10000"  # $0.01 in USDC's six decimals
    assert option["extra"]["feePayer"] == FEE_PAYER

    free_response = client.get("/score")
    assert free_response.status_code == 200, "the free route is not gated"


def test_disabled_install_changes_nothing():
    flask_app = Flask(__name__)
    original = flask_app.wsgi_app
    assert x402_solana.install(flask_app, settings(enabled=False)) is None
    assert flask_app.wsgi_app == original


# --- the paid view in the real app ------------------------------------------


@pytest.fixture()
def client():
    api.app.config.update(TESTING=True)
    return api.app.test_client()


def test_paid_route_is_absent_when_x402_is_off(client, monkeypatch):
    monkeypatch.setattr(api, "X402_ACTIVE", False)
    assert client.get(f"/x402/score?address={MINT}").status_code == 404
    info = client.get("/").get_json()
    assert info["x402"] == {"enabled": False}
    assert "paid_score" not in info["endpoints"]


def test_no_verdict_is_not_settled(client, monkeypatch):
    monkeypatch.setattr(api, "X402_ACTIVE", True)
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "ensure_live_cache_schema", lambda: None)
    monkeypatch.setattr(api, "fetch_live_cache", lambda _address: None)
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: None)

    response = client.get(f"/x402/score?address={MINT}")
    assert response.status_code == 503
    body = response.get_json()
    assert body["label"] == "UNKNOWN"
    assert body["x402"]["charged"] is False


def test_invalid_mint_is_not_settled(client, monkeypatch):
    monkeypatch.setattr(api, "X402_ACTIVE", True)
    assert client.get("/x402/score?address=notamint").status_code == 400


def test_free_score_body_has_no_x402_field(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "ensure_live_cache_schema", lambda: None)
    monkeypatch.setattr(api, "fetch_live_cache", lambda _address: None)
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: None)
    body = client.get(f"/score?address={MINT}").get_json()
    assert "x402" not in body


def test_real_server_builds_without_network():
    # Catches wiring mistakes (wrong scheme class, wrong client) that would
    # otherwise only show up as a silently disabled paid route in production.
    server = x402_solana.build_server(settings(), record=lambda _record: None)
    assert server is not None
