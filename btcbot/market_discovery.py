"""Discover active 5-minute BTC Up/Down markets via Gamma API."""

from __future__ import annotations

import logging
import time

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import CONFIG
from .models import Market

log = logging.getLogger(__name__)

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=4.0),
    retry=retry_if_exception_type(
        (httpx.TransportError, httpx.HTTPStatusError, httpx.ReadTimeout)
    ),
    reraise=True,
)


def _window_end_ts(now: float | None = None) -> int:
    """Compute the end timestamp of the current 5-minute window.

    Windows are aligned to Unix epoch modulo 300.
    """
    t = int(now or time.time())
    return t - (t % 300) + 300


def _parse_market(event: dict, window_end_ts: int) -> Market | None:
    """Parse a Gamma event response into a Market."""
    markets = event.get("markets")
    if not markets:
        return None
    m = markets[0]

    outcomes = m.get("outcomes")
    token_ids = m.get("clobTokenIds")

    # Handle JSON-encoded strings (Gamma sometimes does this)
    if isinstance(outcomes, str):
        import json
        outcomes = json.loads(outcomes)
    if isinstance(token_ids, str):
        import json
        token_ids = json.loads(token_ids)

    if not outcomes or not token_ids or len(outcomes) != len(token_ids):
        log.warning("Malformed market data for %s", event.get("slug"))
        return None

    # Map outcomes to token IDs
    try:
        up_idx = outcomes.index("Up")
        down_idx = outcomes.index("Down")
    except ValueError:
        log.warning("Expected 'Up'/'Down' outcomes, got %s", outcomes)
        return None

    slug = event.get("slug", f"btc-updown-5m-{window_end_ts}")
    condition_id = m.get("conditionId") or m.get("condition_id", "")

    return Market(
        slug=slug,
        condition_id=condition_id,
        up_token_id=token_ids[up_idx],
        down_token_id=token_ids[down_idx],
        start_ts=window_end_ts - 300,
        end_ts=window_end_ts,
    )


@retry(**_RETRY)
async def _fetch_event(client: httpx.AsyncClient, slug: str) -> dict | None:
    resp = await client.get(
        f"{CONFIG.gamma_api_base}/events",
        params={"slug": slug},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        return data[0]
    return None


async def discover_active_market(
    client: httpx.AsyncClient | None = None,
) -> Market | None:
    """Find the currently active 5-minute BTC market.

    Tries the current window first, then the next window (in case the
    current one is about to close and the next is already available).
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            headers={"User-Agent": "btcbot/0.1"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    try:
        now = time.time()
        current_end = _window_end_ts(now)
        # Try current window, then next
        for end_ts in [current_end, current_end + 300]:
            slug = f"btc-updown-5m-{end_ts}"
            try:
                event = await _fetch_event(client, slug)
                if event:
                    market = _parse_market(event, end_ts)
                    if market:
                        return market
            except Exception:
                log.debug("Failed to fetch market %s", slug, exc_info=True)
        return None
    finally:
        if owns_client:
            await client.aclose()
