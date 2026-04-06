"""Unit tests for the signal generator."""

import time
from unittest.mock import patch

import pytest

from btcbot.models import Market
from btcbot.signal import SignalGenerator, _sigmoid


@pytest.fixture
def market() -> Market:
    now = int(time.time())
    return Market(
        slug="btc-updown-5m-test",
        condition_id="0xabc",
        up_token_id="token-up",
        down_token_id="token-down",
        start_ts=now - 150,  # halfway through
        end_ts=now + 150,
    )


@pytest.fixture
def gen(market: Market) -> SignalGenerator:
    g = SignalGenerator()
    g.reset(market)
    return g


class TestSigmoid:
    def test_midpoint(self):
        assert _sigmoid(0) == 0.5

    def test_positive(self):
        assert _sigmoid(2) > 0.5

    def test_negative(self):
        assert _sigmoid(-2) < 0.5

    def test_symmetry(self):
        assert abs(_sigmoid(1) + _sigmoid(-1) - 1.0) < 1e-10


class TestSignalGenerator:
    def test_no_market_start_price(self, gen: SignalGenerator, market: Market):
        # No BTC prices fed yet — should return null signal
        sig = gen.evaluate(
            btc_price=50000,
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        assert sig.strength == 0.0
        assert "No market start price" in sig.reason

    def test_no_poly_prices(self, gen: SignalGenerator, market: Market):
        gen.update_btc_price(50000, market.start_ts + 1)
        sig = gen.evaluate(
            btc_price=50000,
            poly_up_price=None,
            poly_down_price=None,
            time_remaining_sec=150,
        )
        assert sig.strength == 0.0

    def test_btc_up_detects_up(self, gen: SignalGenerator, market: Market):
        # Feed prices showing BTC going up
        base = 50000
        t = market.start_ts
        for i in range(30):
            gen.update_btc_price(base + i * 2, t + i)

        sig = gen.evaluate(
            btc_price=base + 60,
            poly_up_price=0.50,  # Market hasn't moved yet
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        # Should favour UP since BTC is rising but market is at 50/50
        assert sig.direction == "UP"
        assert sig.edge > 0

    def test_btc_down_detects_down(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        for i in range(30):
            gen.update_btc_price(base - i * 2, t + i)

        sig = gen.evaluate(
            btc_price=base - 60,
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        assert sig.direction == "DOWN"
        assert sig.edge > 0

    def test_no_edge_when_market_correct(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        for i in range(30):
            gen.update_btc_price(base + i * 2, t + i)

        sig = gen.evaluate(
            btc_price=base + 60,
            poly_up_price=0.85,  # Market already priced it in
            poly_down_price=0.15,
            time_remaining_sec=150,
        )
        # Edge should be small or negative since market already reflects reality
        assert sig.edge < 0.10

    def test_warmup_kills_strength(self, gen: SignalGenerator, market: Market):
        """During warmup period, strength should be 0."""
        base = 50000
        t = market.start_ts
        gen.update_btc_price(base, t + 1)

        sig = gen.evaluate(
            btc_price=base + 100,
            poly_up_price=0.40,
            poly_down_price=0.60,
            time_remaining_sec=280,  # Only 20s elapsed = still in warmup
        )
        assert sig.strength == 0.0

    def test_cooldown_kills_strength(self, gen: SignalGenerator, market: Market):
        """During cooldown period, strength should be 0."""
        base = 50000
        t = market.start_ts
        gen.update_btc_price(base, t + 1)

        sig = gen.evaluate(
            btc_price=base + 100,
            poly_up_price=0.40,
            poly_down_price=0.60,
            time_remaining_sec=30,  # Only 30s left = cooldown
        )
        assert sig.strength == 0.0

    def test_fair_prob_clamped(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        gen.update_btc_price(base, t + 1)

        sig = gen.evaluate(
            btc_price=base + 5000,  # Massive move
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        # Fair prob should be clamped, not 0.99+
        assert sig.fair_prob <= 0.92
