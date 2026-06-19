from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest

import app as api


USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture()
def client():
    api.app.config.update(TESTING=True)
    return api.app.test_client()


@pytest.fixture(autouse=True)
def isolate_live_dependencies(monkeypatch):
    monkeypatch.setattr(api, "ensure_live_cache_schema", lambda: None)
    monkeypatch.setattr(api, "fetch_live_cache", lambda _address: None)


def collector_row(address=USDC):
    return {
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
    }


def live_report(**overrides):
    report = {
        "score": 12,
        "score_normalised": 12,
        "rugged": False,
        "token": {"mintAuthority": None, "freezeAuthority": None},
        "tokenMeta": {"name": "Live Token", "symbol": "LIVE", "mutable": False},
        "risks": [],
    }
    report.update(overrides)
    return report


def test_collector_hit_has_priority_and_skips_live_scan(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: collector_row())

    def should_not_run(_address):
        raise AssertionError("RugCheck must not run on a collector cache hit")

    monkeypatch.setattr(api, "request_live_rugcheck", should_not_run)
    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["risk_score"] == 85
    assert data["label"] == "DANGER"
    assert data["source"] == "postgres_cache"


def test_live_rugcheck_success_is_scored_and_inserted(monkeypatch, client):
    inserted = []
    report = live_report()
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "request_live_rugcheck", lambda _address: report)
    monkeypatch.setattr(
        api,
        "insert_live_cache",
        lambda address, result, raw: inserted.append((address, result.copy(), raw)),
    )

    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["label"] == "GOOD"
    assert data["risk_score"] == 12
    assert data["source"] == "live_rugcheck"
    assert inserted == [(USDC, inserted[0][1], report)]
    assert inserted[0][1]["token_symbol"] == "LIVE"


def test_rugcheck_429_returns_safe_unknown(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "RUGCHECK_MIN_GAP_SECONDS", 0)

    def rate_limited(*_args, **_kwargs):
        raise HTTPError("https://rugcheck", 429, "rate limited", {}, None)

    monkeypatch.setattr(api, "urlopen", rate_limited)
    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["label"] == "UNKNOWN"
    assert data["risk_score"] is None
    assert data["source"] == "live_scan_unavailable"


def test_rugcheck_timeout_returns_safe_unknown(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(api, "RUGCHECK_MIN_GAP_SECONDS", 0)
    monkeypatch.setattr(
        api,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    response = client.get(f"/score?address={USDC}")
    data = response.get_json()
    assert response.status_code == 200
    assert data["label"] == "UNKNOWN"
    assert data["source"] == "live_scan_unavailable"


def test_rugged_override_forces_danger(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(
        api,
        "request_live_rugcheck",
        lambda _address: live_report(score=1, score_normalised=1, rugged=True),
    )
    monkeypatch.setattr(api, "insert_live_cache", lambda *_args: None)
    data = client.get(f"/score?address={USDC}").get_json()
    assert data["label"] == "DANGER"
    assert data["risk_score"] == 98
    assert "rugcheck_flagged_rugged" in data["risk_flags"]


def test_recent_live_cache_hit_skips_rugcheck(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(
        api,
        "fetch_live_cache",
        lambda address: {
            "contract_address": address,
            "risk_score": 46,
            "label": "WARN",
            "rugcheck_score": 600,
            "risk_flags": ["mutable_metadata"],
            "token_name": "Cached Live",
            "token_symbol": "CL",
            "created_at": datetime(2026, 6, 19, tzinfo=timezone.utc),
        },
    )

    def should_not_run(_address):
        raise AssertionError("RugCheck must not run on a fresh live-cache hit")

    monkeypatch.setattr(api, "request_live_rugcheck", should_not_run)
    data = client.get(f"/score?address={USDC}").get_json()
    assert data["source"] == "live_cache"
    assert data["label"] == "WARN"
    assert data["risk_score"] == 46


def test_evm_address_is_rejected(client):
    response = client.get("/score?address=0x2801225bfd2cb8959e344ebf37bf7f92632c9a51")
    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "invalid solana mint address"}


def test_malformed_collector_record_falls_back_safely(monkeypatch, client):
    row = collector_row()
    row.update({"label": None, "full_record": "not-json", "created_at": None})
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: row)
    data = client.get(f"/score?address={USDC}").get_json()
    assert data["risk_score"] == 55
    assert data["label"] == "WARN"


def test_collector_db_error_is_503(monkeypatch, client):
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


def test_live_cache_schema_error_degrades_to_unknown(monkeypatch, client):
    monkeypatch.setattr(api, "fetch_latest_scan", lambda _address: None)
    monkeypatch.setattr(
        api, "ensure_live_cache_schema", lambda: (_ for _ in ()).throw(RuntimeError("no ddl"))
    )
    data = client.get(f"/score?address={USDC}").get_json()
    assert data["label"] == "UNKNOWN"
    assert data["source"] == "live_scan_unavailable"
