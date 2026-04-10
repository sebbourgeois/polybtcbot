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

    def sync_streak(self, streak: int) -> None:
        """Set consecutive losses from a DB-computed streak (chronological order).

        This is immune to async resolution races — the DB query orders by
        trade time, not resolution time.
        """
        self.consecutive_losses = streak
        if streak >= CONFIG.max_consecutive_losses:
            if self._loss_cooldown_until <= time.time():
                self._loss_cooldown_until = time.time() + LOSS_COOLDOWN_SEC
                log.warning(
                    "%d consecutive losses — pausing for %dm",
                    streak,
                    LOSS_COOLDOWN_SEC // 60,
                )
        elif streak == 0:
            self._loss_cooldown_until = 0.0

    def record_hedge(self, pnl: float) -> None:
        self.daily_pnl += pnl

    def can_trade(self, signal: Signal, choppiness: float = 0.0) -> bool:
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

        if signal.poly_implied_prob < CONFIG.min_price_to_pay:
            log.debug(
                "Price too low: %.3f < %.3f",
                signal.poly_implied_prob,
                CONFIG.min_price_to_pay,
            )
            return False

        # Dynamic min_edge: require more edge in choppy markets
        effective_min_edge = CONFIG.min_edge + choppiness * 0.05
        if signal.edge < effective_min_edge:
            log.debug(
                "Edge too small: %.3f < %.3f (chop=%.2f)",
                signal.edge, effective_min_edge, choppiness,
            )
            return False

        return True

    def calc_position_size(self, signal: Signal, choppiness: float = 0.0) -> float:
        """Kelly-criterion-inspired sizing, clamped to config limits.

        For binary markets at near-even odds:
            f* = (p * b - q) / b
        where b = payout odds = (1/price - 1), p = fair_prob, q = 1 - p.

        Position is scaled down in choppy (mean-reverting) markets.
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
        size = min(size, CONFIG.max_position_usd)

        # Apply regime scaling AFTER clamping so it always reduces in choppy markets
        regime_factor = 1.0 - (choppiness * 0.6)
        size *= regime_factor

        return max(CONFIG.min_position_usd, size)

    def should_hedge(
        self,
        position: OpenPosition,
        btc_price: float,
        poly_feed: PolymarketFeed,
        choppiness: float = 0.0,
        opposite_price: float | None = None,
    ) -> bool:
        """Check whether an open position should be hedged.

        Hedge when our token has dropped significantly from entry price,
        signalling the bet is likely wrong. Hedging (buying the opposite
        side) caps the loss instead of risking 100%.

        Guards prevent hedging when it would destroy value:
        - Too early (price can still recover)
        - Too close to resolution (let it ride)
        - Opposite side too expensive (minimal loss reduction)
        - In choppy markets the threshold is raised (drops reverse often)
        """
        if position.is_hedged:
            return False

        remaining = position.market.seconds_remaining
        if remaining < 30:
            return False  # Too close to resolution, let it ride

        # Don't hedge in the first half of the window — price can recover
        if remaining > 150:
            return False

        our_price = poly_feed.get_price(position.token_id)
        if our_price is None:
            return False

        if position.fill_price <= 0:
            return False

        # Don't hedge when the opposite side is too expensive — the locked-in
        # loss (entry + hedge - 1.0) would be nearly as bad as losing outright
        if opposite_price is not None and opposite_price > 0:
            if opposite_price > 0.85:
                log.debug(
                    "Hedge skipped: opposite price %.3f too expensive",
                    opposite_price,
                )
                return False

        # In choppy markets drops reverse more often — raise the bar
        effective_threshold = max(0.10, CONFIG.hedge_trigger_threshold + (choppiness * 0.05))
        drop_ratio = (position.fill_price - our_price) / position.fill_price
        if drop_ratio > effective_threshold:
            log.info(
                "Hedge triggered: entry=%.3f now=%.3f drop=%.1f%% (threshold=%.1f%%)",
                position.fill_price,
                our_price,
                drop_ratio * 100,
                effective_threshold * 100,
            )
            return True
        return False
