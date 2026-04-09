"""Main trading engine — orchestrates feeds, signals, execution, and risk."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

import httpx

from .config import CONFIG
from .feeds.binance_ws import BinanceFeed
from .feeds.chainlink import ChainlinkFeed
from .feeds.polymarket_ws import PolymarketFeed
from .market_discovery import discover_active_market
from .models import Market, OpenPosition, TradeRecord
from .regime import RegimeDetector
from .risk import RiskManager
from .signal import SignalGenerator
from .storage.db import connect, init_db
from .storage.repo import (
    clear_open_position,
    get_market,
    insert_btc_price,
    insert_poly_price,
    insert_trade,
    load_open_position,
    save_open_position,
    set_market_outcome,
    set_market_start_price,
    trailing_loss_streak,
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
        self._chainlink = ChainlinkFeed(on_price=self._on_chainlink_price)
        self._polymarket = PolymarketFeed(on_price=self._on_poly_price)
        self._signal_gen = SignalGenerator()
        self._risk = RiskManager()
        self._regime = RegimeDetector(window=CONFIG.regime_window)

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
        self._mid_window_price: float | None = None
        self._mid_captured: bool = False

    async def run(self) -> None:
        """Main lifecycle. Initialises DB, then runs concurrent loops."""
        mode = "PAPER" if self._paper_mode else "LIVE"
        log.info("Starting BTC bot in %s mode (bankroll=$%.2f)", mode, CONFIG.bankroll)

        await init_db()
        await self._restore_state()
        self._http_client = httpx.AsyncClient(
            headers={"User-Agent": "btcbot/0.1"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._binance.run(), name="binance_ws")
                tg.create_task(self._chainlink.run(), name="chainlink")
                tg.create_task(self._polymarket.run(), name="polymarket_ws")
                tg.create_task(self._discovery_loop(), name="discovery")
                tg.create_task(self._trading_loop(), name="trading")
                tg.create_task(self._risk_monitor_loop(), name="risk")
                tg.create_task(self._sweep_unresolved_loop(), name="sweep")
        except* KeyboardInterrupt:
            log.info("Keyboard interrupt — shutting down")
        finally:
            await self._binance.stop()
            await self._chainlink.stop()
            await self._polymarket.stop()
            if self._http_client:
                await self._http_client.aclose()
            log.info("Engine stopped")

    # ── State recovery ─────────────────────────────────────────────────

    async def _restore_state(self) -> None:
        """Restore open position and regime detector from DB."""
        await self._seed_regime()
        try:
            async with connect() as conn:
                pos_data = await load_open_position(conn)
                if not pos_data:
                    return

                market_row = await get_market(conn, pos_data["market_slug"])
                if not market_row:
                    log.warning("Saved position references unknown market %s — clearing", pos_data["market_slug"])
                    await clear_open_position(conn)
                    return

                market = Market(
                    slug=market_row.slug,
                    condition_id=market_row.condition_id,
                    up_token_id=market_row.up_token_id,
                    down_token_id=market_row.down_token_id,
                    start_ts=market_row.start_ts,
                    end_ts=market_row.end_ts,
                )
                self._current_market = market
                self._position = OpenPosition(
                    market=market,
                    direction=pos_data["direction"],
                    token_id=pos_data["token_id"],
                    fill_price=pos_data["fill_price"],
                    token_quantity=pos_data["token_quantity"],
                    entry_time=pos_data.get("entry_time", time.time()),
                )
                self._last_discovery_market_slug = market.slug
                log.info(
                    "Restored position: %s %s @ $%.3f in %s",
                    pos_data["direction"], market.slug,
                    pos_data["fill_price"], market.slug,
                )
        except Exception:
            log.warning("Failed to restore position state", exc_info=True)

    async def _seed_regime(self) -> None:
        """Seed regime detector from recent historical data so it starts informed."""
        try:
            async with connect() as conn:
                cur = await conn.execute(
                    """SELECT m.start_ts, m.start_btc_price, m.outcome
                       FROM markets m
                       WHERE m.outcome IS NOT NULL AND m.start_btc_price IS NOT NULL
                       ORDER BY m.start_ts DESC
                       LIMIT ?""",
                    (CONFIG.regime_window,),
                )
                rows = await cur.fetchall()

            # Process oldest first so the buffer ends with the most recent
            seeded = 0
            for start_ts, start_price, outcome in reversed(rows):
                mid_ts = start_ts + 150
                async with connect() as conn:
                    cur = await conn.execute(
                        "SELECT price FROM btc_prices WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1",
                        (mid_ts - 15, mid_ts + 15),
                    )
                    mid_row = await cur.fetchone()
                if mid_row:
                    first_half_dir = "UP" if mid_row[0] >= start_price else "DOWN"
                    self._regime.record(first_half_dir, outcome)
                    seeded += 1

            if seeded:
                log.info(
                    "Regime detector seeded: %d samples, choppiness=%.2f",
                    seeded, self._regime.choppiness,
                )
        except Exception:
            log.warning("Failed to seed regime detector", exc_info=True)

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

    async def _on_chainlink_price(self, price: float, ts: float) -> None:
        """Called on every Chainlink update — this is the oracle's price source."""
        self._signal_gen.update_chainlink_price(price, ts)
        self._price_event.set()

        # Capture mid-window price for regime detection (~150s into the window)
        mkt = self._current_market
        if mkt and not self._mid_captured:
            elapsed = ts - mkt.start_ts
            if elapsed >= 150:
                self._mid_window_price = price
                self._mid_captured = True

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
        # Resolve previous position in background (oracle polling can be slow)
        if self._position and self._current_market:
            pos = self._position
            mkt = self._current_market
            self._position = None
            asyncio.create_task(
                self._resolve_position(pos, mkt), name=f"resolve-{mkt.slug}"
            )

        log.info(
            "New market: %s (ends in %.0fs)",
            market.slug,
            market.seconds_remaining,
        )
        self._current_market = market
        self._position = None
        self._mid_window_price = None
        self._mid_captured = False
        self._signal_gen.reset(market)

        # Subscribe to the new tokens
        await self._polymarket.set_token_ids({market.up_token_id, market.down_token_id})

        # Persist market to DB
        try:
            async with connect() as conn:
                await upsert_market(conn, market)
        except Exception:
            log.warning("Failed to persist market", exc_info=True)

        # Record market start price (prefer Chainlink since it matches the oracle)
        start_price = self._chainlink.latest_price or self._binance.latest_price
        if start_price > 0:
            try:
                async with connect() as conn:
                    await set_market_start_price(conn, market.slug, start_price)
            except Exception:
                pass

    async def _fetch_oracle_outcome(self, slug: str) -> str | None:
        """Poll the Gamma API for the oracle-resolved outcome."""
        import json as _json

        for attempt in range(80):
            try:
                resp = await self._http_client.get(
                    f"{CONFIG.gamma_api_base}/events",
                    params={"slug": slug},
                    timeout=10.0,
                )
                data = resp.json()
                if not data:
                    continue
                event = data[0] if isinstance(data, list) else data
                market = event.get("markets", [{}])[0]
                outcomes = market.get("outcomes", [])
                prices = market.get("outcomePrices", [])
                if isinstance(outcomes, str):
                    outcomes = _json.loads(outcomes)
                if isinstance(prices, str):
                    prices = _json.loads(prices)
                if "1" in prices:
                    winner = outcomes[prices.index("1")]
                    return winner.upper()
            except Exception:
                pass
            await asyncio.sleep(15)
        return None

    async def _resolve_position(self, pos: OpenPosition | None = None, mkt: Market | None = None) -> None:
        """Resolve the current position using the Polymarket oracle outcome."""
        pos = pos or self._position
        mkt = mkt or self._current_market
        if not pos or not mkt:
            return

        outcome = await self._fetch_oracle_outcome(mkt.slug)
        if outcome is None:
            log.warning("Could not fetch oracle outcome for %s — skipping resolution", mkt.slug)
            return

        btc_end = self._binance.latest_price
        won = pos.direction == outcome

        # Feed regime detector with reversal data
        if self._mid_window_price is not None and self._signal_gen._chainlink_start_price is not None:
            first_half_dir = "UP" if self._mid_window_price >= self._signal_gen._chainlink_start_price else "DOWN"
            self._regime.record(first_half_dir, outcome)

        # Calculate P&L
        entry_cost = pos.fill_price * pos.token_quantity
        payout = pos.token_quantity if won else 0.0
        net_pnl = payout - entry_cost

        status = "WIN" if won else "LOSS"
        log.info(
            "Resolved %s: %s — PnL=$%.2f (entry=$%.3f, payout=$%.2f)",
            mkt.slug, status, net_pnl, entry_cost, payout,
        )

        # Auto-redeem winning tokens in live mode (background — oracle may lag)
        if won and not self._paper_mode and hasattr(self._executor, "redeem"):
            asyncio.create_task(
                self._redeem_and_mark(mkt.slug, mkt.condition_id),
                name=f"redeem-{mkt.slug}",
            )

        self._risk.open_positions.clear()

        # Persist to DB, then sync risk manager from DB (immune to async race)
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
                # Sync consecutive losses from DB (chronological trade order)
                self._risk.daily_pnl += net_pnl
                streak = await trailing_loss_streak(conn)
                self._risk.sync_streak(streak)

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
                await clear_open_position(conn)
        except Exception:
            log.warning("Failed to persist resolution", exc_info=True)

    async def _redeem_and_mark(self, slug: str, condition_id: str) -> None:
        """Redeem tokens and mark the result as redeemed in the DB."""
        tx_hash = await self._executor.redeem(condition_id)
        if tx_hash:
            try:
                async with connect() as conn:
                    await conn.execute(
                        "UPDATE market_results SET redeemed_at = ? WHERE market_slug = ?",
                        (int(time.time()), slug),
                    )
                    await conn.commit()
            except Exception:
                log.warning("Failed to mark %s as redeemed", slug, exc_info=True)

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
                choppiness=self._regime.choppiness,
            )

            if signal.strength < CONFIG.min_signal_strength:
                continue

            if not self._risk.can_trade(signal, choppiness=self._regime.choppiness):
                continue

            # Place trade
            amount = self._risk.calc_position_size(signal, choppiness=self._regime.choppiness)
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
                        await save_open_position(
                            conn,
                            market_slug=market.slug,
                            direction=trade.direction,
                            token_id=trade.token_id,
                            fill_price=trade.fill_price,
                            token_quantity=trade.token_quantity,
                            entry_time=self._position.entry_time,
                        )
                except Exception:
                    log.warning("Failed to persist trade", exc_info=True)

                log.info(
                    "TRADE %s %s @ $%.3f ($%.2f) — edge=%.3f chop=%.2f",
                    trade.direction,
                    market.slug,
                    trade.fill_price,
                    trade.amount_usd,
                    signal.edge,
                    self._regime.choppiness,
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
                    choppiness=self._regime.choppiness,
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

    # ── Unresolved sweep loop ────────────────────────────────────────

    async def _sweep_unresolved_loop(self) -> None:
        """Periodically resolve markets that the background task missed."""
        import json as _json

        while not self._stop.is_set():
            await _sleep_or_stop(self._stop, 300)  # every 5 minutes
            try:
                async with connect() as conn:
                    # Find trades with no market_result yet, for markets that ended >5min ago
                    cutoff = int(time.time()) - 300
                    cur = await conn.execute(
                        """SELECT DISTINCT t.market_slug, m.condition_id, t.direction,
                                  t.fill_price, t.token_quantity
                           FROM trades t
                           JOIN markets m ON m.slug = t.market_slug
                           LEFT JOIN market_results mr ON mr.market_slug = m.slug
                           WHERE t.trade_type = 'ENTRY'
                             AND m.outcome IS NULL
                             AND m.end_ts < ?
                             AND mr.market_slug IS NULL""",
                        (cutoff,),
                    )
                    rows = await cur.fetchall()

                if not rows:
                    continue

                log.info("Sweep: found %d unresolved market(s)", len(rows))

                for row in rows:
                    slug, condition_id, direction, fill_price, qty = (
                        row[0], row[1], row[2], row[3], row[4],
                    )
                    outcome = await self._fetch_oracle_outcome(slug)
                    if not outcome:
                        continue

                    # Feed regime detector from historical prices
                    try:
                        async with connect() as conn2:
                            mkt_row = await get_market(conn2, slug)
                            if mkt_row and mkt_row.start_btc_price:
                                mid_ts = mkt_row.start_ts + 150
                                cur2 = await conn2.execute(
                                    "SELECT price FROM btc_prices WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT 1",
                                    (mid_ts - 15, mid_ts + 15),
                                )
                                mid_row = await cur2.fetchone()
                                if mid_row:
                                    first_half_dir = "UP" if mid_row[0] >= mkt_row.start_btc_price else "DOWN"
                                    self._regime.record(first_half_dir, outcome)
                    except Exception:
                        pass

                    won = direction == outcome
                    payout = qty if won else 0.0
                    entry_cost = fill_price * qty
                    net_pnl = payout - entry_cost

                    log.info(
                        "Sweep resolved %s: %s (bet %s) — PnL=$%+.2f",
                        slug, outcome, direction, net_pnl,
                    )

                    async with connect() as conn:
                        await set_market_outcome(conn, slug, outcome, 0.0)
                        await upsert_result(
                            conn, slug,
                            entry_cost=entry_cost,
                            hedge_cost=0.0,
                            payout=payout,
                            net_pnl=net_pnl,
                            outcome_correct=1 if won else 0,
                        )

                    # Sync risk manager from DB after all sweep writes
                    self._risk.daily_pnl += net_pnl
                    async with connect() as conn3:
                        streak = await trailing_loss_streak(conn3)
                        self._risk.sync_streak(streak)

                    # Auto-redeem if won in live mode
                    if won and not self._paper_mode and hasattr(self._executor, "redeem"):
                        asyncio.create_task(
                            self._redeem_and_mark(slug, condition_id),
                            name=f"redeem-sweep-{slug}",
                        )

            except Exception:
                log.warning("Sweep error", exc_info=True)

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
