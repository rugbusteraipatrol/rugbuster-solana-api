import json
import os
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.extras import Json, RealDictCursor

from scoring import score_live_rugcheck_report, score_scan_row


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE58_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{address}/report"
RUGCHECK_TIMEOUT_SECONDS = 6
RUGCHECK_MIN_GAP_SECONDS = 0.5

_rugcheck_lock = threading.Lock()
_last_rugcheck_call = 0.0
_schema_lock = threading.Lock()
_schema_ready = False

app = Flask(__name__)


def is_valid_solana_mint(address: str) -> bool:
    return bool(address and not address.startswith("0x") and BASE58_MINT_RE.fullmatch(address))


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL)


def fetch_latest_scan(address: str) -> dict[str, Any] | None:
    """Read collector-owned data. Never write to solana_scans."""
    with db_connect() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT contract_address, chain, label, full_record, created_at
                FROM solana_scans
                WHERE contract_address = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (address,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def ensure_live_cache_schema() -> None:
    """Create only the service-owned additive cache table and index."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with db_connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS solana_live_cache (
                      id BIGSERIAL PRIMARY KEY,
                      contract_address TEXT NOT NULL,
                      source TEXT NOT NULL DEFAULT 'live_rugcheck',
                      risk_score NUMERIC,
                      label TEXT,
                      rugcheck_score INTEGER,
                      risk_flags JSONB,
                      raw_response JSONB,
                      token_name TEXT,
                      token_symbol TEXT,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    CREATE INDEX IF NOT EXISTS idx_solana_live_cache_address
                      ON solana_live_cache (contract_address, created_at DESC);
                    """
                )
        _schema_ready = True


def fetch_live_cache(address: str) -> dict[str, Any] | None:
    with db_connect() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT contract_address, risk_score, label, rugcheck_score,
                       risk_flags, token_name, token_symbol, created_at
                FROM solana_live_cache
                WHERE contract_address = %s
                  AND created_at >= now() - interval '1 hour'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (address,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None


def insert_live_cache(address: str, result: dict[str, Any], raw_response: dict[str, Any]) -> None:
    with db_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO solana_live_cache (
                    contract_address, source, risk_score, label, rugcheck_score,
                    risk_flags, raw_response, token_name, token_symbol
                ) VALUES (%s, 'live_rugcheck', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    address,
                    result.get("risk_score"),
                    result.get("label"),
                    result.get("rugcheck_score"),
                    Json(result.get("risk_flags") or []),
                    Json(raw_response),
                    result.get("token_name"),
                    result.get("token_symbol"),
                ),
            )


def request_live_rugcheck(address: str) -> dict[str, Any] | None:
    """Make one serialized, rate-limited RugCheck request."""
    global _last_rugcheck_call
    with _rugcheck_lock:
        wait_seconds = RUGCHECK_MIN_GAP_SECONDS - (time.monotonic() - _last_rugcheck_call)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _last_rugcheck_call = time.monotonic()
        request_object = Request(
            RUGCHECK_REPORT_URL.format(address=quote(address, safe="")),
            headers={"Accept": "application/json", "User-Agent": "RugBuster-Solana-API/2"},
        )
        try:
            with urlopen(request_object, timeout=RUGCHECK_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) else None
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None


def cache_miss_response(address: str, source: str = "cache_miss") -> dict[str, Any]:
    return {
        "ok": True,
        "address": address,
        "chain": "solana",
        "risk_score": None,
        "label": "UNKNOWN",
        "risk_flags": ["not_in_intelligence_db"],
        "source": source,
        "scanned_at": None,
        "note": (
            "No verified score is available. Treat this token as unverified."
        ),
    }


def live_cache_result(row: dict[str, Any], address: str) -> dict[str, Any]:
    flags = row.get("risk_flags") or []
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except json.JSONDecodeError:
            flags = []
    risk_value = row.get("risk_score")
    scanned_at = row.get("created_at")
    return {
        "ok": True,
        "address": row.get("contract_address") or address,
        "chain": "solana",
        "risk_score": round(float(risk_value)) if risk_value is not None else None,
        "label": row.get("label") or "UNKNOWN",
        "rugcheck_score": row.get("rugcheck_score"),
        "risk_flags": flags,
        "token_name": row.get("token_name"),
        "token_symbol": row.get("token_symbol"),
        "source": "live_cache",
        "scanned_at": scanned_at.isoformat() if hasattr(scanned_at, "isoformat") else scanned_at,
    }


@app.get("/")
def service_info():
    return jsonify(
        {
            "ok": True,
            "name": "RugBuster Solana API",
            "version": "rugbuster-solana-api-v2",
            "chain": "solana",
            "mode": "collector_cache_plus_live_rugcheck",
            "endpoints": {
                "health": "/health",
                "score": "/score?address=<solana_mint>",
            },
        }
    )


@app.get("/health")
def health():
    try:
        with db_connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return jsonify({"ok": True, "database": "connected", "chain": "solana"})
    except Exception:
        return jsonify({"ok": False, "database": "unavailable", "chain": "solana"}), 503


@app.get("/score")
def score():
    address = str(request.args.get("address") or "").strip()
    if not is_valid_solana_mint(address):
        return jsonify({"ok": False, "error": "invalid solana mint address"}), 400

    try:
        row = fetch_latest_scan(address)
    except Exception:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "intelligence db unavailable",
                    "source": "db_error",
                }
            ),
            503,
        )

    if row is None:
        try:
            ensure_live_cache_schema()
            cached_live = fetch_live_cache(address)
        except Exception:
            return jsonify(cache_miss_response(address, "live_scan_unavailable"))

        if cached_live is not None:
            return jsonify(live_cache_result(cached_live, address))

        report = request_live_rugcheck(address)
        if report is None:
            return jsonify(cache_miss_response(address, "live_scan_unavailable"))

        result = score_live_rugcheck_report(report)
        try:
            insert_live_cache(address, result, report)
        except Exception:
            # The verdict is still valid, but the next request may need to rescan.
            pass
        result.update(
            {
                "ok": True,
                "address": address,
                "chain": "solana",
                "source": "live_rugcheck",
                "scanned_at": None,
            }
        )
        return jsonify(result)

    result = score_scan_row(row)
    scanned_at = row.get("created_at")
    result.update(
        {
            "ok": True,
            "address": row.get("contract_address") or address,
            "chain": "solana",
            "source": "postgres_cache",
            "scanned_at": (
                scanned_at.isoformat() if hasattr(scanned_at, "isoformat") else scanned_at
            ),
        }
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
