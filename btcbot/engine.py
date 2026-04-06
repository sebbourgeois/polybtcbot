"""Main trading engine — orchestrates feeds, signals, execution, and risk."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

import httpx

from .config import CONFIG
from .feeds.binance_ws import BinanceFeed
from .feeds.polymarket_ws import PolymarketFeed
from .market_discovery import discover_active_market
from .models import Market, OpenPosition, TradeRecord
from .risk import RiskManager
from .signal import SignalGenerator
from .storage.db import connect, init_db
from .storage.repo import (
    insert_btc_price,
    insert_poly_price,
    insert_trade,
    set_market_outcome,
    set_market_start_price,
    update_daily_pnl,
    upsert_market,
    upsert_result,
    win_loss_counts,
)

log = logging.getLogger(__name__)


class Engine:
    """Runs 5 concurrent async loops under a single TaskGroup."""

    def __init__(self, paper_mode: bool = True) -> None:
        self._paper_mode = paper_mode
        self._stop = asyncio.Event()
        self._price_event = asyncio.Event()

        # Components
        self._binance = BinanceFeed(on_price=self._on_btc_price)
        self._polymarket = PolymarketFeed(on_price=self._on_poly_price)
        self._signal_gen = SignalGenerator()
        self._risk = RiskManager()

        # Build executor
        if paper_mode:
            from .paper import PaperExecutor
            self._executor: object = PaperExecutor()
        else:
            from .execution import Executor
            self._executor = Executor()

        # State
        self._current_market: Market | None = None
        self._position: OpenPosition | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._btc_price_sample_ts: float = 0.0
        self._last_discovery_market_slug: str = ""
        self._current_date: str = datetime.date.today().isoformat()

    async def run(self) -> None:
        """Main lifecycle. Initialises DB, then runs concurrent loops."""
        mode = "PAPER" if self._paper_mode else "LIVE"
        log.info("Starting BTC bot in %s mode (bankroll=$%.2f)", mode, CONFIG.bankroll)

        await init_db()
        self._http_client = httpx.AsyncClient(
            headers={"User-Agent": "btcbot/0.1"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._binance.run(), name="binance_ws")
                tg.create_task(self._polymarket.run(), name="polymarket_ws")
                tg.create_task(self._discovery_loop(), name="discovery")
                tg.create_task(self._trading_loop(), name="trading")
                tg.create_task(self._risk_monitor_loop(), name="risk")
        except* KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down")
        finally:
            await self._binance.stop()
            await self._polymarket.stop()
            if self._http_client:
                await self._http_client.aclose()
            log.info("Engine stopped")

    # ── Callbacks ──────────────────────────────────────────────────────

    async def _on_btc_price(self, price: float, ts: float) -> None:
        """Called on every Binance tick."""
        self._signal_gen.update_btc_price(price, ts)
        self._price_event.set()

        # Sample BTC prices to DB every 5 seconds
        if ts - self._btc_price_sample_ts >= 5.0:
            self._btc_price_sample_ts = ts
            try:
                async with connect() as conn:
                    await insert_btc_price(conn, price, int(ts))
            except Exception:
                pass

    async def _on_poly_price(self, token_id: str, price: float) -> None:
        """Called on every Polymarket price update."""
        self._price_event.set()
        try:
            async with connect() as conn:
                await insert_poly_price(conn, token_id, price)
        except Exception:
            pass

    # ── Discovery loop ─────────────────────────────────────────────────

    async def _discovery_loop(self) -> None:
        """Poll for the active 5-minute BTC market every N seconds."""
        while not self._stop.is_set():
            # Reset risk counters at midnight
            today = datetime.date.today().isoformat()
            if today != self._current_date:
                log.info("New day %s — resetting daily risk counters", today)
                self._risk.reset_daily()
                self._current_date = today

            try:
                market = await discover_active_market(self._http_client)
                if market and market.slug != self._last_discovery_market_slug:
                    self._last_discovery_market_slug = market.slug
                    await self._switch_market(market)
            except Exception:
                log.warning("Discovery error", exc_info=True)
            await _sleep_or_stop(self._stop, CONFIG.discovery_interval_sec)

    async def _switch_market(self, market: Market) -> None:
        """Transition to a new 5-minute market window."""
        # Resolve previous position if any
        if self._position and self._current_market:
            await self._resolve_position()

        log.info(
            "New market: %s (ends in %.0fs)",
            market.slug,
            market.seconds_remaining,
        )
        self._current_market = market
        self._position = None
        self._signal_gen.reset(market)

        # Subscribe to the new tokens
        await self._polymarket.set_token_ids({market.up_token_id, market.down_token_id})

        # Persist market to DB
        try:
            async with connect() as conn:
                await upsert_market(conn, market)
        except Exception:
            log.warning("Failed to persist market", exc_info=True)

        # Record market start price once we have BTC data
        if self._binance.latest_price > 0:
            try:
                async with connect() as conn:
                    await set_market_start_price(
                        conn, market.slug, self._binance.latest_price
                    )
            except Exception:
                pass

    async def _resolve_position(self) -> None:
        """Resolve the current position based on BTC price at window end."""
        pos = self._position
        mkt = self._current_market
        if not pos or not mkt:
            return

        btc_end = self._binance.latest_price
        start_row = None
        try:
            from .storage.repo import get_market
            async with connect() as conn:
                start_row = await get_market(conn, mkt.slug)
        except Exception:
            pass

        btc_start = start_row.start_btc_price if start_row else None
        if btc_start is None:
            log.warning("No start BTC price for %s — cannot resolve", mkt.slug)
            return

        outcome = "UP" if btc_end >= btc_start else "DOWN"
        won = pos.direction == outcome

        # Calculate P&L
        entry_cost = pos.fill_price * pos.token_quantity
        payout = pos.token_quantity if won else 0.0
        net_pnl = payout - entry_cost

        status = "WIN" if won else "LOSS"
        log.info(
            "Resolved %s: %s — PnL=$%.2f (entry=$%.3f, payout=$%.2f)",
            mkt.slug, status, net_pnl, entry_cost, payout,
        )

        # Update risk manager
        if won:
            self._risk.record_win(net_pnl)
        else:
            self._risk.record_loss(net_pnl)

        self._risk.open_positions.clear()

        # Persist to DB
        try:
            async with connect() as conn:
                await set_market_outcome(conn, mkt.slug, outcome, btc_end)
                await upsert_result(
                    conn, mkt.slug,
                    entry_cost=entry_cost,
                    hedge_cost=0.0,
                    payout=payout,
                    net_pnl=net_pnl,
                    outcome_correct=1 if won else 0,
                )
                # Update daily P&L aggregate
                today = datetime.date.today().isoformat()
                today_ts = int(
                    datetime.datetime.combine(
                        datetime.date.today(), datetime.time.min
                    ).timestamp()
                )
                wins, losses, hedged = await win_loss_counts(conn, since_ts=today_ts)
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(net_pnl_usd), 0) FROM market_results WHERE resolved_at >= ?",
                    (today_ts,),
                )
                row = await cur.fetchone()
                today_pnl = float(row[0])
                await update_daily_pnl(
                    conn, today,
                    trades_count=wins + losses + hedged,
                    wins=wins,
                    losses=losses,
                    hedged=hedged,
                    gross_pnl=today_pnl,
                    net_pnl=today_pnl,
                )
        except Exception:
            log.warning("Failed to persist resolution", exc_info=True)

        self._position = None

    # ── Trading loop ───────────────────────────────────────────────────

    async def _trading_loop(self) -> None:
        """Evaluate signals on every price update and trade when appropriate."""
        while not self._stop.is_set():
            # Wait for a price update
            try:
                await asyncio.wait_for(self._price_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            self._price_event.clear()

            market = self._current_market
            if not market or not market.is_active:
                continue

            # Already have a position in this window
            if self._position:
                continue

            signal = self._signal_gen.evaluate(
                btc_price=self._binance.latest_price,
                poly_up_price=self._polymarket.get_price(market.up_token_id),
                poly_down_price=self._polymarket.get_price(market.down_token_id),
                time_remaining_sec=market.seconds_remaining,
            )

            if signal.strength < CONFIG.min_signal_strength:
                continue

            if not self._risk.can_trade(signal):
                continue

            # Place trade
            amount = self._risk.calc_position_size(signal)
            trade = await self._executor.place_trade(market, signal, amount)

            if trade:
                self._position = OpenPosition(
                    market=market,
                    direction=trade.direction,
                    token_id=trade.token_id,
                    fill_price=trade.fill_price,
                    token_quantity=trade.token_quantity,
                )
                self._risk.open_positions.append(self._position)

                try:
                    async with connect() as conn:
                        await insert_trade(conn, trade)
                except Exception:
                    log.warning("Failed to persist trade", exc_info=True)

                log.info(
                    "TRADE %s %s @ $%.3f ($%.2f) — edge=%.3f",
                    trade.direction,
                    market.slug,
                    trade.fill_price,
                    trade.amount_usd,
                    signal.edge,
                )

    # ── Risk monitor loop ──────────────────────────────────────────────

    async def _risk_monitor_loop(self) -> None:
        """Periodically check if open positions need hedging."""
        while not self._stop.is_set():
            if self._position and self._current_market:
                if self._risk.should_hedge(
                    self._position,
                    self._binance.latest_price,
                    self._polymarket,
                ):
                    hedge = await self._executor.place_hedge(
                        self._current_market, self._position
                    )
                    if hedge:
                        try:
                            async with connect() as conn:
                                await insert_trade(conn, hedge)
                        except Exception:
                            log.warning("Failed to persist hedge", exc_info=True)
                        log.info(
                            "HEDGED %s — bought %s @ $%.3f",
                            self._current_market.slug,
                            hedge.direction,
                            hedge.fill_price,
                        )
            await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)

    # ── Shutdown ───────────────────────────────────────────────────────

    async def stop(self) -> None:
        self._stop.set()
        await self._binance.stop()
        await self._polymarket.stop()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds`, but return immediately if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
