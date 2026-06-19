from datetime import datetime, timezone

import pytest

import app as api


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture()
def client():
    api.app.config.update(TESTING=True)
    return api.app.test_client()


def test_cache_hit_uses_precomputed_risk(monkeypatch, client):
    monkeypatch.setattr(
        api,
        "fetch_latest_scan",
        lambda address: {
            "contract_address": address,
            "chain": "SOLANA",
            "label": "DANGER",
            "full_record": {
                "risk_percent": 85,
                "rugcheck_score": 1,
                "token_name": "Example",
                "token_symbol": "EX",
                "creator_rug_rate": 94.7,
            },
            "created_at": datetime(2026, 6, 19, tzinfo=timezone.utc),
        },
    )

    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["ok"] is True
    assert data["risk_score"] == 85
    assert data["label"] == "DANGER"
    assert data["source"] == "postgres_cache"


def test_cache_miss_is_unknown_never_good(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda address: None)
    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["ok"] is True
    assert data["label"] == "UNKNOWN"
    assert data["risk_score"] is None
    assert data["risk_flags"] == ["not_in_intelligence_db"]


def test_evm_address_is_rejected(client):
    response = client.get("/score?address=0x2801225bfd2cb8959e344ebf37bf7f92632c9a51")
    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": "invalid solana mint address",
    }


def test_malformed_full_record_falls_back_safely(monkeypatch, client):
    monkeypatch.setattr(
        api,
        "fetch_latest_scan",
        lambda address: {
            "contract_address": address,
            "chain": "SOLANA",
            "label": None,
            "full_record": "not-json",
            "created_at": None,
        },
    )
    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["risk_score"] == 55
    assert data["label"] == "WARN"


def test_db_error_is_503(monkeypatch, client):
    def fail(_address):
        raise RuntimeError("database down")

    monkeypatch.setattr(api, "fetch_latest_scan", fail)
    response = client.get(f"/score?address={USDC}")
    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "error": "intelligence db unavailable",
        "source": "db_error",
    }
