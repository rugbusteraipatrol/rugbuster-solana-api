import perks
import app as api


def test_skr_or_seeker_token_unlocks_pro(monkeypatch):
    monkeypatch.setattr(perks, "skr_balance", lambda w: 0.0)
    monkeypatch.setattr(perks, "holds_seeker_genesis_token", lambda w: True)
    perks._cache.clear()
    r = perks.perks_for("A" * 44)
    assert r["pro"] and r["reason"] == "Seeker Genesis Token"
    monkeypatch.setattr(perks, "holds_seeker_genesis_token", lambda w: False)
    monkeypatch.setattr(perks, "skr_balance", lambda w: 12.5)
    perks._cache.clear()
    r = perks.perks_for("B" * 44)
    assert r["pro"] and r["reason"] == "12.5 SKR"


def test_dust_below_one_skr_is_not_pro(monkeypatch):
    monkeypatch.setattr(perks, "skr_balance", lambda w: 0.4)
    monkeypatch.setattr(perks, "holds_seeker_genesis_token", lambda w: False)
    perks._cache.clear()
    assert perks.perks_for("C" * 44)["pro"] is False


def test_an_unread_wallet_is_reported_not_denied(monkeypatch):
    def boom(w):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(perks, "skr_balance", boom)
    perks._cache.clear()
    r = perks.perks_for("D" * 44)
    assert r["checked"] is False and r["pro"] is False
    api.app.config.update(TESTING=True)
    resp = api.app.test_client().get("/perks?wallet=" + "D" * 44)
    assert resp.status_code == 503


def test_bad_wallet_is_400():
    api.app.config.update(TESTING=True)
    assert api.app.test_client().get("/perks?wallet=nope").status_code == 400
