"""One JSON-RPC POST with a short retry when the provider rate-limits.

A burst of requests (a scan reads the creation transaction, the first
buyers and, since chain_checks, the mint and holders) can draw a 429 from
the RPC. Retrying twice after a short pause turns most of those into
answers instead of `unavailable` readings, which on the live path would
change what the scan can say.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

RETRY_DELAYS_SECONDS = (0.4, 1.0)


def rpc_post(url: str, method: str, params: Any, timeout: float) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        request = Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            break
        except HTTPError as exc:
            if exc.code != 429 or attempt == len(RETRY_DELAYS_SECONDS):
                raise
            time.sleep(RETRY_DELAYS_SECONDS[attempt])
    if "error" in payload:
        raise RuntimeError(str(payload["error"])[:120])
    return payload.get("result")
