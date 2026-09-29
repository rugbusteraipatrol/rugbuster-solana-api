"""Pay-per-scan on Solana through x402.

`/score` stays free and unchanged. `/x402/score` returns the same answer, but
only after a USDC payment on Solana settles through the Coinbase CDP
facilitator, which also pays the network fee, so a paying agent needs USDC and
no SOL.

Nothing here runs unless RUGBUSTER_X402_ENABLED is set: a deploy without the
variables serves exactly what it served before, and the paid route answers 404
like any other unknown path.

A caller is charged only for a verdict. The middleware settles only responses
below 400, so the paid view turns "no verdict" (unknown token, withheld stale
reading, database down) into a 503 that says it was not charged.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
PAID_PATH = "/x402/score"
PAID_ROUTE = f"GET {PAID_PATH}"
CHARGEABLE_LABELS = frozenset({"DANGER", "WARN", "GOOD"})
LEDGER_TIER = "solana-score"
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_PRICE = re.compile(r"\$\d+(?:\.\d{1,6})?")


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class X402Settings:
    enabled: bool = False
    network: str = SOLANA_MAINNET
    price: str = "$0.01"
    pay_to: str = ""
    resource_url: str = ""
    facilitator_url: str = CDP_FACILITATOR_URL
    cdp_api_key_id: str = ""
    cdp_api_key_secret: str = ""
    rpc_url: str = ""

    @classmethod
    def from_env(cls) -> "X402Settings":
        return cls(
            enabled=_enabled(os.getenv("RUGBUSTER_X402_ENABLED")),
            network=os.getenv("RUGBUSTER_X402_NETWORK", SOLANA_MAINNET).strip(),
            price=os.getenv("RUGBUSTER_X402_PRICE", "$0.01").strip(),
            pay_to=os.getenv("RUGBUSTER_X402_PAY_TO", "").strip(),
            resource_url=os.getenv("RUGBUSTER_X402_RESOURCE_URL", "").strip().rstrip("/"),
            facilitator_url=os.getenv(
                "RUGBUSTER_X402_FACILITATOR_URL", CDP_FACILITATOR_URL
            ).strip().rstrip("/"),
            cdp_api_key_id=os.getenv("CDP_API_KEY_ID", "").strip(),
            cdp_api_key_secret=os.getenv("CDP_API_KEY_SECRET", ""),
            rpc_url=os.getenv("SOLANA_RPC_URL", "").strip(),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if self.network not in {SOLANA_MAINNET, SOLANA_DEVNET}:
            raise ValueError("RUGBUSTER_X402_NETWORK must be Solana mainnet or devnet (CAIP-2)")
        if not _SOLANA_ADDRESS.fullmatch(self.pay_to):
            raise ValueError("RUGBUSTER_X402_PAY_TO must be a Solana address")
        if not _PRICE.fullmatch(self.price):
            raise ValueError("RUGBUSTER_X402_PRICE must look like $0.01")
        parsed = urlsplit(self.resource_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("RUGBUSTER_X402_RESOURCE_URL must be a public HTTPS URL")
        if not self.cdp_api_key_id or not self.cdp_api_key_secret:
            raise ValueError("CDP_API_KEY_ID and CDP_API_KEY_SECRET are required")

    def public_status(self) -> dict[str, Any]:
        """What the service info endpoint may say about the paid route."""
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "x402_version": 2,
            "route": PAID_ROUTE,
            "network": self.network,
            "asset": "USDC",
            "price": self.price,
            "pay_to": self.pay_to,
            "charged_only_for": sorted(CHARGEABLE_LABELS),
        }


def is_chargeable(payload: dict[str, Any]) -> bool:
    """A response is worth a payment only when it carries a verdict."""
    return bool(payload.get("ok")) and payload.get("label") in CHARGEABLE_LABELS


def settlement_record(context: Any) -> dict[str, Any]:
    result = context.result
    requirements = context.requirements
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "network": getattr(result, "network", None) or getattr(requirements, "network", None),
        "transaction": getattr(result, "transaction", None),
        "payer": getattr(result, "payer", None),
        "amount": getattr(result, "amount", None) or getattr(requirements, "amount", None),
        "asset": getattr(requirements, "asset", None),
        "pay_to": getattr(requirements, "pay_to", None),
        "route": PAID_ROUTE,
    }


class _CdpAuthProvider:
    """Adapt Coinbase's CDP x402 auth headers to the x402 SDK protocol."""

    def __init__(self, settings: X402Settings) -> None:
        from cdp.x402.x402 import create_cdp_auth_headers

        self._create_headers = create_cdp_auth_headers(
            settings.cdp_api_key_id, settings.cdp_api_key_secret
        )

    def get_auth_headers(self):
        from x402.http import AuthHeaders

        headers = self._create_headers()
        return AuthHeaders(
            verify=headers["verify"],
            settle=headers["settle"],
            supported=headers["supported"],
            bazaar=headers["list"],
        )


def build_server(settings: X402Settings, record: Callable[[dict[str, Any]], None] | None = None):
    """Resource server bound to the CDP facilitator for Solana `exact` payments.

    The facilitator's /supported answer carries the fee payer the client must
    name in its transaction, so it is fetched from the facilitator on the first
    paid request rather than hard-coded here.
    """
    from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
    from x402.mechanisms.svm.exact import ExactSvmServerScheme
    from x402.server import x402ResourceServerSync

    facilitator = HTTPFacilitatorClientSync(
        FacilitatorConfig(
            url=settings.facilitator_url,
            auth_provider=_CdpAuthProvider(settings),
            identifier="coinbase-cdp",
        )
    )
    server = x402ResourceServerSync(facilitator)
    # With an RPC the challenge carries a fresh blockhash, so a browser wallet
    # can build the payment without a Solana RPC of its own.
    server.register(settings.network, ExactSvmServerScheme(rpc_url=settings.rpc_url or None))

    if record is not None:
        def _after_settle(context: Any) -> None:
            # The payment already settled; failing to write the ledger must not
            # turn a paid answer into an error for the caller.
            try:
                record(settlement_record(context))
            except Exception as exc:  # noqa: BLE001
                print(f"x402 settlement not recorded: {exc!r}", file=sys.stderr)

        server.on_after_settle(_after_settle)
    return server


# Browsers need to read the challenge and receipt headers and to send the
# payment header, which makes the paid request a preflighted CORS request.
CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, OPTIONS"),
    ("Access-Control-Allow-Headers", "PAYMENT-SIGNATURE, X-PAYMENT, Content-Type"),
    (
        "Access-Control-Expose-Headers",
        "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE",
    ),
]


class CorsMiddleware:
    """Outermost WSGI layer, so the 402 written by the payment middleware
    carries CORS headers too; Flask's after_request never sees that response."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") == "OPTIONS":
            start_response("204 No Content", CORS_HEADERS + [("Access-Control-Max-Age", "600")])
            return [b""]

        def with_cors(status, headers, exc_info=None):
            present = {name.lower() for name, _ in headers}
            headers = list(headers) + [h for h in CORS_HEADERS if h[0].lower() not in present]
            if exc_info is None:
                return start_response(status, headers)
            return start_response(status, headers, exc_info)

        return self.inner(environ, with_cors)


def install(app, settings: X402Settings, *, record=None, server=None):
    """Wrap the Flask app with the payment middleware for the paid route only."""
    settings.validate()
    if not settings.enabled:
        return None

    from x402.http import PaymentOption
    from x402.http.middleware.flask import PaymentMiddleware
    from x402.http.types import RouteConfig

    if server is None:
        server = build_server(settings, record)
    routes = {
        PAID_ROUTE: RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=settings.pay_to,
                price=settings.price,
                network=settings.network,
            ),
            resource=f"{settings.resource_url}{PAID_PATH}",
            mime_type="application/json",
            description=(
                "Solana token risk verdict before signing: what the creator did with "
                "their own supply, bot wallets in the first five seconds, the "
                "creator's earlier tokens, and an explicit list of what was not "
                "checked. Charged only when a verdict is returned"
            ),
            service_name="RugBuster Solana",
            tags=["security", "solana", "token-risk", "pre-sign"],
        )
    }
    middleware = PaymentMiddleware(app, routes, server)
    app.wsgi_app = CorsMiddleware(app.wsgi_app)
    return middleware
