"""Chainlink BTC/USD on-chain price feed (Polygon) — polls the aggregator."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

log = logging.getLogger(__name__)

# Chainlink BTC/USD aggregator on Polygon mainnet
_AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "type": "function",
    }
]

_POLL_INTERVAL = 5.0  # seconds


class ChainlinkFeed:
    """Polls the Chainlink BTC/USD aggregator on Polygon."""

    def __init__(
        self,
        on_price: Callable[[float, float], Awaitable[None]] | None = None,
        rpc_url: str = "https://polygon-bor-rpc.publicnode.com",
    ) -> None:
        self._on_price = on_price
        self._stop = asyncio.Event()
        self.latest_price: float = 0.0
        self.latest_ts: float = 0.0
        self._last_round_id: int = 0

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._contract = w3.eth.contract(address=_AGGREGATOR, abi=_ABI)

    async def run(self) -> None:
        log.info("Chainlink feed starting (poll every %.0fs)", _POLL_INTERVAL)
        while not self._stop.is_set():
            try:
                data = await asyncio.to_thread(
                    self._contract.functions.latestRoundData().call
                )
                round_id, answer, _, updated_at, _ = data
                price = answer / 1e8
                ts = float(updated_at)

                if price > 0 and round_id != self._last_round_id:
                    self._last_round_id = round_id
                    self.latest_price = price
                    self.latest_ts = ts
                    if self._on_price:
                        await self._on_price(price, ts)
                elif price > 0:
                    # Same round but keep latest_price fresh
                    self.latest_price = price
                    self.latest_ts = ts

            except Exception:
                log.debug("Chainlink poll error", exc_info=True)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_POLL_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

        log.info("Chainlink feed stopped")

    async def stop(self) -> None:
        self._stop.set()
