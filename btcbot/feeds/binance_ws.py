"""Binance BTC/USDT WebSocket feed — sub-second price updates."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Awaitable, Callable

import websockets

from ..config import CONFIG

log = logging.getLogger(__name__)

OnBTCPrice = Callable[[float, float], Awaitable[None]]  # (price, timestamp)


class BinanceFeed:
    """Persistent WebSocket connection to Binance for real-time BTC price."""

    def __init__(self, on_price: OnBTCPrice | None = None) -> None:
        self._on_price = on_price
        self._stop = asyncio.Event()
        self._prices: deque[tuple[float, float]] = deque(maxlen=3000)
        self.latest_price: float = 0.0
        self.latest_ts: float = 0.0

    @property
    def trend(self) -> float:
        """Price change rate in $/second over the last 5 seconds."""
        return self._calc_momentum(5.0)

    def price_at(self, seconds_ago: float) -> float | None:
        """Return the approximate price N seconds ago."""
        cutoff = time.time() - seconds_ago
        for ts, price in reversed(self._prices):
            if ts <= cutoff:
                return price
        return None

    def _calc_momentum(self, seconds: float) -> float:
        now = time.time()
        cutoff = now - seconds
        recent = [(ts, p) for ts, p in self._prices if ts >= cutoff]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        if dt < 0.3:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / dt

    def momentum(self, seconds: float) -> float:
        """Price change rate in $/second over the last N seconds."""
        return self._calc_momentum(seconds)

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect to Binance WS, receive trade stream, auto-reconnect."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                log.info("Connecting to Binance WS: %s", CONFIG.binance_ws_url)
                async with websockets.connect(
                    CONFIG.binance_ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    log.info("Binance WS connected")
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            price = float(data["p"])
                            ts = data["T"] / 1000.0  # ms -> seconds
                            self.latest_price = price
                            self.latest_ts = ts
                            self._prices.append((ts, price))
                            if self._on_price:
                                await self._on_price(price, ts)
                        except (KeyError, ValueError, TypeError):
                            continue
            except websockets.ConnectionClosed:
                log.warning("Binance WS disconnected, reconnecting in %.0fs", backoff)
            except Exception:
                log.warning("Binance WS error, reconnecting in %.0fs", backoff, exc_info=True)
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
