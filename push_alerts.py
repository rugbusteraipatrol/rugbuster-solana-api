"""Push alerts: tell a phone the moment a tracked token's creator sells.

Why this exists. The app used to re-check tracked tokens itself, in an Android
background task. Android runs those at most every 15 minutes and Samsung
defers them for hours while the phone sleeps: on 2026-10-06 the creator of
Seedthink sold their 6.7% at 21:43 and the phone said so at 12:27 the next
day, when it was picked up. On a pump.fun token the price is gone in seconds.

How it works. When the app tracks a token it sends its Firebase device token
and the mint here. The service stores the creator and the share they held
(read from the same /score answer the app shows), and asks Helius to call
/hooks/helius whenever that creator wallet signs or receives a transaction.
On each call the creator's balance in that mint is read again; when it has
fallen to 5% or less of what they held, every phone tracking the token gets a
Firebase Cloud Messaging notification. A poll every 10 minutes catches any
webhook that never arrives.

Nothing here reads or writes a verdict. A failure to notify is logged and
retried on the next poll; it never fails a request.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable
from urllib.error import HTTPError

from rpc_http import rpc_post

SOLD_FRACTION = 0.05  # at or below 5% of the share held when tracking began
POLL_SECONDS = 600
WEBHOOK_SYNC_SECONDS = 30
MAX_WATCHES_PER_DEVICE = 100
RPC_TIMEOUT = 6

HELIUS_API = "https://api.helius.xyz/v0/webhooks"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_state_lock = threading.Lock()
_webhook_dirty = threading.Event()
_access_token: dict[str, Any] = {"token": None, "expires": 0.0}
_started = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_watches (
  device_token TEXT NOT NULL,
  mint TEXT NOT NULL,
  creator TEXT NOT NULL,
  symbol TEXT,
  start_share_pct DOUBLE PRECISION NOT NULL,
  last_share_pct DOUBLE PRECISION,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  notified_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_token, mint)
);
CREATE INDEX IF NOT EXISTS idx_push_watches_creator ON push_watches (creator) WHERE active;
CREATE INDEX IF NOT EXISTS idx_push_watches_mint ON push_watches (mint) WHERE active;
"""


# --- configuration -------------------------------------------------------


def rpc_url() -> str | None:
    return os.getenv("SOLANA_RPC_URL", "").strip() or None


def helius_key() -> str | None:
    explicit = os.getenv("HELIUS_API_KEY", "").strip()
    if explicit:
        return explicit
    url = rpc_url() or ""
    if "helius" in url and "api-key=" in url:
        return url.split("api-key=", 1)[1].split("&", 1)[0] or None
    return None


def webhook_url() -> str | None:
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return f"{base}/hooks/helius" if base else None


def webhook_secret() -> str | None:
    return os.getenv("HELIUS_WEBHOOK_SECRET", "").strip() or None


def service_account() -> dict[str, Any] | None:
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64", "").strip()
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:  # noqa: BLE001
        return None


# --- chain reads ----------------------------------------------------------


def creator_share_pct(mint: str, creator: str, call: Callable[[str, Any], Any] | None = None) -> float | None:
    """The creator's share of supply right now, in percent. None if unreadable."""
    url = rpc_url()
    if call is None:
        if not url:
            return None
        call = lambda method, params: rpc_post(url, method, params, RPC_TIMEOUT)  # noqa: E731
    try:
        supply = call("getTokenSupply", [mint]) or {}
        total = float((supply.get("value") or {}).get("amount") or 0)
        if total <= 0:
            return None
        accounts = call("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
        held = 0.0
        for account in accounts.get("value") or []:
            info = (((account.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            held += float((info.get("tokenAmount") or {}).get("amount") or 0)
        return 100.0 * held / total
    except Exception:  # noqa: BLE001
        return None


def has_sold(start_share: float, now_share: float | None) -> bool:
    return now_share is not None and start_share > 0 and now_share <= start_share * SOLD_FRACTION


def alert_text(symbol: str | None, start_share: float) -> str:
    name = symbol or "a token you track"
    return f"The creator of {name} just sold their {start_share:.1f}%."


# --- Firebase Cloud Messaging ---------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fcm_access_token(account: dict[str, Any]) -> str:
    now = time.time()
    with _state_lock:
        if _access_token["token"] and now < _access_token["expires"] - 60:
            return _access_token["token"]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({
        "iss": account["client_email"], "scope": FCM_SCOPE, "aud": account["token_uri"],
        "iat": int(now), "exp": int(now) + 3600,
    }).encode())
    key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header}.{claims}.{_b64url(signature)}"
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(account["token_uri"], data=body), timeout=10) as response:
        token = json.load(response)
    with _state_lock:
        _access_token.update(token=token["access_token"], expires=now + float(token.get("expires_in", 3600)))
    return token["access_token"]


def send_push(device_token: str, body: str, mint: str) -> str:
    """'sent', 'gone' (the token is no longer valid) or 'failed'."""
    account = service_account()
    if not account:
        return "failed"
    message = {
        "message": {
            "token": device_token,
            "notification": {"title": "RugBuster", "body": body},
            "data": {"mint": mint},
            "android": {"priority": "HIGH", "notification": {"channel_id": "default"}},
        }
    }
    url = f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send"
    try:
        request = urllib.request.Request(
            url, data=json.dumps(message).encode(),
            headers={"Authorization": f"Bearer {fcm_access_token(account)}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10):
            return "sent"
    except HTTPError as error:
        return "gone" if error.code in (400, 404) else "failed"
    except Exception:  # noqa: BLE001
        return "failed"


# --- storage -----------------------------------------------------------------


class Store:
    """The push_watches table. `connect` returns a DB-API connection."""

    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect
        self.ready = False

    def ensure(self) -> None:
        if self.ready:
            return
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA)
        self.ready = True

    def add(self, device: str, mint: str, creator: str, symbol: str | None, share: float) -> str:
        self.ensure()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM push_watches WHERE device_token = %s AND active", (device,))
                if cursor.fetchone()[0] >= MAX_WATCHES_PER_DEVICE:
                    return "limit"
                cursor.execute("SELECT 1 FROM push_watches WHERE active AND creator = %s LIMIT 1", (creator,))
                known_creator = cursor.fetchone() is not None
                cursor.execute(
                    """
                    INSERT INTO push_watches (device_token, mint, creator, symbol, start_share_pct, last_share_pct)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (device_token, mint) DO UPDATE
                      SET active = TRUE, creator = EXCLUDED.creator, symbol = EXCLUDED.symbol,
                          start_share_pct = EXCLUDED.start_share_pct, last_share_pct = EXCLUDED.last_share_pct,
                          notified_at = NULL, updated_at = now()
                    """,
                    (device, mint, creator, symbol, share, share),
                )
        return "added" if known_creator else "added_new_creator"

    def remove(self, device: str, mint: str) -> None:
        self.ensure()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE push_watches SET active = FALSE, updated_at = now() WHERE device_token = %s AND mint = %s",
                    (device, mint),
                )

    def active_creators(self) -> list[str]:
        self.ensure()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DISTINCT creator FROM push_watches WHERE active")
                return [row[0] for row in cursor.fetchall()]

    def watched(self, creators: list[str] | None = None) -> list[tuple[str, str, str | None, float]]:
        """(mint, creator, symbol, start share) for every active watch, one row per mint."""
        self.ensure()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                if creators is None:
                    cursor.execute(
                        "SELECT mint, creator, max(symbol), max(start_share_pct) FROM push_watches "
                        "WHERE active GROUP BY mint, creator"
                    )
                else:
                    cursor.execute(
                        "SELECT mint, creator, max(symbol), max(start_share_pct) FROM push_watches "
                        "WHERE active AND creator = ANY(%s) GROUP BY mint, creator",
                        (creators,),
                    )
                return [(row[0], row[1], row[2], float(row[3])) for row in cursor.fetchall()]

    def devices_for(self, mint: str) -> list[tuple[str, float, str | None]]:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT device_token, start_share_pct, symbol FROM push_watches "
                    "WHERE active AND mint = %s AND notified_at IS NULL",
                    (mint,),
                )
                return [(row[0], float(row[1]), row[2]) for row in cursor.fetchall()]

    def mark(self, device: str, mint: str, share: float | None, notified: bool, gone: bool = False) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE push_watches
                       SET last_share_pct = %s, updated_at = now(),
                           notified_at = CASE WHEN %s THEN now() ELSE notified_at END,
                           active = CASE WHEN %s OR %s THEN FALSE ELSE active END
                     WHERE device_token = %s AND mint = %s
                    """,
                    (share, notified, notified, gone, device, mint),
                )

    def counts(self) -> dict[str, int]:
        self.ensure()
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FILTER (WHERE active), count(DISTINCT device_token) FILTER (WHERE active), "
                    "count(*) FILTER (WHERE notified_at IS NOT NULL) FROM push_watches"
                )
                active, devices, notified = cursor.fetchone()
                return {"active_watches": active, "devices": devices, "alerts_sent": notified}


# --- the check ---------------------------------------------------------------


def check(store: Store, creators: list[str] | None = None,
          share_of: Callable[[str, str], float | None] = creator_share_pct,
          push: Callable[[str, str, str], str] = send_push) -> int:
    """Re-read the watched creators' balances; notify on a sale. Returns pushes sent."""
    sent = 0
    for mint, creator, _symbol, start in store.watched(creators):
        now_share = share_of(mint, creator)
        if not has_sold(start, now_share):
            continue
        for device, device_start, symbol in store.devices_for(mint):
            outcome = push(device, alert_text(symbol, device_start), mint)
            store.mark(device, mint, now_share, notified=outcome == "sent", gone=outcome == "gone")
            sent += outcome == "sent"
    return sent


def creators_in(payload: Any) -> set[str]:
    """Every account address in a Helius raw or enhanced webhook payload."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if 32 <= len(value) <= 44:
                found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


# --- Helius webhook ---------------------------------------------------------------


def sync_webhook(store: Store) -> str:
    """Point the one Helius webhook at every creator being watched."""
    key, target, secret = helius_key(), webhook_url(), webhook_secret()
    if not (key and target and secret):
        return "not_configured"
    addresses = sorted(store.active_creators())
    with urllib.request.urlopen(f"{HELIUS_API}?api-key={key}", timeout=10) as response:
        hooks = json.load(response) or []
    ours = next((hook for hook in hooks if hook.get("webhookURL") == target), None)
    if not addresses and not ours:
        return "nothing_to_watch"
    body = {
        "webhookURL": target, "transactionTypes": ["ANY"], "webhookType": "raw",
        "accountAddresses": addresses or ["11111111111111111111111111111111"], "authHeader": secret,
    }
    if ours and sorted(ours.get("accountAddresses") or []) == addresses:
        return "unchanged"
    method, url = ("PUT", f"{HELIUS_API}/{ours['webhookID']}?api-key={key}") if ours else ("POST", f"{HELIUS_API}?api-key={key}")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10):
        return "updated" if ours else "created"


def mark_webhook_dirty() -> None:
    _webhook_dirty.set()


def start_background(store: Store, log: Callable[[str], None] = print) -> None:
    """One thread: keep the webhook in step and poll every POLL_SECONDS."""
    global _started
    with _state_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        # Webhook edits cost 100 credits each, so additions are batched: the
        # webhook is synced at most once per WEBHOOK_SYNC_SECONDS.
        last_poll = 0.0
        _webhook_dirty.set()
        while True:
            try:
                if _webhook_dirty.is_set():
                    _webhook_dirty.clear()
                    log(f"push: webhook {sync_webhook(store)}")
                if time.time() - last_poll >= POLL_SECONDS:
                    last_poll = time.time()
                    sent = check(store)
                    if sent:
                        log(f"push: poll sent {sent}")
            except Exception as error:  # noqa: BLE001
                log(f"push: background error {str(error)[:160]}")
            time.sleep(WEBHOOK_SYNC_SECONDS)

    threading.Thread(target=loop, name="push-alerts", daemon=True).start()

