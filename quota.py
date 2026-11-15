"""Free daily limit on GET /score (2026-11-14).

Since 2026-11-11 a few outside programs call /score around the clock (about
1,000 fresh scans a day), and every fresh scan costs Helius credits. Anyone
keeps FREE_PER_DAY free calls per UTC day. After that /score answers 402 and
points to the paid route /x402/score ($0.01 USDC on Solana), which is unaffected.

A caller is counted under its IP address and, for a program that names itself,
also under that name, so a program spread over many IPs still shares one
allowance. Browser and generic library User-Agents are counted by IP only, so
one python-requests user cannot use up another's allowance. Our own bot is not
counted.

Only salted hashes of the keys are stored, never a raw IP. If the database is
unreachable the call is let through: a broken counter must never break the
free product.

API keys (2026-11-15, beta): a caller that sends a key in the X-API-Key header
(or ?api_key=) is counted under that key against its plan's daily cap instead
of the free 100. During the beta every plan is free; keys are issued by hand
with scripts/issue_api_key.py. Only a sha256 of a key is stored.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone

FREE_PER_DAY = int(os.getenv("SCORE_FREE_PER_DAY", "100"))
SALT = os.getenv("SCORE_QUOTA_SALT", "rugbuster-score-quota")
CONTACT = "fedja@rugbuster.io"

# Daily caps per plan: the monthly volume on rugbuster.io divided by 30.
PLANS = {"starter": 3400, "growth": 17000, "pro": 34000}

# Our own services, never counted.
OWN_AGENTS = ("RugBuster-Bot/", "rugbuster-solana-qa/", "rugbuster-confirmed-rug-regression/")

# Shared by many unrelated users, so never used as a caller identity.
GENERIC_AGENTS = (
    "mozilla/", "python-requests/", "python-urllib/", "python-httpx/", "aiohttp/",
    "curl/", "wget/", "node-fetch/", "node", "axios/", "undici", "go-http-client/",
    "okhttp/", "java/", "dart/", "postmanruntime/", "insomnia/", "ruby", "php",
)

_ready = False
_keys_ready = False

KEYS_TABLE = """
CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  plan TEXT NOT NULL,
  daily_limit INTEGER NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def client_ip(headers, remote_addr: str | None) -> str:
    """The caller's IP as Railway's edge reports it.

    X-Real-IP is set by the edge. X-Forwarded-For may carry entries the client
    wrote itself, so only its last entry (added by the edge) is trusted.
    """
    real = (headers.get("X-Real-IP") or "").strip()
    if real:
        return real
    forwarded = [part.strip() for part in (headers.get("X-Forwarded-For") or "").split(",") if part.strip()]
    if forwarded:
        return forwarded[-1]
    return remote_addr or "unknown"


def is_own(user_agent: str) -> bool:
    return any(user_agent.startswith(prefix) for prefix in OWN_AGENTS)


def caller_keys(ip: str, user_agent: str) -> list[str]:
    """Raw identities this call is counted under (hashed before storage)."""
    keys = [f"ip:{ip}"]
    ua = (user_agent or "").strip()
    if ua and not ua.lower().startswith(GENERIC_AGENTS):
        keys.append(f"ua:{ua[:200]}")
    return keys


def hashed(key: str, day: str) -> str:
    return hashlib.sha256(f"{SALT}|{day}|{key}".encode("utf-8")).hexdigest()[:32]


def count_call(connect, keys: list[str], day: str | None = None) -> int:
    """Add one call to every key and return the highest count among them."""
    global _ready
    day = day or today()
    highest = 0
    with connect() as connection:
        with connection.cursor() as cursor:
            if not _ready:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS score_quota (
                      day DATE NOT NULL,
                      caller TEXT NOT NULL,
                      kind TEXT NOT NULL,
                      calls INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY (day, caller)
                    )
                    """
                )
                _ready = True
            for key in keys:
                cursor.execute(
                    """
                    INSERT INTO score_quota (day, caller, kind, calls) VALUES (%s, %s, %s, 1)
                    ON CONFLICT (day, caller) DO UPDATE SET calls = score_quota.calls + 1
                    RETURNING calls
                    """,
                    (day, hashed(key, day), key.split(":", 1)[0]),
                )
                highest = max(highest, int(cursor.fetchone()[0]))
        connection.commit()
    return highest


def key_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def api_key_from(headers, args) -> str:
    return str(headers.get("X-API-Key") or args.get("api_key") or "").strip()


def lookup_key(connect, raw_key: str) -> dict | None:
    """The active key's plan and daily cap, or None for an unknown or revoked key."""
    global _keys_ready
    with connect() as connection:
        with connection.cursor() as cursor:
            if not _keys_ready:
                cursor.execute(KEYS_TABLE)
                _keys_ready = True
            cursor.execute(
                "SELECT plan, daily_limit FROM api_keys WHERE key_hash = %s AND active",
                (key_hash(raw_key),),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        return None
    return {"plan": row[0], "daily_limit": int(row[1]), "counter": f"key:{key_hash(raw_key)}"}


def issue_key(connect, label: str, plan: str) -> str:
    """Create a key for `plan` and return it. It is shown once and never stored in clear."""
    if plan not in PLANS:
        raise ValueError(f"unknown plan {plan!r}, use one of {sorted(PLANS)}")
    raw_key = "rb_" + secrets.token_urlsafe(24)
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(KEYS_TABLE)
            cursor.execute(
                "INSERT INTO api_keys (key_hash, label, plan, daily_limit) VALUES (%s, %s, %s, %s)",
                (key_hash(raw_key), label, plan, PLANS[plan]),
            )
        connection.commit()
    return raw_key


def invalid_key_answer(address: str) -> dict:
    return {
        "ok": False,
        "error": "invalid_api_key",
        "message": f"This API key is unknown or revoked. Leave it out to use the free {FREE_PER_DAY} checks a day, or write to {CONTACT}.",
        "contact": CONTACT,
        "address": address,
    }


def plan_limit_answer(address: str, plan: str, daily_limit: int) -> dict:
    return {
        "ok": False,
        "error": "plan_daily_limit",
        "plan": plan,
        "daily_limit": daily_limit,
        "message": f"Your {plan} key has used its {daily_limit} checks for today. Write to {CONTACT} for more.",
        "resets": "00:00 UTC",
        "contact": CONTACT,
        "address": address,
    }


def over_limit(calls: int) -> bool:
    return calls > FREE_PER_DAY


def limit_answer(address: str, paid_url: str) -> dict:
    return {
        "ok": False,
        "error": "free_daily_limit",
        "free_per_day": FREE_PER_DAY,
        "message": (
            f"The free limit of {FREE_PER_DAY} checks per day is used up. "
            "Pay per check over x402 at paid_url ($0.01 USDC on Solana), "
            f"or write to {CONTACT} for an API key (free during the beta)."
        ),
        "paid_url": paid_url,
        "price": {"amount": "0.01", "asset": "USDC", "network": "solana", "protocol": "x402"},
        "resets": "00:00 UTC",
        "contact": CONTACT,
        "address": address,
    }
