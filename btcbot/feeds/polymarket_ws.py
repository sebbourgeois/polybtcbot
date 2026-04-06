"""Polymarket CLOB WebSocket feed — live odds for Up/Down tokens."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import websockets

from ..config import CONFIG

log = logging.getLogger(__name__)

PING_INTERVAL_SEC = 10

OnPolyPrice = Callable[[str, float], Awaitable[None]]  # (token_id, price)


class PolymarketFeed:
    """Persistent WebSocket subscription for Polymarket token prices."""

    def __init__(self, on_price: OnPolyPrice | None = None) -> None:
        self._on_price = on_price
        self._desired_tokens: set[str] = set()
        self._current_tokens: set[str] = set()
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._prices: dict[str, float] = {}

    def get_price(self, token_id: str) -> float | None:
        return self._prices.get(token_id)

    async def set_token_ids(self, tokens: set[str]) -> None:
        async with self._lock:
            if tokens != self._desired_tokens:
                self._desired_tokens = tokens
                # Force reconnect to update subscriptions
                if self._ws:
                    await self._ws.close()

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            await self._ws.close()

    async def run(self) -> None:
        """Connect, subscribe, receive price updates, auto-reconnect."""
        backoff = 1.0
        while not self._stop.is_set():
            if not self._desired_tokens:
                await asyncio.sleep(2.0)
                continue
            try:
                log.info("Connecting to Polymarket WS")
                async with websockets.connect(
                    CONFIG.clob_ws_url,
                    ping_interval=None,  # We handle PING manually
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    async with self._lock:
                        self._current_tokens = set(self._desired_tokens)

                    sub_msg = json.dumps({
                        "type": "market",
                        "assets_ids": list(self._current_tokens),
                    })
                    await ws.send(sub_msg)
                    log.info("Polymarket WS subscribed to %d tokens", len(self._current_tokens))

                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            if self._stop.is_set():
                                break
                            await self._handle_message(raw)
                    finally:
                        ping_task.cancel()
            except websockets.ConnectionClosed:
                log.debug("Polymarket WS closed, reconnecting in %.0fs", backoff)
            except Exception:
                log.warning("Polymarket WS error, reconnecting in %.0fs", backoff, exc_info=True)
            self._ws = None
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                await ws.send("PING")
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            pass

    async def _handle_message(self, raw: str | bytes) -> None:
        msg = str(raw)
        if msg == "PONG":
            return
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return

        events = data if isinstance(data, list) else [data]
        for ev in events:
            token_id = ev.get("asset_id") or ev.get("token_id")
            if not token_id:
                continue

            price: float | None = None
            ev_type = ev.get("event_type", "")

            if ev_type in ("book", "price_change"):
                price = self._mid_from_book(ev)
            elif ev_type == "last_trade_price":
                try:
                    price = float(ev["price"])
                except (KeyError, ValueError, TypeError):
                    pass
            elif "price" in ev:
                try:
                    price = float(ev["price"])
                except (ValueError, TypeError):
                    pass

            if price is not None and price > 0:
                self._prices[token_id] = price
                if self._on_price:
                    await self._on_price(token_id, price)

    @staticmethod
    def _mid_from_book(ev: dict) -> float | None:
        bids = ev.get("bids", [])
        asks = ev.get("asks", [])

        best_bid = None
        for b in bids:
            p = float(b.get("price", 0))
            if best_bid is None or p > best_bid:
                best_bid = p

        best_ask = None
        for a in asks:
            p = float(a.get("price", 0))
            if best_ask is None or p < best_ask:
                best_ask = p

        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask
