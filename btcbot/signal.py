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
        self._market: Market | None = None

    def reset(self, market: Market) -> None:
        """Called when switching to a new 5-minute market."""
        self._btc_prices.clear()
        self._market_start_price = None
        self._market = market

    def update_btc_price(self, price: float, ts: float) -> None:
        self._btc_prices.append((ts, price))
        if self._market_start_price is None and self._market:
            # Set the start price once we see the first tick in this window
            if ts >= self._market.start_ts:
                self._market_start_price = price

    def evaluate(
        self,
        *,
        btc_price: float,
        poly_up_price: float | None,
        poly_down_price: float | None,
        time_remaining_sec: float,
    ) -> Signal:
        """Core signal logic.

        Compares our estimated fair probability (based on BTC momentum and
        current delta from the window start) against Polymarket's implied odds.
        Returns a Signal with direction, strength, and edge.
        """
        # Can't trade without Polymarket prices
        if poly_up_price is None or poly_down_price is None:
            return _null_signal("No Polymarket prices")

        # Can't evaluate without knowing the market start price
        if self._market_start_price is None:
            return _null_signal("No market start price yet")

        # 1. BTC delta from market start
        price_delta = btc_price - self._market_start_price

        # 2. Momentum over multiple timeframes
        mom_5s = self._calc_momentum(5.0)
        mom_15s = self._calc_momentum(15.0)
        mom_30s = self._calc_momentum(30.0)

        # 3. Estimate our fair probability of "Up"
        fair_prob_up = self._estimate_fair_prob(
            price_delta, mom_5s, mom_15s, time_remaining_sec
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
        strength = self._calc_strength(edge, mom_5s, mom_15s, time_remaining_sec)

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
        blended_mom = 0.7 * mom_5s + 0.3 * mom_15s
        momentum_extrapolation = blended_mom * time_remaining_sec
        z_mom = momentum_extrapolation / vol

        # Combined z-score
        combined_z = z_delta + 0.3 * z_mom

        # Time factor: more time remaining → less confidence → gentler sigmoid
        # At t=300s remaining, scale ~0.5; at t=30s, scale ~2.0
        time_scale = 0.5 + 1.5 * (1.0 - time_remaining_sec / 300.0)
        time_scale = max(0.5, min(time_scale, 3.0))

        raw_prob = _sigmoid(combined_z * time_scale)

        # Clamp — our model isn't good enough for extreme probabilities
        return max(0.08, min(0.92, raw_prob))

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
            mom_agreement = 0.5

        # Time window: don't trade in warmup or cooldown
        if time_remaining_sec > (300 - CONFIG.warmup_sec):
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
