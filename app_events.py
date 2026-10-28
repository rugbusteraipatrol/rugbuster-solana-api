"""Anonymous usage counts from the phone app (2026-10-28).

The app sends one small event when it opens, scans, tracks, connects a wallet, gets Pro or pays.
Each install picks a random id on first start; no wallet address, token, IP or device detail is
stored. This only answers "how many phones used the app today, and for what".
"""
import re

EVENTS = {"open", "scan", "track", "connect", "pro", "pay"}
DEV_RE = re.compile(r"[a-z0-9]{8,32}")
VER_RE = re.compile(r"[0-9][0-9.]{0,11}")

_ready = False


def parse(body):
    """Return (dev, ev, ver) or None when the event is not one we keep."""
    if not isinstance(body, dict):
        return None
    dev = str(body.get("dev") or "").strip().lower()
    ev = str(body.get("ev") or "").strip().lower()
    ver = str(body.get("v") or "").strip()
    if not DEV_RE.fullmatch(dev) or ev not in EVENTS:
        return None
    return dev, ev, ver if VER_RE.fullmatch(ver) else ""


def record(connect, dev, ev, ver):
    """Insert one event. connect() returns a psycopg2 connection."""
    global _ready
    with connect() as connection:
        with connection.cursor() as cursor:
            if not _ready:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_events (
                      id BIGSERIAL PRIMARY KEY,
                      dev TEXT NOT NULL,
                      ev TEXT NOT NULL,
                      ver TEXT NOT NULL DEFAULT '',
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE INDEX IF NOT EXISTS idx_app_events_created ON app_events (created_at);
                    """
                )
                _ready = True
            cursor.execute("INSERT INTO app_events (dev, ev, ver) VALUES (%s, %s, %s)", (dev, ev, ver))
