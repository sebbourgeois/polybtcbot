"""Risk management — position sizing, loss limits, hedge triggers."""

from __future__ import annotations

import logging
import time

from .config import CONFIG
from .feeds.polymarket_ws import PolymarketFeed
from .models import OpenPosition, Signal

log = logging.getLogger(__name__)

# After hitting the consecutive loss limit, pause for this many seconds
# before allowing trades again (default: 15 minutes).
LOSS_COOLDOWN_SEC = 15 * 60


class RiskManager:
    """Enforces all safety limits and computes position sizing."""

    def __init__(self) -> None:
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.open_positions: list[OpenPosition] = []
        self._loss_cooldown_until: float = 0.0

    def reset_daily(self) -> None:
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self._loss_cooldown_until = 0.0

    def record_win(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.consecutive_losses = 0
        self._loss_cooldown_until = 0.0

    def record_loss(self, pnl: float) -> None:
        self.daily_pnl += pnl
        self.consecutive_losses += 1
        if self.consecutive_losses >= CONFIG.max_consecutive_losses:
            self._loss_cooldown_until = time.time() + LOSS_COOLDOWN_SEC
            log.warning(
                "%d consecutive losses — pausing for %dm",
                self.consecutive_losses,
                LOSS_COOLDOWN_SEC // 60,
            )

    def record_hedge(self, pnl: float) -> None:
        self.daily_pnl += pnl

    def can_trade(self, signal: Signal) -> bool:
        """Pre-trade risk check. Returns False if any limit is breached."""
        if self.open_positions:
            log.debug("Already have an open position")
            return False

        if self.daily_pnl <= -CONFIG.max_daily_loss_usd:
            log.warning("Daily loss limit hit: $%.2f", self.daily_pnl)
            return False

        if self._loss_cooldown_until > time.time():
            remaining = int(self._loss_cooldown_until - time.time())
            log.debug("Loss cooldown active: %ds remaining", remaining)
            return False

        if self.consecutive_losses >= CONFIG.max_consecutive_losses:
            # Cooldown has expired — reset and allow trading again
            log.info("Loss cooldown expired — resuming trading")
            self.consecutive_losses = 0
            self._loss_cooldown_until = 0.0

        if signal.poly_implied_prob > CONFIG.max_price_to_pay:
            log.debug(
                "Price too high: %.3f > %.3f",
                signal.poly_implied_prob,
                CONFIG.max_price_to_pay,
            )
            return False

        if signal.edge < CONFIG.min_edge:
            log.debug("Edge too small: %.3f < %.3f", signal.edge, CONFIG.min_edge)
            return False

        return True

    def calc_position_size(self, signal: Signal) -> float:
        """Kelly-criterion-inspired sizing, clamped to config limits.

        For binary markets at near-even odds:
            f* = (p * b - q) / b
        where b = payout odds = (1/price - 1), p = fair_prob, q = 1 - p.
        """
        price = signal.poly_implied_prob
        if price <= 0.01 or price >= 0.99:
            return CONFIG.min_position_usd

        b = (1.0 / price) - 1.0  # payout odds
        p = signal.fair_prob
        q = 1.0 - p

        kelly = (p * b - q) / b if b > 0 else 0
        kelly = max(0.0, min(kelly, 0.25))  # cap at quarter-Kelly

        size = CONFIG.bankroll * kelly
        return max(CONFIG.min_position_usd, min(size, CONFIG.max_position_usd))

    def should_hedge(
        self,
        position: OpenPosition,
        btc_price: float,
        poly_feed: PolymarketFeed,
    ) -> bool:
        """Check whether an open position should be hedged.

        Hedge when our token has dropped significantly from entry price,
        signalling the bet is likely wrong. Hedging (buying the opposite
        side) caps the loss instead of risking 100%.
        """
        if position.market.seconds_remaining < 30:
            return False  # Too close to resolution, let it ride

        our_price = poly_feed.get_price(position.token_id)
        if our_price is None:
            return False

        drop = position.fill_price - our_price
        if drop > CONFIG.hedge_trigger_threshold:
            log.info(
                "Hedge triggered: entry=%.3f now=%.3f drop=%.3f",
                position.fill_price,
                our_price,
                drop,
            )
            return True
        return False
