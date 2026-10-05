import pytest

import app as api


MINT = "SH55hfaipFAbwT42nQYhRoM5o5t61QpkmJ6p62vXB3m"


@pytest.fixture()
def client():
    api.app.config.update(TESTING=True)
    return api.app.test_client()


def _resolves_to(monkeypatch, ip):
    monkeypatch.setattr(api.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", (ip, 443))])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1"])
def test_metadata_image_never_reaches_a_private_address(monkeypatch, ip):
    _resolves_to(monkeypatch, ip)
    assert api._is_public_https("https://evil.example/logo.png") is False


def test_plain_http_is_refused(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    assert api._is_public_https("http://example.com/logo.png") is False
    assert api._is_public_https("https://example.com/logo.png") is True


def test_invalid_mint_is_rejected(client):
    assert client.get("/token-image?address=nope").status_code == 400


def test_image_is_served_with_cors_and_a_sandbox(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_token_meta", lambda a: {"image_candidates": ["https://img.example/a.svg"]})
    monkeypatch.setattr(api, "fetch_public_image", lambda url: (b"<svg/>", "image/svg+xml"))
    response = client.get(f"/token-image?address={MINT}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/svg+xml"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "sandbox" in response.headers["Content-Security-Policy"]


def test_no_readable_image_is_a_404(client, monkeypatch):
    monkeypatch.setattr(api, "fetch_token_meta", lambda a: {"image_candidates": ["https://img.example/a.png"]})
    monkeypatch.setattr(api, "fetch_public_image", lambda url: None)
    assert client.get(f"/token-image?address={MINT}").status_code == 404
