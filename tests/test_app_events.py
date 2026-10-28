import app_events


def test_parse_keeps_known_events_only():
    assert app_events.parse({"dev": "abc12345xyz", "ev": "open", "v": "1.7.4"}) == ("abc12345xyz", "open", "1.7.4")
    assert app_events.parse({"dev": "ABC12345XYZ", "ev": "SCAN"}) == ("abc12345xyz", "scan", "")
    assert app_events.parse({"dev": "abc12345xyz", "ev": "delete"}) is None
    assert app_events.parse({"dev": "short", "ev": "open"}) is None
    assert app_events.parse({"dev": "abc12345xyz", "ev": "open", "v": "<script>"}) == ("abc12345xyz", "open", "")
    assert app_events.parse(None) is None


def test_endpoint_rejects_unknown_event():
    from app import app
    r = app.test_client().post("/app/event", json={"dev": "abc12345xyz", "ev": "nope"})
    assert r.status_code == 400
