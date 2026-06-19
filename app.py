import json
import os
import re
from typing import Any

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.extras import RealDictCursor

from scoring import score_scan_row


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BASE58_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

app = Flask(__name__)


def is_valid_solana_mint(address: str) -> bool:
    return bool(address and not address.startswith("0x") and BASE58_MINT_RE.fullmatch(address))


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL)


def fetch_latest_scan(address: str) -> dict[str, Any] | None:
    """Read one cached scan. This service never writes to Postgres."""
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


def cache_miss_response(address: str) -> dict[str, Any]:
    return {
        "ok": True,
        "address": address,
        "chain": "solana",
        "risk_score": None,
        "label": "UNKNOWN",
        "risk_flags": ["not_in_intelligence_db"],
        "source": "cache_miss",
        "scanned_at": None,
        "note": (
            "Token not yet in RugBuster intelligence DB. Live scan arrives in a later "
            "version. Treat as unverified."
        ),
    }


@app.get("/")
def service_info():
    return jsonify(
        {
            "ok": True,
            "name": "RugBuster Solana API",
            "version": "rugbuster-solana-api-v1",
            "chain": "solana",
            "mode": "cache_only",
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
        return jsonify(cache_miss_response(address))

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
