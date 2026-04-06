"""Paper-trading executor — simulates fills using real Polymarket prices."""

from __future__ import annotations

import logging
import time
import uuid

from .config import CONFIG
from .models import Market, OpenPosition, Signal, TradeRecord

log = logging.getLogger(__name__)

# Simulated spread: we assume we'd cross the spread
SIMULATED_SPREAD = 0.02


class PaperExecutor:
    """Drop-in replacement for Executor that logs virtual trades."""

    @property
    def is_ready(self) -> bool:
        return True

    async def place_trade(
        self,
        market: Market,
        signal: Signal,
        amount_usd: float,
    ) -> TradeRecord | None:
        """Simulate a fill at the current market price + half spread."""
        token_id = market.token_id_for(signal.direction)
        fill_price = signal.poly_implied_prob + SIMULATED_SPREAD / 2
        fill_price = min(fill_price, 0.99)
        qty = amount_usd / fill_price if fill_price > 0 else 0

        log.info(
            "[PAPER] %s %s @ $%.3f — $%.2f (edge=%.3f strength=%.3f)",
            signal.direction,
            market.slug,
            fill_price,
            amount_usd,
            signal.edge,
            signal.strength,
        )

        return TradeRecord(
            market_slug=market.slug,
            trade_type="ENTRY",
            direction=signal.direction,
            token_id=token_id,
            side="BUY",
            amount_usd=amount_usd,
            fill_price=fill_price,
            token_quantity=qty,
            signal_strength=signal.strength,
            signal_edge=signal.edge,
            order_id=f"paper-{uuid.uuid4().hex[:8]}",
            is_paper=True,
        )

    async def place_hedge(
        self,
        market: Market,
        position: OpenPosition,
    ) -> TradeRecord | None:
        """Simulate a hedge by buying the opposite side."""
        opposite_dir = "DOWN" if position.direction == "UP" else "UP"
        opposite_token = market.token_id_for(opposite_dir)
        hedge_cost = position.token_quantity * 0.50
        fill_price = 0.50 + SIMULATED_SPREAD / 2
        qty = hedge_cost / fill_price if fill_price > 0 else 0

        log.info(
            "[PAPER] HEDGE %s %s @ $%.3f — $%.2f",
            opposite_dir,
            market.slug,
            fill_price,
            hedge_cost,
        )

        return TradeRecord(
            market_slug=market.slug,
            trade_type="HEDGE",
            direction=opposite_dir,
            token_id=opposite_token,
            side="BUY",
            amount_usd=hedge_cost,
            fill_price=fill_price,
            token_quantity=qty,
            signal_strength=0.0,
            signal_edge=0.0,
            order_id=f"paper-hedge-{uuid.uuid4().hex[:8]}",
            is_paper=True,
        )
