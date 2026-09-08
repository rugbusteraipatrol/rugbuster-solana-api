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

from build_identity import build_identity
from evidence import build_evidence
from freshness import (
    LIVE_CACHE_MAX_AGE,
    assess,
    is_servable_as_current,
    now_utc,
    withhold_verdict,
)
from scoring import (
    SCORING_VERSION,
    has_recomputable_evidence,
    score_live_rugcheck_report,
    score_scan_row,
    stored_scoring_version,
)


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


@app.after_request
def _allow_browser_reads(response):
    # This is a public, read-only, GET-only score lookup -- no cookies/auth
    # header, so an open CORS policy is safe. Needed so the website (running
    # in a browser, unlike the Telegram bot's server-side requests) can call
    # this service directly instead of re-implementing scoring itself.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET"
    return response


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
                    ALTER TABLE solana_live_cache
                      ADD COLUMN IF NOT EXISTS scoring_version TEXT NOT NULL DEFAULT 'pre-2026.09.1';
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
                  AND scoring_version = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (address, SCORING_VERSION),
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
                    risk_flags, raw_response, token_name, token_symbol, scoring_version
                ) VALUES (%s, 'live_rugcheck', %s, %s, %s, %s, %s, %s, %s, %s)
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
                    SCORING_VERSION,
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


def retrieval_times() -> dict[str, Any]:
    """Timestamps for an upstream fetch, saying only what we actually know.

    RugCheck's report carries no observation timestamp, so we know when we
    retrieved it and not when it was observed. Stamping retrieval time into
    `observed_at` presented the second as the first: a report describing a
    snapshot taken minutes or hours earlier was reported as observed now, age
    zero, FRESH.

    `observed_at` therefore stays null, `fetched_at` carries the retrieval
    time, and `observation_coverage` says which of the two we have. The verdict
    is not withheld -- a fetch made a moment ago is legitimately the most
    current thing available -- but the response no longer claims to know when
    the upstream looked.
    """
    now = now_utc().isoformat()
    return {
        "scanned_at": None,
        "observed_at": None,
        "fetched_at": now,
        "retrieved_at": now,
        "age_seconds": None,
        "data_freshness": "RETRIEVED_NOW",
        "observation_coverage": "RETRIEVAL_TIME_ONLY",
        "observation_coverage_note": (
            "The upstream report carries no observation timestamp. We know when "
            "we fetched it, not when it was gathered."
        ),
    }


def _record_of(row: dict[str, Any]) -> dict[str, Any]:
    """The stored record from a collector row, whatever shape it arrived in."""
    record = row.get("full_record")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except json.JSONDecodeError:
            return {}
    return record if isinstance(record, dict) else {}


def with_identity(payload: dict[str, Any], report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Attach build provenance, and the evidence split, to any response.

    Every exit needs the identity, not only the successful ones: a review
    cannot tell which code produced an error or an UNKNOWN either, and those
    are the answers most likely to be argued about.

    The evidence block is only built for answers that carry a verdict -- an
    invalid-address 400 has nothing to split. `report` is the raw upstream
    response where the caller has one; without it the market and account
    readings come out UNKNOWN rather than being inferred from the verdict.
    """
    enriched = dict(payload)
    enriched.update(build_identity(SCORING_VERSION))
    if "label" in enriched:
        enriched["evidence"] = build_evidence(enriched, report)
    return enriched


def refresh_stale_record(address: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Get a current reading for a token whose stored evidence is not current.

    Checks the live cache before the network. Returns None when neither can
    supply one, leaving the caller to withhold the verdict.
    """
    try:
        ensure_live_cache_schema()
        cached_live = fetch_live_cache(address)
    except Exception:
        cached_live = None

    if cached_live is not None:
        cached_state = assess(cached_live.get("created_at"), max_age=LIVE_CACHE_MAX_AGE)
        # A cached row that is itself stale, undated or future-dated cannot
        # stand in for a refresh. Returning it early meant an unusable cache
        # blocked the network call that could have recovered -- the cache's own
        # SQL has no upper time bound, so a future-dated row reaches here.
        if is_servable_as_current(cached_state):
            result = live_cache_result(cached_live, address)
            result["source"] = "live_cache_refresh"
            result["refreshed_stale_record_observed_at"] = state["observed_at"]
            return result

    try:
        report = request_live_rugcheck(address)
    except Exception:
        report = None
    if report is None:
        return None

    live = score_live_rugcheck_report(report)
    try:
        insert_live_cache(address, live, report)
    except Exception:
        pass
    live.update(
        {
            "ok": True,
            "address": address,
            "chain": "solana",
            "source": "live_rugcheck_refresh",
            # The same upstream report must not mean different things depending
            # on whether an old collector row happened to exist. This path gets
            # the identical retrieval-only contract as a direct live fetch.
            **retrieval_times(),
            # The replaced row's own observation time, kept as history rather
            # than folded into this answer's.
            "refreshed_stale_record_observed_at": state["observed_at"],
        }
    )
    return with_identity(live, report)


def cache_miss_response(address: str, source: str = "cache_miss") -> dict[str, Any]:
    return with_identity({
        "ok": True,
        "address": address,
        "chain": "solana",
        "risk_score": None,
        "label": "UNKNOWN",
        "risk_flags": ["not_in_intelligence_db"],
        "source": source,
        "scanned_at": None,
        "observed_at": None,
        "fetched_at": now_utc().isoformat(),
        "data_freshness": "UNDATED",
        "age_seconds": None,
        "note": (
            "No verified score is available. Treat this token as unverified."
        ),
    })


def live_cache_result(row: dict[str, Any], address: str) -> dict[str, Any]:
    """A stored live verdict, aged the same way a collector row is.

    The SQL that selects these rows has a lower age bound but no upper one, so
    a row written with a future timestamp would pass the query. Assessing the
    row here as well means one place decides what "current" means, rather than
    the answer depending on which path a request happened to take.
    """
    flags = row.get("risk_flags") or []
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except json.JSONDecodeError:
            flags = []
    risk_value = row.get("risk_score")
    # `created_at` is when we wrote this row, not when the upstream observed
    # anything. The TTL is still enforced against it -- retrieval age is a real
    # and useful bound -- but it is reported as retrieval time, and observed_at
    # stays null, because the report it came from never carried one.
    state = assess(row.get("created_at"), max_age=LIVE_CACHE_MAX_AGE)
    retrieved_at = state["observed_at"]
    result = with_identity({
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
        "scanned_at": None,
        "observed_at": None,
        "retrieved_at": retrieved_at,
        "fetched_at": now_utc().isoformat(),
        "data_freshness": state["freshness"],
        "retrieval_age_seconds": state["age_seconds"],
        "age_seconds": None,
        "observation_coverage": "RETRIEVAL_TIME_ONLY",
        "observation_coverage_note": (
            "Cached from an upstream report that carries no observation "
            "timestamp. retrieval_age_seconds is the age of our copy, not of "
            "the evidence."
        ),
    })
    if is_servable_as_current(state):
        return result
    return with_identity(withhold_verdict(result, state))


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
        return jsonify({
            "ok": True,
            "database": "connected",
            "chain": "solana",
            **build_identity(SCORING_VERSION),
        })
    except Exception:
        return jsonify({
            "ok": False,
            "database": "unavailable",
            "chain": "solana",
            **build_identity(SCORING_VERSION),
        }), 503


@app.get("/score")
def score():
    address = str(request.args.get("address") or "").strip()
    if not is_valid_solana_mint(address):
        return jsonify(with_identity({"ok": False, "error": "invalid solana mint address"})), 400

    try:
        row = fetch_latest_scan(address)
    except Exception:
        return (
            jsonify(
                with_identity({
                    "ok": False,
                    "error": "intelligence db unavailable",
                    "source": "db_error",
                })
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
        _live_observed = now_utc().isoformat()
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
                **retrieval_times(),
                **build_identity(SCORING_VERSION),
            }
        )
        result["evidence"] = build_evidence(result, report)
        return jsonify(result)

    # A stored risk_percent was produced by whichever rules wrote the row, and
    # today's collector records carry no version at all -- fifty keys in a live
    # sample, not one of them naming a scorer. Serving that number under the
    # running scoring_version would claim a provenance we cannot support.
    #
    # Three options in order, per the agreed acceptance: recompute from the
    # record's own raw signals if they are adequate; otherwise refresh; and only
    # if that fails, withhold. Recomputation is preferred because the evidence is
    # already in hand and it costs no upstream call.
    #
    # A stored *label* inherits exactly as a stored number does -- with no
    # precomputed score and no usable evidence, `derive_score` maps the label
    # the previous run wrote -- so the ladder turns on whether the verdict can be
    # derived here, not on which field the old verdict happened to sit in.
    record = _record_of(row)
    source_version = stored_scoring_version(record)
    provenance_known = source_version == SCORING_VERSION

    if provenance_known:
        result = score_scan_row(row)
        verdict_provenance = "current_rules"
    elif has_recomputable_evidence(record):
        result = score_scan_row(row, trust_stored_score=False)
        verdict_provenance = "recomputed_from_raw_evidence"
    else:
        result = score_scan_row(row)
        verdict_provenance = "inherited_unknown_version"

    # Evidence age and verdict provenance are separate axes and a row can fail
    # either. Overwriting the freshness state with a provenance verdict lost the
    # first: an undated row with an unversioned score is both, and a reader
    # needs both reasons.
    state = assess(row.get("created_at"))
    inherited_unknown = verdict_provenance == "inherited_unknown_version"
    result.update(
        {
            "ok": True,
            "address": row.get("contract_address") or address,
            "chain": "solana",
            "source": "postgres_cache",
            # Kept for existing integrators; observed_at is the precise name.
            "scanned_at": state["observed_at"],
            "observed_at": state["observed_at"],
            "fetched_at": now_utc().isoformat(),
            "data_freshness": state["freshness"],
            "age_seconds": state["age_seconds"],
            "verdict_provenance": verdict_provenance,
            "source_scoring_version": source_version,
            **build_identity(SCORING_VERSION),
        }
    )

    if is_servable_as_current(state) and not inherited_unknown:
        # Through with_identity like every other exit, so no path can quietly
        # skip the evidence split by building its response inline.
        return jsonify(with_identity(result))

    if inherited_unknown and is_servable_as_current(state):
        # The evidence is current; only the number's provenance is not. Say so
        # rather than reporting a staleness that is not the problem.
        state = {
            **state,
            "reason": (
                "stored score carries no scoring version and the record has no "
                "raw evidence to recompute from"
            ),
        }

    # The observation is too old, undated or impossible. Replace it with a
    # current reading if one can be had, and prefer an already-cached live
    # result over another upstream call: the stale collector row does not go
    # away, so without this every subsequent request re-fetches the same token
    # and an upstream outage returns UNKNOWN while valid recent evidence sits
    # in the cache unread.
    refreshed = refresh_stale_record(address, state)
    if refreshed is not None:
        return jsonify(refreshed)

    return jsonify(with_identity(withhold_verdict(result, state)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
