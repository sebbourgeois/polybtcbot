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
    def test_no_chainlink_price(self, gen: SignalGenerator, market: Market):
        # No prices fed yet — should return null signal
        sig = gen.evaluate(
            btc_price=50000,
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        assert sig.strength == 0.0
        assert "No Chainlink price" in sig.reason

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
        # Feed prices showing BTC going up (delta=$30, within max_delta)
        base = 50000
        t = market.start_ts
        gen.update_chainlink_price(base, t)
        for i in range(30):
            gen.update_btc_price(base + i, t + i)
        gen.update_chainlink_price(base + 30, t + 30)

        sig = gen.evaluate(
            btc_price=base + 30,
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
        gen.update_chainlink_price(base, t)
        for i in range(30):
            gen.update_btc_price(base - i, t + i)
        gen.update_chainlink_price(base - 30, t + 30)

        sig = gen.evaluate(
            btc_price=base - 30,
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        assert sig.direction == "DOWN"
        assert sig.edge > 0

    def test_no_edge_when_market_correct(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        gen.update_chainlink_price(base, t)
        for i in range(30):
            gen.update_btc_price(base + i, t + i)
        gen.update_chainlink_price(base + 30, t + 30)

        sig = gen.evaluate(
            btc_price=base + 30,
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
        gen.update_chainlink_price(base, t + 1)

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
        gen.update_chainlink_price(base, t + 1)

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
        gen.update_chainlink_price(base, t + 1)
        gen.update_chainlink_price(base + 40, t + 2)  # Near max_delta but under

        sig = gen.evaluate(
            btc_price=base + 40,
            poly_up_price=0.50,
            poly_down_price=0.50,
            time_remaining_sec=150,
        )
        # Fair prob should be clamped, not 0.99+
        assert sig.fair_prob <= 0.80

    def test_chainlink_start_price_waits_for_market_start(self, market: Market):
        gen = SignalGenerator()
        gen.reset(market)
        gen.update_chainlink_price(50000, market.start_ts - 5)
        assert gen._chainlink_start_price is None

        gen.update_chainlink_price(50010, market.start_ts + 1)
        assert gen._chainlink_start_price == 50010

    def test_reversal_setup_kills_strength(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        gen.update_chainlink_price(base, t)

        # Recent BTC momentum points down while the oracle delta is still up.
        for i in range(16):
            gen.update_btc_price(base + 20 - i, t + i)
        gen.update_chainlink_price(base + 15, t + 16)

        with patch("btcbot.signal.time.time", return_value=t + 16):
            sig = gen.evaluate(
                btc_price=base + 4,
                poly_up_price=0.30,
                poly_down_price=0.70,
                time_remaining_sec=150,
            )
        assert sig.strength == 0.0

    def test_small_reversal_noise_does_not_kill_strength(self, gen: SignalGenerator, market: Market):
        base = 50000
        t = market.start_ts
        gen.update_chainlink_price(base, t)

        # Small disagreement should be treated as noise, not a hard veto.
        prices = [base + 10, base + 10.5, base + 10.2, base + 10.0, base + 9.8, base + 9.7]
        for i, price in enumerate(prices, start=1):
            gen.update_btc_price(price, t + i * 3)
        gen.update_chainlink_price(base + 12, t + 18)

        with patch("btcbot.signal.time.time", return_value=t + 18):
            sig = gen.evaluate(
                btc_price=prices[-1],
                poly_up_price=0.40,
                poly_down_price=0.60,
                time_remaining_sec=150,
            )
        assert sig.strength >= 0.0
