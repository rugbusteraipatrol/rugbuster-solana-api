import json
import os
import re
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import psycopg2
from flask import Flask, jsonify, make_response, request, send_from_directory
from psycopg2.extras import Json, RealDictCursor

from build_identity import build_identity
from evidence import build_evidence
from plain_language import describe
from freshness import (
    LIVE_CACHE_MAX_AGE,
    assess,
    is_servable_as_current,
    now_utc,
    withhold_verdict,
)
import chain_checks
import creator_position as creator_position_module
import x402_solana
import launch_snipe as launch_snipe_module
from scoring import (
    SCORING_VERSION,
    apply_deployer_history,
    deployer_history_from_record,
    has_recomputable_evidence,
    label_for_risk,
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
                    ALTER TABLE solana_live_cache
                      ADD COLUMN IF NOT EXISTS creator_position JSONB;
                    ALTER TABLE solana_live_cache
                      ADD COLUMN IF NOT EXISTS launch_snipe JSONB;
                    """
                )
        _schema_ready = True


def fetch_live_cache(address: str) -> dict[str, Any] | None:
    with db_connect() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT contract_address, risk_score, label, rugcheck_score,
                       risk_flags, token_name, token_symbol, created_at, creator_position, launch_snipe
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
                    risk_flags, raw_response, token_name, token_symbol, scoring_version,
                    creator_position, launch_snipe
                ) VALUES (%s, 'live_rugcheck', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    Json(result["creator_position"]) if isinstance(result.get("creator_position"), dict) else None,
                    Json(result["launch_snipe"]) if isinstance(result.get("launch_snipe"), dict) else None,
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


def attach_chain_checks(payload: dict[str, Any]) -> None:
    """Contract, holders and funding read from chain (informational)."""
    if "label" not in payload or "chain_checks" in payload:
        return
    address = payload.get("address")
    if not isinstance(address, str) or not is_valid_solana_mint(address):
        return
    position = payload.get("creator_position") or {}
    creator = position.get("creator") or (payload.get("deployer_history") or {}).get("creator")
    created_at = (payload.get("launch_snipe") or {}).get("creation_time") or position.get("first_seen_at")
    try:
        checks = chain_checks.run(address, creator, created_at)
    except Exception:  # noqa: BLE001 -- informational; never fail the answer
        checks = None
    if checks is not None:
        payload["chain_checks"] = checks


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
    attach_chain_checks(enriched)
    if "label" in enriched:
        enriched["evidence"] = build_evidence(enriched, report)
        # A reader who stops at the top of the response should still be told
        # which kind of answer this is: something found, or something we could
        # not check. Restates the evidence block; never changes it.
        enriched.update(describe(enriched))
    return enriched


def history_block(history: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """The deployer history as it travels in a response: what the collector
    recorded, and when. A rug that happened does not un-happen when the row
    holding it ages, so the history is carried across a refresh; the date it
    was recorded is kept beside it rather than hidden."""
    return {**history, "source": "collector_record", "observed_at": state.get("observed_at")}


def with_deployer_history(
    result: dict[str, Any], history: dict[str, Any] | None, state: dict[str, Any]
) -> dict[str, Any]:
    """Apply a stored row's deployer history to an already-scored live answer.

    Used where the answer was scored without the history in hand -- a cached
    live row written on a direct fetch. The floors are the same ones the
    network path applies inside `score_live_rugcheck_report`, so the two
    refresh branches cannot disagree about the same deployer.
    """
    if not history or result.get("risk_score") is None:
        return result
    risk, flags = apply_deployer_history(int(result["risk_score"]), list(result.get("risk_flags") or []), history)
    updated = {
        **result,
        "risk_score": risk,
        "label": "DANGER" if result.get("label") == "DANGER" else label_for_risk(risk),
        "risk_flags": sorted(set(flags)),
        "deployer_history": history_block(history, state),
    }
    return with_identity(updated)


def refresh_stale_record(
    address: str, state: dict[str, Any], history: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Get a current reading for a token whose stored evidence is not current.

    Checks the live cache before the network. Returns None when neither can
    supply one, leaving the caller to withhold the verdict. `history` is what
    the stale row recorded about the creator; it is applied on both branches.
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
            return with_deployer_history(result, history, state)

    try:
        report = request_live_rugcheck(address)
    except Exception:
        report = None
    if report is None:
        return None

    position = creator_position_module.from_report(report)
    snipe = launch_snipe_module.from_report(report)
    live = score_live_rugcheck_report(report, history=history, position=position, snipe=snipe)
    live["creator_position"] = position
    live["launch_snipe"] = snipe
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
    if history:
        live["deployer_history"] = history_block(history, state)
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


def _json_column(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


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
    position = _json_column(row.get("creator_position"))
    snipe = _json_column(row.get("launch_snipe"))
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
        # The position stored with the row, placed before the evidence split
        # is built so the split can see it.
        **({"creator_position": position} if isinstance(position, dict) else {}),
        **({"launch_snipe": snipe} if isinstance(snipe, dict) else {}),
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
                **(
                    {"paid_score": f"{x402_solana.PAID_PATH}?address=<solana_mint>"}
                    if X402_ACTIVE
                    else {}
                ),
            },
            "x402": X402_SETTINGS.public_status() if X402_ACTIVE else {"enabled": False},
        }
    )


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "").strip()
IPFS_GATEWAYS = (
    "https://gateway.pinata.cloud/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
)
_token_meta_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_token_meta_lock = threading.Lock()
TOKEN_META_TTL_SECONDS = 6 * 3600


def image_candidates(url: str | None) -> list[str]:
    """Where a browser can try to load a token image, best first.

    Metadata images mostly live on IPFS, and any one public gateway is often
    slow or rate-limited, so the same CID is offered through several.
    """
    if not url or not isinstance(url, str):
        return []
    url = url.strip()
    cid_path = None
    if url.startswith("ipfs://"):
        cid_path = url[len("ipfs://"):].lstrip("/")
        if cid_path.startswith("ipfs/"):
            cid_path = cid_path[len("ipfs/"):]
    elif "/ipfs/" in url:
        cid_path = url.split("/ipfs/", 1)[1]
    if cid_path:
        return [gateway + cid_path for gateway in IPFS_GATEWAYS]
    return [url] if url.startswith("https://") else []


def fetch_token_meta(address: str) -> dict[str, Any]:
    """Name, symbol and image from the chain's metadata (Helius DAS getAsset)."""
    now = time.time()
    with _token_meta_lock:
        hit = _token_meta_cache.get(address)
        if hit and now - hit[0] < TOKEN_META_TTL_SECONDS:
            return hit[1]
    meta: dict[str, Any] = {"address": address, "image": None, "image_candidates": []}
    if SOLANA_RPC_URL:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": "meta", "method": "getAsset", "params": {"id": address}}
        ).encode()
        req = Request(SOLANA_RPC_URL, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=6) as response:
                asset = (json.load(response) or {}).get("result") or {}
            content = asset.get("content") or {}
            links = content.get("links") or {}
            files = content.get("files") or []
            image = links.get("image") or (files[0].get("uri") if files else None)
            info = content.get("metadata") or {}
            meta.update(
                {
                    "name": info.get("name"),
                    "symbol": info.get("symbol"),
                    "image": image,
                    "image_candidates": image_candidates(image),
                }
            )
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            pass
    with _token_meta_lock:
        _token_meta_cache[address] = (now, meta)
        if len(_token_meta_cache) > 2000:
            _token_meta_cache.pop(next(iter(_token_meta_cache)))
    return meta


@app.get("/token-meta")
def token_meta():
    address = str(request.args.get("address") or "").strip()
    if not is_valid_solana_mint(address):
        return jsonify({"ok": False, "error": "invalid solana mint address"}), 400
    response = jsonify({"ok": True, **fetch_token_meta(address)})
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/shield")
def shield_page():
    """Creator Trace: the browser front end for /score and /x402/score."""
    return send_from_directory(WEB_DIR, "shield.html")


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

        position = creator_position_module.from_report(report)
        snipe = launch_snipe_module.from_report(report)
        result = score_live_rugcheck_report(report, position=position, snipe=snipe)
        result["creator_position"] = position
        result["launch_snipe"] = snipe
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
        attach_chain_checks(result)
        result["evidence"] = build_evidence(result, report)
        result.update(describe(result))
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
    # What the row recorded about the creator. `derive_score` already applied
    # its floors to the stored verdict above; carrying the block here lets the
    # evidence split report the history instead of calling it uncollected, and
    # lets a refresh below keep it when the rest of the row is too old to serve.
    history = deployer_history_from_record(record)
    if history:
        result["deployer_history"] = history_block(history, state)
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
    refreshed = refresh_stale_record(address, state, history)
    if refreshed is not None:
        return jsonify(refreshed)

    return jsonify(with_identity(withhold_verdict(result, state)))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


# --- x402 pay-per-scan on Solana -------------------------------------------
#
# Same answer as /score, sold per request in USDC on Solana. The free route is
# untouched. A misconfigured deploy must not take the free API down with it,
# so a settings error disables only the paid route and says why in the log.

X402_SETTINGS = x402_solana.X402Settings.from_env()
X402_ACTIVE = False
_settlements_ready = False
_settlements_lock = threading.Lock()


def record_settlement(record: dict[str, Any]) -> None:
    """Append to the shared x402 ledger that the Base service already writes.

    One table for every chain: `network` tells Solana from Base, and `tier`
    names the product that was sold.
    """
    global _settlements_ready
    with db_connect() as connection:
        with connection.cursor() as cursor:
            if not _settlements_ready:
                with _settlements_lock:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS x402_settlements (
                            id BIGSERIAL PRIMARY KEY,
                            settled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                            network TEXT,
                            payer TEXT,
                            pay_to TEXT,
                            asset TEXT,
                            amount_atomic NUMERIC,
                            tier TEXT,
                            route TEXT,
                            transaction TEXT
                        )
                        """
                    )
                    _settlements_ready = True
            cursor.execute(
                """
                INSERT INTO x402_settlements
                    (settled_at, network, payer, pay_to, asset, amount_atomic, tier, route, transaction)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record["recorded_at"],
                    record.get("network"),
                    record.get("payer"),
                    record.get("pay_to"),
                    record.get("asset"),
                    None if record.get("amount") is None else str(record["amount"]),
                    x402_solana.LEDGER_TIER,
                    record.get("route"),
                    record.get("transaction"),
                ),
            )
        connection.commit()


@app.get(x402_solana.PAID_PATH)
def paid_score():
    if not X402_ACTIVE:
        return jsonify({"ok": False, "error": "not found"}), 404
    response = make_response(score())
    if response.status_code >= 400:
        return response
    payload = response.get_json(silent=True) or {}
    if x402_solana.is_chargeable(payload):
        return response
    # No verdict to sell. A 5xx keeps the middleware from settling the payment.
    payload["x402"] = {
        "charged": False,
        "reason": "No verdict was available for this token, so the payment was not settled.",
    }
    return jsonify(payload), 503


if X402_SETTINGS.enabled:
    try:
        x402_solana.install(app, X402_SETTINGS, record=record_settlement)
        X402_ACTIVE = True
    except Exception as exc:  # noqa: BLE001
        print(f"x402 paid route disabled: {exc}", file=sys.stderr)
