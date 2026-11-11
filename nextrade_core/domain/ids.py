"""Deterministic client order IDs: the idempotency key for every order.

The same (run, opportunity, leg) always produces the same ID, so a retry after a
timeout can never create a second order: the venue either already has this ID
or it does not. Binance accepts up to 36 characters matching ^[.A-Z:/a-z0-9_-]{1,36}$.
"""

from __future__ import annotations

import hashlib
import re

CLIENT_ORDER_ID_RE = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")
PREFIX = "nt-"


def make_client_order_id(run_id: str, opportunity_id: str, leg: str) -> str:
    if not run_id or not opportunity_id or not leg:
        raise ValueError("run_id, opportunity_id and leg are all required")
    digest = hashlib.sha256(f"{run_id}|{opportunity_id}|{leg}".encode()).hexdigest()
    cid = f"{PREFIX}{digest[:32]}"  # 3 + 32 = 35 characters
    assert CLIENT_ORDER_ID_RE.match(cid)
    return cid
