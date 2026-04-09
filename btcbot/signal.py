"""Signal generation — detect mispricing between BTC reality and Polymarket odds."""

from __future__ import annotations

import math
import time
from collections import deque

from .config import CONFIG
from .models import Market, Signal


def _sigmoid(x: float) -> float:
    """Standard sigmoid squashing to (0, 1)."""
    return 1.0 / (1.0 + math.exp(-x))


class SignalGenerator:
    """Evaluates whether the Polymarket odds are mispriced relative to real BTC momentum."""

    def __init__(self) -> None:
        self._btc_prices: deque[tuple[float, float]] = deque(maxlen=3000)
        self._market_start_price: float | None = None
        self._chainlink_price: float = 0.0
        self._chainlink_start_price: float | None = None
        self._market: Market | None = None

    def reset(self, market: Market) -> None:
        """Called when switching to a new 5-minute market."""
        self._btc_prices.clear()
        self._market_start_price = None
        self._chainlink_start_price = None
        self._market = market

    def update_btc_price(self, price: float, ts: float) -> None:
        self._btc_prices.append((ts, price))
        if self._market_start_price is None and self._market:
            if ts >= self._market.start_ts:
                self._market_start_price = price

    def update_chainlink_price(self, price: float, ts: float) -> None:
        self._chainlink_price = price
        if self._chainlink_start_price is None and self._market:
            self._chainlink_start_price = price

    def evaluate(
        self,
        *,
        btc_price: float,
        poly_up_price: float | None,
        poly_down_price: float | None,
        time_remaining_sec: float,
        choppiness: float = 0.0,
    ) -> Signal:
        """Core signal logic.

        Compares our estimated fair probability (based on BTC momentum and
        current delta from the window start) against Polymarket's implied odds.
        Returns a Signal with direction, strength, and edge.
        """
        # Can't trade without Polymarket prices
        if poly_up_price is None or poly_down_price is None:
            return _null_signal("No Polymarket prices")

        # Need both Chainlink and Binance start prices
        if self._chainlink_start_price is None or self._chainlink_price <= 0:
            return _null_signal("No Chainlink price yet")
        if self._market_start_price is None:
            return _null_signal("No market start price yet")

        # 1. BTC delta from market start — use CHAINLINK (matches oracle)
        price_delta = self._chainlink_price - self._chainlink_start_price

        # Skip when BTC has moved too far — model is unreliable on large deltas
        max_delta = CONFIG.btc_5m_volatility * 1.5  # ~$50 at default vol
        if abs(price_delta) > max_delta:
            return _null_signal(
                f"Price delta too large: ${abs(price_delta):.2f} > ${max_delta:.2f}"
            )

        # 2. Momentum over multiple timeframes
        mom_5s = self._calc_momentum(5.0)
        mom_15s = self._calc_momentum(15.0)
        mom_30s = self._calc_momentum(30.0)

        # 3. Estimate our fair probability of "Up"
        fair_prob_up = self._estimate_fair_prob(
            price_delta, mom_5s, mom_15s, time_remaining_sec, choppiness
        )

        # 4. Compare to Polymarket implied probability
        edge_up = fair_prob_up - poly_up_price
        edge_down = (1.0 - fair_prob_up) - poly_down_price

        # 5. Pick the side with the bigger edge
        if edge_up >= edge_down:
            direction = "UP"
            edge = edge_up
            fair_p = fair_prob_up
            poly_p = poly_up_price
        else:
            direction = "DOWN"
            edge = edge_down
            fair_p = 1.0 - fair_prob_up
            poly_p = poly_down_price

        # 6. Signal strength (0..1)
        strength = self._calc_strength(edge, mom_5s, mom_15s, time_remaining_sec, choppiness)

        btc_dir = "above" if price_delta >= 0 else "below"
        reason = (
            f"BTC {btc_dir} start by ${abs(price_delta):.2f}, "
            f"mom5={mom_5s:+.2f}$/s, mom15={mom_15s:+.2f}$/s, "
            f"fair={fair_p:.3f} vs poly={poly_p:.3f}"
        )

        return Signal(
            direction=direction,
            strength=strength,
            edge=edge,
            btc_momentum=mom_5s,
            poly_implied_prob=poly_p,
            fair_prob=fair_p,
            reason=reason,
        )

    def _estimate_fair_prob(
        self,
        price_delta: float,
        mom_5s: float,
        mom_15s: float,
        time_remaining_sec: float,
        choppiness: float = 0.0,
    ) -> float:
        """Estimate fair probability of 'Up' resolving.

        Model: sigmoid of a combined z-score that factors in current delta,
        momentum, and time remaining. More time remaining = more uncertainty
        = pulled toward 0.50.
        """
        vol = CONFIG.btc_5m_volatility
        if vol <= 0:
            vol = 30.0

        # How far above/below start, normalised by expected 5m volatility
        z_delta = price_delta / vol

        # Momentum contribution: extrapolate momentum over remaining time
        # Scale down in choppy markets where momentum is unreliable
        blended_mom = 0.7 * mom_5s + 0.3 * mom_15s
        mom_weight = 1.0 - choppiness  # trending=1.0, choppy=0.0
        momentum_extrapolation = blended_mom * time_remaining_sec * mom_weight
        z_mom = momentum_extrapolation / vol

        # Combined z-score — momentum weight kept very low since delta alone
        # is a better predictor (large-delta momentum trades historically lose)
        combined_z = z_delta + 0.05 * z_mom

        # Time factor: more time remaining → less confidence → gentler sigmoid
        # At t=300s remaining, scale ~0.5; at t=30s, scale ~2.0
        time_scale = 0.5 + 1.5 * (1.0 - time_remaining_sec / 300.0)
        time_scale = max(0.5, min(time_scale, 3.0))

        raw_prob = _sigmoid(combined_z * time_scale)

        # Dynamic clamp — tighter in choppy markets to reduce overconfidence
        max_clamp = 0.80 - (choppiness * 0.15)
        min_clamp = 1.0 - max_clamp
        return max(min_clamp, min(max_clamp, raw_prob))

    def _calc_momentum(self, seconds: float) -> float:
        """Price change in $/second over the last N seconds."""
        now = time.time()
        cutoff = now - seconds
        recent = [(ts, p) for ts, p in self._btc_prices if ts >= cutoff]
        if len(recent) < 2:
            return 0.0
        dt = recent[-1][0] - recent[0][0]
        if dt < 0.3:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / dt

    def _calc_strength(
        self,
        edge: float,
        mom_5s: float,
        mom_15s: float,
        time_remaining_sec: float,
        choppiness: float = 0.0,
    ) -> float:
        """Signal strength (0..1). Must exceed CONFIG.min_signal_strength to trade."""
        # Edge contribution (primary): 15% edge = max score
        edge_score = min(1.0, abs(edge) / 0.15)

        # Momentum agreement: short-term and medium-term agree?
        if mom_5s == 0 and mom_15s == 0:
            mom_agreement = 0.5
        elif (mom_5s > 0) == (mom_15s > 0):
            mom_agreement = 1.0
        else:
            # Disagreement: in choppy markets this kills the signal entirely
            mom_agreement = 0.5 * (1.0 - choppiness)

        # Dynamic warmup: slightly longer in choppy markets, but not so much
        # that we miss the 0-30s and 60-90s sweet spots (both 61% WR historically)
        effective_warmup = CONFIG.warmup_sec + choppiness * 15

        # Time window: don't trade in warmup or cooldown
        if time_remaining_sec > (300 - effective_warmup):
            # Still in warmup
            time_score = 0.0
        elif time_remaining_sec < CONFIG.cooldown_sec:
            # In cooldown
            time_score = 0.0
        else:
            time_score = 1.0

        return edge_score * mom_agreement * time_score


def _null_signal(reason: str) -> Signal:
    return Signal(
        direction="UP",
        strength=0.0,
        edge=0.0,
        btc_momentum=0.0,
        poly_implied_prob=0.5,
        fair_prob=0.5,
        reason=reason,
    )
