"""Push alerts: a tracked creator's sale reaches the phone without the phone asking.

Seedthink, 2026-10-06: the creator sold their 6.7% at 21:43 and the phone,
re-checking on its own, said so at 12:27 the next day. These tests pin the
server side that replaces that: what counts as a sale, who gets told, and that
nobody is told twice.
"""

from __future__ import annotations

import pytest

import app as app_module
import push_alerts

MINT = "GS5zxjMkHftNgedLt2n7bKPRJ2G28QGqNVQgKNdzqmCh"
CREATOR = "5F1sAbCdEfGhJkLmNoPqRsTuVwXyZ123456789abcd"
DEVICE = "fcm-token:" + "x" * 40


class FakeStore:
    def __init__(self, rows):
        self.rows = rows  # device -> dict
        self.marks = []

    def watched(self, creators=None):
        seen = {}
        for row in self.rows.values():
            if row["active"] and (creators is None or row["creator"] in creators):
                seen[(row["mint"], row["creator"])] = (row["mint"], row["creator"], row["symbol"], row["start"])
        return list(seen.values())

    def devices_for(self, mint):
        return [(d, r["start"], r["symbol"]) for d, r in self.rows.items()
                if r["active"] and r["mint"] == mint and not r["notified"]]

    def mark(self, device, mint, share, notified, gone=False):
        self.marks.append((device, notified, gone))
        if notified:
            self.rows[device]["notified"] = True
        if notified or gone:
            self.rows[device]["active"] = False

    def active_creators(self):
        return sorted({r["creator"] for r in self.rows.values() if r["active"]})


def _row(start=6.7):
    return {"mint": MINT, "creator": CREATOR, "symbol": "Seedthink", "start": start, "active": True, "notified": False}


def test_a_sale_is_95_percent_of_the_tracked_share_gone():
    assert push_alerts.has_sold(6.7, 0.0)
    assert push_alerts.has_sold(6.7, 0.3)
    assert not push_alerts.has_sold(6.7, 3.0)
    assert not push_alerts.has_sold(6.7, None)


def test_the_alert_names_the_token_and_the_share():
    assert push_alerts.alert_text("Seedthink", 6.706) == "The creator of Seedthink just sold their 6.7%."


def test_every_tracking_phone_is_told_once():
    store = FakeStore({DEVICE: _row(), DEVICE + "2": _row()})
    pushes = []
    push = lambda device, body, mint: pushes.append((device, body)) or "sent"  # noqa: E731
    sent = push_alerts.check(store, share_of=lambda m, c: 0.0, push=push)
    assert sent == 2 and len(pushes) == 2
    assert push_alerts.check(store, share_of=lambda m, c: 0.0, push=push) == 0


def test_no_sale_no_push():
    store = FakeStore({DEVICE: _row()})
    sent = push_alerts.check(store, share_of=lambda m, c: 6.7, push=lambda *a: pytest.fail("pushed"))
    assert sent == 0


def test_a_dead_device_token_stops_being_watched():
    store = FakeStore({DEVICE: _row()})
    push_alerts.check(store, share_of=lambda m, c: 0.0, push=lambda *a: "gone")
    assert store.rows[DEVICE]["active"] is False and store.rows[DEVICE]["notified"] is False


def test_a_failed_push_is_retried_on_the_next_check():
    store = FakeStore({DEVICE: _row()})
    push_alerts.check(store, share_of=lambda m, c: 0.0, push=lambda *a: "failed")
    assert store.rows[DEVICE]["active"] is True
    assert push_alerts.check(store, share_of=lambda m, c: 0.0, push=lambda *a: "sent") == 1


def test_creator_share_reads_balance_over_supply():
    def call(method, params):
        if method == "getTokenSupply":
            return {"value": {"amount": "1000000"}}
        return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "67000"}}}}}}]}

    assert push_alerts.creator_share_pct(MINT, CREATOR, call=call) == pytest.approx(6.7)


def test_an_unreadable_balance_is_not_a_sale():
    def call(method, params):
        raise TimeoutError

    share = push_alerts.creator_share_pct(MINT, CREATOR, call=call)
    assert share is None and not push_alerts.has_sold(6.7, share)


def test_webhook_payload_addresses_are_found_anywhere():
    payload = [{"transaction": {"message": {"accountKeys": [CREATOR, "So11111111111111111111111111111111111111112"]}}}]
    assert CREATOR in push_alerts.creators_in(payload)


def test_helius_key_comes_from_the_rpc_url(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.setenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=abc123")
    assert push_alerts.helius_key() == "abc123"


# --- routes ----------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    added = []

    class Store:
        def add(self, device, mint, creator, symbol, share):
            added.append((device, mint, creator, symbol, share))
            return "added_new_creator"

        def remove(self, device, mint):
            added.append(("removed", device, mint))

        def active_creators(self):
            return [CREATOR]

    monkeypatch.setattr(app_module, "push_store", Store())
    monkeypatch.setattr(push_alerts, "mark_webhook_dirty", lambda: added.append("dirty"))
    client = app_module.app.test_client()
    client.added = added
    return client


def _score_answer(monkeypatch, position, symbol="Seedthink"):
    class Response:
        def get_json(self, silent=True):
            return {"creator_position": position, "token_symbol": symbol}

    real = app_module.app.test_client

    class Client:
        def get(self, url):
            return Response()

    monkeypatch.setattr(app_module.app, "test_client", lambda: Client())
    return real()


def test_watch_stores_a_holding_creator(monkeypatch, client):
    real = _score_answer(monkeypatch, {"status": "holding", "creator": CREATOR, "current_share_pct": 6.7})
    response = real.post("/watch", json={"device_token": DEVICE, "mint": MINT})
    assert response.status_code == 200 and response.get_json()["watching"] is True
    assert client.added[0][2] == CREATOR and "dirty" in client.added


def test_watch_declines_a_creator_who_already_sold(monkeypatch, client):
    real = _score_answer(monkeypatch, {"status": "sold", "creator": CREATOR, "share_at_creation_pct": 6.7})
    body = real.post("/watch", json={"device_token": DEVICE, "mint": MINT}).get_json()
    assert body["watching"] is False and client.added == []


def test_watch_rejects_bad_input(client):
    assert client.post("/watch", json={"device_token": "x", "mint": MINT}).status_code == 400
    assert client.post("/watch", json={"device_token": DEVICE, "mint": "0xabc"}).status_code == 400


def test_helius_hook_needs_the_secret(monkeypatch, client):
    monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", "s3cret")
    assert client.post("/hooks/helius", json=[]).status_code == 401
    monkeypatch.setattr(push_alerts, "check", lambda store, creators: len(creators))
    body = client.post("/hooks/helius", json=[{"accountKeys": [CREATOR]}], headers={"Authorization": "s3cret"}).get_json()
    assert body == {"ok": True, "creators": 1, "sent": 1}


def test_helius_requests_name_the_client():
    """Helius answers 403 to Python-urllib's default user agent (2026-10-07)."""
    assert push_alerts.HEADERS["User-Agent"].startswith("RugBuster")
