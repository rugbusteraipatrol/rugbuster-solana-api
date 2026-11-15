"""Free daily limit on /score (quota.py)."""
import pytest

import quota

MINT = "So11111111111111111111111111111111111111112"


class FakeDb:
    """Stands in for psycopg2: keeps score_quota counts in a dict."""

    def __init__(self):
        self.calls = {}
        self.fail = False

    def connect(self):
        if self.fail:
            raise RuntimeError("db down")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self._last = None
        if params and "INSERT INTO score_quota" in sql:
            key = (params[0], params[1])
            self.calls[key] = self.calls.get(key, 0) + 1
            self._last = (self.calls[key],)

    def fetchone(self):
        return self._last


@pytest.fixture
def db(monkeypatch):
    import app as app_module

    fake = FakeDb()
    monkeypatch.setattr(app_module, "db_connect", fake.connect)
    monkeypatch.setattr(quota, "FREE_PER_DAY", 3)
    # Keep the real /score from touching the network or a database.
    monkeypatch.setattr(app_module, "fetch_latest_scan", lambda address: (_ for _ in ()).throw(RuntimeError("no db")))
    return fake


def call(ua, ip="1.2.3.4"):
    from app import app

    return app.test_client().get(f"/score?address={MINT}", headers={"User-Agent": ua, "X-Real-IP": ip})


def test_named_program_is_counted_across_ips(db):
    statuses = [call("Purpump/0.1 (personal research app)", ip=f"10.0.0.{i}").status_code for i in range(5)]
    assert statuses[:3] == [503, 503, 503]  # passed the limit check, then hit the (disabled) db
    assert statuses[3:] == [402, 402]


def test_browsers_are_counted_per_ip_only(db):
    for i in range(5):
        assert call("Mozilla/5.0", ip=f"10.0.1.{i}").status_code != 402
    statuses = [call("Mozilla/5.0", ip="10.0.2.1").status_code for _ in range(4)]
    assert statuses[-1] == 402 and 402 not in statuses[:3]


def test_limit_answer_points_to_paid_route(db):
    for _ in range(3):
        call("memecoin-scanner-research/1.0")
    r = call("memecoin-scanner-research/1.0")
    body = r.get_json()
    assert r.status_code == 402
    assert body["error"] == "free_daily_limit"
    assert body["paid_url"].endswith(f"/x402/score?address={MINT}")
    assert body["price"]["amount"] == "0.01"


def test_own_bot_is_never_counted(db):
    for _ in range(6):
        assert call("RugBuster-Bot/1.0 (+https://rugbuster.io)").status_code != 402
    assert db.calls == {}


def test_database_down_lets_calls_through(db):
    db.fail = True
    for _ in range(6):
        assert call("Purpump/0.1").status_code != 402


def test_other_routes_are_not_counted(db):
    from app import app

    client = app.test_client()
    for _ in range(5):
        client.get("/health", headers={"User-Agent": "Purpump/0.1"})
    assert db.calls == {}


def test_client_ip_trusts_edge_headers_only():
    assert quota.client_ip({"X-Real-IP": "9.9.9.9", "X-Forwarded-For": "1.1.1.1"}, "127.0.0.1") == "9.9.9.9"
    assert quota.client_ip({"X-Forwarded-For": "6.6.6.6, 5.5.5.5"}, "127.0.0.1") == "5.5.5.5"
    assert quota.client_ip({}, "127.0.0.1") == "127.0.0.1"


def test_no_raw_ip_is_stored():
    key = quota.caller_keys("185.98.171.244", "Purpump/0.1")[0]
    assert "185.98" not in quota.hashed(key, "2026-11-14")


def call_with_key(key, ip="1.2.3.4"):
    from app import app

    return app.test_client().get(
        f"/score?address={MINT}", headers={"User-Agent": "Mozilla/5.0", "X-Real-IP": ip, "X-API-Key": key}
    )


def test_key_uses_its_plan_cap_not_the_free_limit(db, monkeypatch):
    monkeypatch.setattr(quota, "lookup_key", lambda connect, raw: {"plan": "starter", "daily_limit": 5, "counter": "key:abc"})
    statuses = [call_with_key("rb_good").status_code for _ in range(6)]
    assert 402 not in statuses[:5] and 429 not in statuses[:5]
    assert statuses[5] == 429
    assert call_with_key("rb_good").get_json()["error"] == "plan_daily_limit"


def test_key_calls_do_not_use_up_the_free_ip_allowance(db, monkeypatch):
    monkeypatch.setattr(quota, "lookup_key", lambda connect, raw: {"plan": "starter", "daily_limit": 50, "counter": "key:abc"})
    for _ in range(5):
        call_with_key("rb_good")
    assert call("Mozilla/5.0").status_code != 402


def test_unknown_key_gets_401(db, monkeypatch):
    monkeypatch.setattr(quota, "lookup_key", lambda connect, raw: None)
    response = call_with_key("rb_wrong")
    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_api_key"


def test_key_lookup_outage_lets_the_call_through(db, monkeypatch):
    def broken(connect, raw):
        raise RuntimeError("db down")

    monkeypatch.setattr(quota, "lookup_key", broken)
    assert call_with_key("rb_any").status_code not in (401, 402, 429)


def test_key_also_read_from_query_and_only_hash_kept():
    assert quota.api_key_from({}, {"api_key": " rb_x "}) == "rb_x"
    assert quota.key_hash("rb_x") != "rb_x" and len(quota.key_hash("rb_x")) == 64


def test_issue_key_rejects_unknown_plan():
    with pytest.raises(ValueError):
        quota.issue_key(lambda: None, "x", "platinum")
