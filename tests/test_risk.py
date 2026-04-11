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

    def test_blocks_low_price(self, risk: RiskManager, monkeypatch):
        import btcbot.risk as _risk

        stricter = _risk.CONFIG.__class__(**{
            **_risk.CONFIG.__dict__,
            "min_price_to_pay": 0.40,
        })
        monkeypatch.setattr(_risk, "CONFIG", stricter)

        cheap = Signal(
            direction="UP",
            strength=0.8,
            edge=0.18,
            btc_momentum=1.0,
            poly_implied_prob=0.18,
            fair_prob=0.36,
            reason="test",
        )
        assert risk.can_trade(cheap) is False

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


class TestHedging:
    @pytest.fixture(autouse=True)
    def _enable_hedging(self, monkeypatch):
        """Hedging is disabled by default in CONFIG; re-enable for these tests
        so the underlying trigger logic is still covered."""
        import btcbot.risk as _risk

        enabled = _risk.CONFIG.__class__(**{
            **_risk.CONFIG.__dict__,
            "hedge_enabled": True,
        })
        monkeypatch.setattr(_risk, "CONFIG", enabled)

    @pytest.fixture
    def hedge_market(self) -> Market:
        """Market with ~120s remaining — inside the hedge-eligible window."""
        now = int(time.time())
        return Market(
            slug="btc-updown-5m-hedge",
            condition_id="0xabc",
            up_token_id="token-up",
            down_token_id="token-down",
            start_ts=now - 180,
            end_ts=now + 120,
        )

    def test_blocks_repeat_hedge_once_position_already_hedged(self, risk: RiskManager, hedge_market: Market):
        position = OpenPosition(
            market=hedge_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
            hedge_count=1,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.30
        assert risk.should_hedge(position, 50000, poly) is False

    def test_hedge_uses_relative_drop(self, risk: RiskManager, hedge_market: Market):
        position = OpenPosition(
            market=hedge_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.40
        assert risk.should_hedge(position, 50000, poly, opposite_price=0.65) is True

    def test_blocks_hedge_too_early(self, risk: RiskManager):
        """Don't hedge when more than 150s remain — price can still recover."""
        now = int(time.time())
        early_market = Market(
            slug="btc-updown-5m-early",
            condition_id="0xabc",
            up_token_id="token-up",
            down_token_id="token-down",
            start_ts=now - 60,
            end_ts=now + 200,
        )
        position = OpenPosition(
            market=early_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.20  # massive drop
        assert risk.should_hedge(position, 50000, poly, opposite_price=0.65) is False

    def test_blocks_expensive_opposite(self, risk: RiskManager, hedge_market: Market):
        """Don't hedge when opposite side > 0.85 — locked-in loss too high."""
        position = OpenPosition(
            market=hedge_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.10  # huge drop
        assert risk.should_hedge(position, 50000, poly, opposite_price=0.92) is False

    def test_allows_hedge_with_reasonable_opposite(self, risk: RiskManager, hedge_market: Market):
        """Hedge allowed when opposite is affordable and drop is real."""
        position = OpenPosition(
            market=hedge_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.30  # 40% drop
        assert risk.should_hedge(position, 50000, poly, opposite_price=0.65) is True


class TestHedgingDisabledByDefault:
    def test_should_hedge_returns_false_when_config_disabled(self, risk: RiskManager):
        """With hedge_enabled=False (current default), no setup ever hedges."""
        now = int(time.time())
        market = Market(
            slug="btc-updown-5m-disabled",
            condition_id="0xabc",
            up_token_id="token-up",
            down_token_id="token-down",
            start_ts=now - 180,
            end_ts=now + 120,
        )
        position = OpenPosition(
            market=market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )
        poly = MagicMock()
        poly.get_price.return_value = 0.20  # catastrophic drop — would normally hedge
        assert risk.should_hedge(position, 50000, poly, opposite_price=0.65) is False
