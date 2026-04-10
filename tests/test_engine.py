"""Unit tests for hedge-aware result accounting."""

from btcbot.engine import _summarize_market_result
from btcbot.models import TradeRecord


class TestSummarizeMarketResult:
    def test_unhedged_win_is_counted_as_win(self):
        trades = [
            TradeRecord(
                market_slug="m1",
                trade_type="ENTRY",
                direction="UP",
                token_id="up",
                side="BUY",
                amount_usd=20.0,
                fill_price=0.50,
                token_quantity=40.0,
                signal_strength=0.8,
                signal_edge=0.12,
            )
        ]

        entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(trades, "UP")
        assert entry_cost == 20.0
        assert hedge_cost == 0.0
        assert payout == 40.0
        assert net_pnl == 20.0
        assert outcome_correct == 1

    def test_hedged_market_uses_both_legs_and_not_win_loss(self):
        trades = [
            TradeRecord(
                market_slug="m1",
                trade_type="ENTRY",
                direction="UP",
                token_id="up",
                side="BUY",
                amount_usd=20.0,
                fill_price=0.50,
                token_quantity=40.0,
                signal_strength=0.8,
                signal_edge=0.12,
            ),
            TradeRecord(
                market_slug="m1",
                trade_type="HEDGE",
                direction="DOWN",
                token_id="down",
                side="BUY",
                amount_usd=18.0,
                fill_price=0.45,
                token_quantity=40.0,
                signal_strength=0.0,
                signal_edge=0.0,
            ),
        ]

        entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(trades, "DOWN")
        assert entry_cost == 20.0
        assert hedge_cost == 18.0
        assert payout == 40.0
        assert net_pnl == 2.0
        assert outcome_correct is None
