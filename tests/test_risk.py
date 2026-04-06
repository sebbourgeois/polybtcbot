"""Unit tests for the risk manager."""

import time
from unittest.mock import MagicMock

import pytest

from btcbot.models import Market, OpenPosition, Signal
from btcbot.risk import RiskManager


@pytest.fixture
def risk() -> RiskManager:
    return RiskManager()


@pytest.fixture
def signal() -> Signal:
    return Signal(
        direction="UP",
        strength=0.5,
        edge=0.08,
        btc_momentum=1.0,
        poly_implied_prob=0.48,
        fair_prob=0.56,
        reason="test",
    )


@pytest.fixture
def market() -> Market:
    now = int(time.time())
    return Market(
        slug="btc-updown-5m-test",
        condition_id="0xabc",
        up_token_id="token-up",
        down_token_id="token-down",
        start_ts=now - 150,
        end_ts=now + 150,
    )


class TestCanTrade:
    def test_allows_valid_signal(self, risk: RiskManager, signal: Signal):
        assert risk.can_trade(signal) is True

    def test_blocks_when_position_open(self, risk: RiskManager, signal: Signal, market: Market):
        risk.open_positions.append(
            OpenPosition(
                market=market,
                direction="UP",
                token_id="x",
                fill_price=0.50,
                token_quantity=10,
            )
        )
        assert risk.can_trade(signal) is False

    def test_blocks_daily_loss_limit(self, risk: RiskManager, signal: Signal):
        risk.daily_pnl = -51.0
        assert risk.can_trade(signal) is False

    def test_blocks_consecutive_losses_during_cooldown(self, risk: RiskManager, signal: Signal):
        # Simulate 5 consecutive losses — should enter cooldown
        for _ in range(5):
            risk.record_loss(-5.0)
        assert risk.can_trade(signal) is False  # cooldown active

    def test_blocks_high_price(self, risk: RiskManager):
        expensive = Signal(
            direction="UP",
            strength=0.5,
            edge=0.08,
            btc_momentum=1.0,
            poly_implied_prob=0.70,  # > max_price_to_pay
            fair_prob=0.78,
            reason="test",
        )
        assert risk.can_trade(expensive) is False

    def test_blocks_small_edge(self, risk: RiskManager):
        weak = Signal(
            direction="UP",
            strength=0.5,
            edge=0.02,  # < min_edge
            btc_momentum=1.0,
            poly_implied_prob=0.48,
            fair_prob=0.50,
            reason="test",
        )
        assert risk.can_trade(weak) is False


class TestPositionSizing:
    def test_returns_min_for_extreme_price(self, risk: RiskManager):
        sig = Signal("UP", 0.5, 0.08, 1.0, 0.005, 0.085, "test")
        size = risk.calc_position_size(sig)
        assert size == 2.0  # min_position_usd

    def test_returns_positive_size(self, risk: RiskManager, signal: Signal):
        size = risk.calc_position_size(signal)
        assert size >= 2.0
        assert size <= 25.0

    def test_bigger_edge_bigger_size(self, risk: RiskManager):
        small_edge = Signal("UP", 0.5, 0.06, 1.0, 0.48, 0.54, "test")
        big_edge = Signal("UP", 0.5, 0.12, 1.0, 0.48, 0.60, "test")
        assert risk.calc_position_size(big_edge) >= risk.calc_position_size(small_edge)


class TestRecording:
    def test_win_resets_consecutive(self, risk: RiskManager):
        risk.consecutive_losses = 3
        risk.record_win(5.0)
        assert risk.consecutive_losses == 0
        assert risk.daily_pnl == 5.0

    def test_loss_increments_consecutive(self, risk: RiskManager):
        risk.record_loss(-5.0)
        assert risk.consecutive_losses == 1
        assert risk.daily_pnl == -5.0

    def test_reset_daily(self, risk: RiskManager):
        risk.daily_pnl = -30.0
        risk.consecutive_losses = 4
        risk.reset_daily()
        assert risk.daily_pnl == 0.0
        assert risk.consecutive_losses == 0
