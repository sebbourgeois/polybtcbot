# Bracket Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add take-profit (TP) and stop-loss (SL) bracket exits to btcbot's trading loop, replacing the current hedge-only exit path with virtual brackets that market-sell positions when thresholds are crossed. Keep hedging as a live-mode fallback.

**Architecture:** Virtual brackets monitored inside the existing `_risk_monitor_loop`. TP is armed immediately, SL after a configurable grace period. On successful exit, the market result is finalized atomically. If the bracket sell errors in live mode, the hedge path fires as a lifeboat with normal guards bypassed.

**Tech Stack:** Python 3.12, asyncio, pytest, py-clob-client, SQLite (aiosqlite). All new code lives in existing modules — no new files in the main package.

**Spec:** `docs/superpowers/specs/2026-04-10-bracket-orders-design.md`

---

## File Structure

**Modified:**
- `btcbot/config.py` — 3 new config fields
- `btcbot/models.py` — extend `TradeRecord.trade_type` literal
- `btcbot/risk.py` — new `should_exit_bracket` method
- `btcbot/execution.py` — new `place_exit` method + `_fok_sell` helper
- `btcbot/paper.py` — new `place_exit` method
- `btcbot/engine.py` — rewrite `_summarize_market_result`, add `_handle_bracket_exit`, update `_risk_monitor_loop`
- `btcbot/web/routes.py` — add labels for EXIT trades on trades page
- `tests/test_risk.py` — new `TestBracketExit` class (8 tests)
- `tests/test_engine.py` — 3 new summary tests
- `README.md` — update features list + risk controls
- `SETUP.md` — add bracket tuning section

**Not modified:**
- `btcbot/storage/repo.py` — no schema changes
- `btcbot/feeds/*` — no new data
- `btcbot/signal.py` — unchanged
- `btcbot/cli.py` — already prints `trade_type` generically

---

## Task 1: Add bracket config fields

**Files:**
- Modify: `btcbot/config.py`

- [ ] **Step 1: Add new fields to `Config` dataclass**

Open `btcbot/config.py`. In the `@dataclass(frozen=True) class Config:` block, locate the risk limits section:

```python
    # --- Risk limits ---
    max_position_usd: float
    min_position_usd: float
    max_daily_loss_usd: float
    max_consecutive_losses: int
    min_price_to_pay: float
    max_price_to_pay: float
    hedge_trigger_threshold: float
```

Add three new fields immediately after `hedge_trigger_threshold`:

```python
    hedge_trigger_threshold: float
    bracket_tp: float
    bracket_sl: float
    bracket_sl_arm_sec: float
```

- [ ] **Step 2: Add env-var loading in `load_config()`**

In `load_config()` in the same file, locate:

```python
        hedge_trigger_threshold=_env_float("BOT_HEDGE_TRIGGER", 0.15),
        regime_window=_env_int("BOT_REGIME_WINDOW", 20),
```

Insert three lines between them:

```python
        hedge_trigger_threshold=_env_float("BOT_HEDGE_TRIGGER", 0.15),
        bracket_tp=_env_float("BOT_BRACKET_TP", 0.40),
        bracket_sl=_env_float("BOT_BRACKET_SL", 0.25),
        bracket_sl_arm_sec=_env_float("BOT_BRACKET_SL_ARM_SEC", 10.0),
        regime_window=_env_int("BOT_REGIME_WINDOW", 20),
```

- [ ] **Step 3: Run existing tests to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: All 35 tests pass (the new config fields have defaults, so nothing breaks).

- [ ] **Step 4: Commit**

```bash
git add btcbot/config.py
git commit -m "Add bracket order config fields"
```

---

## Task 2: Extend `TradeRecord.trade_type` to include `EXIT`

**Files:**
- Modify: `btcbot/models.py`

- [ ] **Step 1: Extend the literal**

Open `btcbot/models.py`. Locate:

```python
    trade_type: Literal["ENTRY", "HEDGE"]
```

Replace with:

```python
    trade_type: Literal["ENTRY", "HEDGE", "EXIT"]
```

- [ ] **Step 2: Run tests to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: All 35 tests still pass. (No runtime use of the literal, so nothing breaks.)

- [ ] **Step 3: Commit**

```bash
git add btcbot/models.py
git commit -m "Extend TradeRecord.trade_type to include EXIT"
```

---

## Task 3: Risk — `should_exit_bracket` method (TDD)

**Files:**
- Test: `tests/test_risk.py`
- Modify: `btcbot/risk.py`

- [ ] **Step 1: Write the failing tests**

Open `tests/test_risk.py`. At the end of the file, add:

```python
class TestBracketExit:
    @pytest.fixture
    def bracket_market(self) -> Market:
        """Market with plenty of time remaining."""
        now = int(time.time())
        return Market(
            slug="btc-updown-5m-bracket",
            condition_id="0xabc",
            up_token_id="token-up",
            down_token_id="token-down",
            start_ts=now - 60,
            end_ts=now + 240,
        )

    @pytest.fixture
    def position(self, bracket_market: Market) -> OpenPosition:
        return OpenPosition(
            market=bracket_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.50,
            token_quantity=10,
        )

    def test_tp_fires_when_price_exceeds_target(self, risk: RiskManager, position: OpenPosition):
        # TP default 0.40 -> target 0.50 * 1.40 = 0.70
        assert risk.should_exit_bracket(position, current_price=0.72, seconds_since_entry=1.0) == "TP"

    def test_tp_not_fired_below_target(self, risk: RiskManager, position: OpenPosition):
        assert risk.should_exit_bracket(position, current_price=0.68, seconds_since_entry=1.0) is None

    def test_sl_fires_when_price_below_target_after_arm(self, risk: RiskManager, position: OpenPosition):
        # SL default 0.25 -> target 0.50 * 0.75 = 0.375
        # Arm delay default 10s
        assert risk.should_exit_bracket(position, current_price=0.36, seconds_since_entry=15.0) == "SL"

    def test_sl_blocked_during_arm_delay(self, risk: RiskManager, position: OpenPosition):
        # Same SL target as above, but within arm delay
        assert risk.should_exit_bracket(position, current_price=0.30, seconds_since_entry=5.0) is None

    def test_tp_armed_immediately_ignores_arm_delay(self, risk: RiskManager, position: OpenPosition):
        # TP fires even at seconds_since_entry=0
        assert risk.should_exit_bracket(position, current_price=0.72, seconds_since_entry=0.0) == "TP"

    def test_tp_preferred_over_sl_same_tick(self, risk: RiskManager, position: OpenPosition, monkeypatch):
        # Degenerate: should never happen in practice but TP wins
        import btcbot.risk as _risk
        weird = _risk.CONFIG.__class__(**{
            **_risk.CONFIG.__dict__,
            "bracket_tp": 0.10,   # target 0.55
            "bracket_sl": 0.10,   # target 0.45
        })
        monkeypatch.setattr(_risk, "CONFIG", weird)
        # At price 0.60, both would trigger — but TP is checked first
        assert risk.should_exit_bracket(position, current_price=0.60, seconds_since_entry=15.0) == "TP"

    def test_bracket_disabled_with_zero(self, risk: RiskManager, position: OpenPosition, monkeypatch):
        import btcbot.risk as _risk
        disabled = _risk.CONFIG.__class__(**{
            **_risk.CONFIG.__dict__,
            "bracket_tp": 0.0,
            "bracket_sl": 0.0,
        })
        monkeypatch.setattr(_risk, "CONFIG", disabled)
        # Even extreme moves don't trigger
        assert risk.should_exit_bracket(position, current_price=0.99, seconds_since_entry=100.0) is None
        assert risk.should_exit_bracket(position, current_price=0.01, seconds_since_entry=100.0) is None

    def test_no_bracket_when_price_is_none(self, risk: RiskManager, position: OpenPosition):
        assert risk.should_exit_bracket(position, current_price=None, seconds_since_entry=15.0) is None

    def test_no_bracket_when_fill_price_zero(self, risk: RiskManager, bracket_market: Market):
        broken = OpenPosition(
            market=bracket_market,
            direction="UP",
            token_id="token-up",
            fill_price=0.0,
            token_quantity=10,
        )
        assert risk.should_exit_bracket(broken, current_price=0.50, seconds_since_entry=15.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_risk.py::TestBracketExit -v`
Expected: All 9 tests FAIL with `AttributeError: 'RiskManager' object has no attribute 'should_exit_bracket'`

- [ ] **Step 3: Implement `should_exit_bracket` in `risk.py`**

Open `btcbot/risk.py`. Locate the end of the `should_hedge` method (around line 207). Add the following method immediately after it, at the same indentation level:

```python
    def should_exit_bracket(
        self,
        position: OpenPosition,
        current_price: float | None,
        seconds_since_entry: float,
    ) -> str | None:
        """Decide whether to close the position via a bracket exit.

        Returns "TP" (take profit) or "SL" (stop loss) when a threshold is
        crossed, None otherwise. TP is armed immediately; SL respects a
        configurable arm delay to absorb post-entry noise.
        """
        if current_price is None or position.fill_price <= 0:
            return None

        # TP: armed immediately — favorable moves are "free"
        if CONFIG.bracket_tp > 0:
            tp_target = position.fill_price * (1 + CONFIG.bracket_tp)
            if current_price >= tp_target:
                return "TP"

        # SL: armed after grace period
        if CONFIG.bracket_sl > 0 and seconds_since_entry >= CONFIG.bracket_sl_arm_sec:
            sl_target = position.fill_price * (1 - CONFIG.bracket_sl)
            if current_price <= sl_target:
                return "SL"

        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_risk.py::TestBracketExit -v`
Expected: All 9 tests PASS.

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 44 tests pass (35 original + 9 new).

- [ ] **Step 6: Commit**

```bash
git add tests/test_risk.py btcbot/risk.py
git commit -m "Add should_exit_bracket with TP/SL logic and arm delay"
```

---

## Task 4: Paper executor — `place_exit`

**Files:**
- Modify: `btcbot/paper.py`

- [ ] **Step 1: Add the `place_exit` method**

Open `btcbot/paper.py`. At the end of the `PaperExecutor` class (after the `place_hedge` method), add:

```python
    async def place_exit(
        self,
        market: Market,
        position: OpenPosition,
        reason: str,
        observed_price: float,
    ) -> TradeRecord | None:
        """Simulate closing the position via a market sell at observed - half-spread."""
        fill_price = max(0.01, observed_price - SIMULATED_SPREAD / 2)
        proceeds = position.token_quantity * fill_price

        log.info(
            "[PAPER] EXIT[%s] %s %s @ $%.3f — $%.2f",
            reason,
            position.direction,
            market.slug,
            fill_price,
            proceeds,
        )

        return TradeRecord(
            market_slug=market.slug,
            trade_type="EXIT",
            direction=position.direction,
            token_id=position.token_id,
            side="SELL",
            amount_usd=proceeds,
            fill_price=fill_price,
            token_quantity=position.token_quantity,
            signal_strength=0.0,
            signal_edge=0.0,
            order_id=f"paper-exit-{uuid.uuid4().hex[:8]}",
            is_paper=True,
        )
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 44 tests still pass (no new tests yet — paper executor is tested indirectly via engine tests in Task 7).

- [ ] **Step 3: Commit**

```bash
git add btcbot/paper.py
git commit -m "Add PaperExecutor.place_exit for bracket exits"
```

---

## Task 5: Live executor — `place_exit` + `_fok_sell`

**Files:**
- Modify: `btcbot/execution.py`

- [ ] **Step 1: Add the `place_exit` method**

Open `btcbot/execution.py`. At the end of the `Executor` class, immediately before the `async def redeem` method (around line 168), add:

```python
    async def place_exit(
        self,
        market: Market,
        position: OpenPosition,
        reason: str,
        observed_price: float,
    ) -> TradeRecord | None:
        """Close the position via a market sell (FOK). Raises on CLOB errors so the
        engine can trigger the hedge fallback."""
        if not self._client:
            return None

        try:
            resp = await asyncio.to_thread(
                self._fok_sell,
                position.token_id,
                position.token_quantity,
                observed_price,
            )
        except Exception:
            log.error("Exit order failed", exc_info=True)
            raise

        if resp and resp.get("orderID"):
            fill_price = float(resp.get("averagePrice", observed_price))
            proceeds = position.token_quantity * fill_price
            log.info(
                "EXIT[%s] %s %s @ $%.3f — $%.2f",
                reason,
                position.direction,
                market.slug,
                fill_price,
                proceeds,
            )
            return TradeRecord(
                market_slug=market.slug,
                trade_type="EXIT",
                direction=position.direction,
                token_id=position.token_id,
                side="SELL",
                amount_usd=proceeds,
                fill_price=fill_price,
                token_quantity=position.token_quantity,
                signal_strength=0.0,
                signal_edge=0.0,
                order_id=resp.get("orderID", ""),
            )
        return None
```

- [ ] **Step 2: Add the `_fok_sell` helper**

In the same file, at the end of the `Executor` class (after `_limit_buy`), add:

```python
    def _fok_sell(
        self, token_id: str, token_quantity: float, observed_price: float
    ) -> dict | None:
        """Synchronous aggressive limit SELL (runs in thread).

        py-clob-client's MarketOrderArgs treats `amount` as USDC on BUY but
        as token-quantity on SELL. We use an aggressive limit sell (current
        price - slippage) with GTC so it fills immediately at top of book.
        This matches how `_limit_buy` handles fallback fills.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType

        price = max(0.01, observed_price - CONFIG.limit_slippage)
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=token_quantity,
            side="SELL",
        )
        signed = self._client.create_order(args)
        return self._client.post_order(signed, OrderType.GTC)
```

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 44 tests still pass (no live CLOB tests — the live executor only runs when a private key is present).

- [ ] **Step 4: Commit**

```bash
git add btcbot/execution.py
git commit -m "Add Executor.place_exit with aggressive limit sell"
```

---

## Task 6: Rewrite `_summarize_market_result` (TDD)

**Files:**
- Test: `tests/test_engine.py`
- Modify: `btcbot/engine.py`

- [ ] **Step 1: Write the failing tests**

Open `tests/test_engine.py`. At the end of the `TestSummarizeMarketResult` class, add these three tests:

```python
    def test_bracket_tp_counted_as_win(self):
        trades = [
            TradeRecord(
                market_slug="m1",
                trade_type="ENTRY",
                direction="UP",
                token_id="up",
                side="BUY",
                amount_usd=40.0,
                fill_price=0.42,
                token_quantity=95.0,
                signal_strength=0.8,
                signal_edge=0.12,
            ),
            TradeRecord(
                market_slug="m1",
                trade_type="EXIT",
                direction="UP",
                token_id="up",
                side="SELL",
                amount_usd=56.0,
                fill_price=0.59,
                token_quantity=95.0,
                signal_strength=0.0,
                signal_edge=0.0,
            ),
        ]
        # Outcome doesn't matter — we've exited
        entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(trades, "DOWN")
        assert entry_cost == 40.0
        assert hedge_cost == 0.0
        assert payout == 56.0  # all from exit proceeds, no redemption
        assert net_pnl == 16.0
        assert outcome_correct == 1

    def test_bracket_sl_counted_as_loss(self):
        trades = [
            TradeRecord(
                market_slug="m1",
                trade_type="ENTRY",
                direction="UP",
                token_id="up",
                side="BUY",
                amount_usd=40.0,
                fill_price=0.42,
                token_quantity=95.0,
                signal_strength=0.8,
                signal_edge=0.12,
            ),
            TradeRecord(
                market_slug="m1",
                trade_type="EXIT",
                direction="UP",
                token_id="up",
                side="SELL",
                amount_usd=30.0,
                fill_price=0.315,
                token_quantity=95.0,
                signal_strength=0.0,
                signal_edge=0.0,
            ),
        ]
        entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(trades, "UP")
        assert entry_cost == 40.0
        assert hedge_cost == 0.0
        assert payout == 30.0
        assert net_pnl == -10.0
        assert outcome_correct == 0

    def test_bracket_sl_with_oracle_reversal_still_loss(self):
        """If SL fires and then the market reverses to our original direction,
        we still took the stop-loss hit — net P&L is negative."""
        trades = [
            TradeRecord(
                market_slug="m1",
                trade_type="ENTRY",
                direction="UP",
                token_id="up",
                side="BUY",
                amount_usd=40.0,
                fill_price=0.42,
                token_quantity=95.0,
                signal_strength=0.8,
                signal_edge=0.12,
            ),
            TradeRecord(
                market_slug="m1",
                trade_type="EXIT",
                direction="UP",
                token_id="up",
                side="SELL",
                amount_usd=30.0,
                fill_price=0.315,
                token_quantity=95.0,
                signal_strength=0.0,
                signal_edge=0.0,
            ),
        ]
        # Market eventually resolves UP but we were already out
        entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(trades, "UP")
        # Holdings[UP] = 95 - 95 = 0, so no redemption
        assert payout == 30.0
        assert net_pnl == -10.0
        assert outcome_correct == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_engine.py::TestSummarizeMarketResult -v`
Expected: The 3 new tests FAIL. The existing 2 tests (`test_unhedged_win_is_counted_as_win`, `test_hedged_market_uses_both_legs_and_not_win_loss`) still PASS.

The failures happen because the current function doesn't account for EXIT trades — it tries to add token_quantity from trades matching outcome but doesn't track sells.

- [ ] **Step 3: Rewrite `_summarize_market_result` in `engine.py`**

Open `btcbot/engine.py`. Replace the entire `_summarize_market_result` function (lines 44-59) with:

```python
def _summarize_market_result(trades: list[TradeRecord], outcome: str) -> tuple[float, float, float, float, int | None]:
    """Return entry cost, hedge cost, payout, net P&L, and outcome classification.

    Handles three trade compositions:
    - ENTRY only:      held to resolution, classified by oracle outcome
    - ENTRY + HEDGE:   neutralized via opposite buy, classified as hedged (None)
    - ENTRY + EXIT:    bracket-closed, classified by net P&L sign
    """
    entry_cost = sum(t.amount_usd for t in trades if t.trade_type == "ENTRY")
    hedge_cost = sum(t.amount_usd for t in trades if t.trade_type == "HEDGE")
    exit_proceeds = sum(t.amount_usd for t in trades if t.trade_type == "EXIT")

    # Track remaining token holdings by direction for oracle redemption
    holdings: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}
    for t in trades:
        if t.side == "BUY":
            holdings[t.direction] = holdings.get(t.direction, 0.0) + t.token_quantity
        else:  # SELL (bracket exit)
            holdings[t.direction] = holdings.get(t.direction, 0.0) - t.token_quantity

    redemption_payout = max(0.0, holdings.get(outcome, 0.0))
    payout = redemption_payout + exit_proceeds
    net_pnl = payout - entry_cost - hedge_cost

    entries = [t for t in trades if t.trade_type == "ENTRY"]
    has_exit = any(t.trade_type == "EXIT" for t in trades)
    has_hedge = any(t.trade_type == "HEDGE" for t in trades)

    if not entries:
        outcome_correct = None
    elif has_hedge:
        outcome_correct = None          # neutralized
    elif has_exit:
        outcome_correct = 1 if net_pnl > 0 else 0   # bracket win/loss
    else:
        outcome_correct = 1 if entries[0].direction == outcome else 0

    return entry_cost, hedge_cost, payout, net_pnl, outcome_correct
```

- [ ] **Step 4: Run tests to verify the new ones pass**

Run: `python -m pytest tests/test_engine.py::TestSummarizeMarketResult -v`
Expected: All 5 tests PASS (2 original + 3 new).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 47 tests pass (44 from prior tasks + 3 new).

- [ ] **Step 6: Commit**

```bash
git add tests/test_engine.py btcbot/engine.py
git commit -m "Rewrite _summarize_market_result to handle bracket EXIT trades"
```

---

## Task 7: Engine — add `_handle_bracket_exit` helper

**Files:**
- Modify: `btcbot/engine.py`

- [ ] **Step 1: Add the `_handle_bracket_exit` method**

Open `btcbot/engine.py`. Locate the `_risk_monitor_loop` method (around line 548). Insert this new method immediately BEFORE it:

```python
    async def _handle_bracket_exit(
        self,
        exit_trade: TradeRecord,
        position: OpenPosition,
        market: Market,
        reason: str,
    ) -> None:
        """Persist the exit, finalize the market result, and clear position state.

        Called on successful bracket exit (TP or SL fill). Computes P&L from
        the full trade list (ENTRY + EXIT) and upserts the market_result row
        immediately — the oracle hasn't resolved yet, but bracket trades are
        atomic: exit fill == final P&L.
        """
        try:
            async with connect() as conn:
                await insert_trade(conn, exit_trade)
                market_trades = await trades_for_market(conn, market.slug)
                # Use entry direction as a proxy "outcome" for the summary call.
                # For ENTRY+EXIT composition, outcome_correct is derived from
                # net_pnl sign (not direction match), so the proxy is harmless.
                entry_cost, hedge_cost, payout, net_pnl, outcome_correct = _summarize_market_result(
                    market_trades, outcome=position.direction
                )
                await upsert_result(
                    conn, market.slug,
                    entry_cost=entry_cost,
                    hedge_cost=hedge_cost,
                    payout=payout,
                    net_pnl=net_pnl,
                    outcome_correct=outcome_correct,
                )
                today_ts = int(
                    datetime.datetime.combine(
                        datetime.date.today(), datetime.time.min
                    ).timestamp()
                )
                self._risk.daily_pnl = await pnl_since(conn, today_ts)
                self._risk.sync_streak(await trailing_loss_streak(conn))
                await clear_open_position(conn)
        except Exception:
            log.warning("Failed to persist bracket exit", exc_info=True)

        self._position = None
        self._risk.open_positions.clear()

        log.info(
            "EXIT[%s] %s %s @ $%.3f — proceeds $%.2f",
            reason,
            position.direction,
            market.slug,
            exit_trade.fill_price,
            exit_trade.amount_usd,
        )
```

- [ ] **Step 2: Run full test suite to verify no regression**

Run: `python -m pytest tests/ -v`
Expected: All 47 tests still pass.

- [ ] **Step 3: Commit**

```bash
git add btcbot/engine.py
git commit -m "Add _handle_bracket_exit helper for atomic bracket finalization"
```

---

## Task 8: Engine — integrate brackets in `_risk_monitor_loop`

**Files:**
- Modify: `btcbot/engine.py`

- [ ] **Step 1: Replace `_risk_monitor_loop` with bracket-aware version**

Open `btcbot/engine.py`. Locate the entire `_risk_monitor_loop` method (around lines 548-596). Replace it with:

```python
    async def _risk_monitor_loop(self) -> None:
        """Periodically check brackets, then hedge, on the current position."""
        while not self._stop.is_set():
            if self._position and self._current_market:
                pos = self._position
                mkt = self._current_market
                our_price = self._polymarket.get_price(pos.token_id)
                seconds_since_entry = time.time() - pos.entry_time

                # 1. Bracket check — TP armed immediately, SL after arm delay
                bracket_reason = self._risk.should_exit_bracket(
                    pos, our_price, seconds_since_entry
                )
                exit_trade: TradeRecord | None = None
                if bracket_reason and our_price is not None:
                    try:
                        exit_trade = await self._executor.place_exit(
                            mkt, pos, reason=bracket_reason, observed_price=our_price,
                        )
                    except Exception:
                        log.warning(
                            "Bracket exit errored — falling through to hedge fallback",
                            exc_info=True,
                        )
                        exit_trade = None

                    if exit_trade:
                        await self._handle_bracket_exit(exit_trade, pos, mkt, bracket_reason)
                        await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)
                        continue

                # 2. Hedge path — normal guards OR fallback-bypass if bracket failed
                opposite_dir = "DOWN" if pos.direction == "UP" else "UP"
                opposite_token = mkt.token_id_for(opposite_dir)
                opposite_price = self._polymarket.get_price(opposite_token)

                bracket_fallback = bracket_reason is not None and exit_trade is None
                should_hedge_normal = self._risk.should_hedge(
                    pos,
                    self._binance.latest_price,
                    self._polymarket,
                    choppiness=self._regime.choppiness,
                    opposite_price=opposite_price,
                )
                if should_hedge_normal or bracket_fallback:
                    hedge = await self._executor.place_hedge(
                        mkt,
                        pos,
                        estimated_price=opposite_price,
                    )
                    if hedge:
                        pos.hedge_count += 1
                        pos.hedge_amount_usd += hedge.amount_usd
                        pos.hedge_token_quantity += hedge.token_quantity
                        pos.hedge_fill_price = hedge.fill_price
                        try:
                            async with connect() as conn:
                                await insert_trade(conn, hedge)
                                await save_open_position(
                                    conn,
                                    market_slug=mkt.slug,
                                    direction=pos.direction,
                                    token_id=pos.token_id,
                                    fill_price=pos.fill_price,
                                    token_quantity=pos.token_quantity,
                                    entry_time=pos.entry_time,
                                    hedge_count=pos.hedge_count,
                                    hedge_amount_usd=pos.hedge_amount_usd,
                                    hedge_token_quantity=pos.hedge_token_quantity,
                                    hedge_fill_price=pos.hedge_fill_price,
                                )
                        except Exception:
                            log.warning("Failed to persist hedge", exc_info=True)
                        log.info(
                            "HEDGED %s — bought %s @ $%.3f ($%.2f)%s",
                            mkt.slug,
                            hedge.direction,
                            hedge.fill_price,
                            hedge.amount_usd,
                            " [FALLBACK]" if bracket_fallback else "",
                        )
            await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 47 tests still pass. (The risk monitor loop has no direct unit tests — the integration is validated through the unit tests of `should_exit_bracket`, `place_exit`, and `_summarize_market_result` which we've already covered, plus the manual validation in Task 12.)

- [ ] **Step 3: Smoke test — verify engine module imports cleanly**

Run: `python -c "from btcbot.engine import Engine; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add btcbot/engine.py
git commit -m "Integrate bracket orders in _risk_monitor_loop with hedge fallback"
```

---

## Task 9: Web routes — label EXIT trades

**Files:**
- Modify: `btcbot/web/routes.py`

- [ ] **Step 1: Update the trade label logic**

Open `btcbot/web/routes.py`. Locate the `trades_page` function (around line 184). Find this block:

```python
        for t in trades:
            result_row = results.get(t.market_slug)
            r = result_row[0] if result_row else None
            hedge_cost = result_row[1] if result_row else 0.0
            if t.trade_type == "HEDGE":
                result = "hedge"
            elif hedge_cost > 0:
                result = "hedge"
            elif r == 1:
                result = "win"
            elif r == 0:
                result = "loss"
            else:
                result = None
```

Replace it with:

```python
        for t in trades:
            result_row = results.get(t.market_slug)
            r = result_row[0] if result_row else None
            hedge_cost = result_row[1] if result_row else 0.0
            if t.trade_type == "HEDGE":
                result = "hedge"
            elif t.trade_type == "EXIT":
                # Bracket exit — classify by market_result P&L sign
                result = "tp" if r == 1 else "sl"
            elif hedge_cost > 0:
                result = "hedge"
            elif r == 1:
                result = "win"
            elif r == 0:
                result = "loss"
            else:
                result = None
```

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All 47 tests still pass.

- [ ] **Step 3: Smoke test — verify routes module imports cleanly**

Run: `python -c "from btcbot.web.routes import router; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add btcbot/web/routes.py
git commit -m "Label bracket EXIT trades as tp/sl in trades view"
```

---

## Task 10: README — update features and risk controls

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the Features list**

Open `README.md`. Locate the Features bullet list (around line 24):

```markdown
- **Risk management** — Quarter-Kelly sizing, daily stop-loss, consecutive loss limits, and single-shot hedging with dynamic thresholds
```

Replace with:

```markdown
- **Bracket orders** — Automatic take-profit and stop-loss exits with asymmetric arming (TP immediate, SL after grace period)
- **Risk management** — Quarter-Kelly sizing, daily stop-loss, consecutive loss limits, bracket exits with hedging fallback
```

- [ ] **Step 2: Update the Risk Controls table**

In the same file, locate the "Risk Controls" table (around line 111). Find:

```markdown
| Hedge trigger | 15% drop | Triggers at a percentage drop from entry price, once per market |
| Hedge time guard | Last 150s | Only hedges in the second half of the market window |
| Hedge price guard | opposite < $0.85 | Skips hedge if the opposite token is too expensive |
```

Replace with:

```markdown
| Bracket take-profit | +40% | Sells position when token rises 40% above entry |
| Bracket stop-loss | −25% | Sells position when token drops 25% below entry |
| Bracket SL arm delay | 10s | SL is inert for the first N seconds after entry to absorb noise |
| Hedge trigger (fallback) | 15% drop | Fires only if bracket exit fails in live mode |
```

- [ ] **Step 3: Replace the "How Hedging Works" section**

In the same file, locate the "### How Hedging Works" section. Replace the entire section (heading + paragraphs) with:

```markdown
### How Bracket Orders Work

If you buy "Up" at $0.42, two exit conditions are monitored on every risk-loop tick:

- **Take profit**: if the token rises to $0.42 × 1.40 = $0.588, the bot market-sells the full position. Locks in the favorable move before a reversal.
- **Stop loss**: if the token drops to $0.42 × 0.75 = $0.315, the bot market-sells the full position. Caps the loss at ~$0.10/token instead of the $0.42/token risked.

The TP is armed from the first tick after entry. The SL waits 10 seconds (configurable) so that normal post-entry noise can't trigger it. Whichever fires first closes the position atomically — the market result is recorded immediately, and the bot is ready for the next market.

### Hedging (Fallback)

Hedging is retained as a safety net for live mode: if a bracket market-sell errors out (CLOB rejection, network failure, thin book), the bot falls back to buying the opposite side to neutralize the position. In paper mode, brackets never fail, so the hedge path is effectively unused.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "README: document bracket orders"
```

---

## Task 11: SETUP — add bracket tuning section

**Files:**
- Modify: `SETUP.md`

- [ ] **Step 1: Add env vars to the reference table**

Open `SETUP.md`. Locate the signal thresholds block in the recommended `.env` (around line 172):

```bash
# ── Signal thresholds ────────────────────────────────────────────────
BOT_MIN_EDGE=0.12                # Require stronger directional edge
BOT_MIN_SIGNAL_STRENGTH=0.65     # Require stronger confirmation
BOT_WARMUP_SEC=45                # Wait longer before entering
BOT_COOLDOWN_SEC=120             # Stop new entries with 2m left
```

Immediately after this block (before the `# ── Dashboard` block), insert:

```bash
# ── Bracket orders ───────────────────────────────────────────────────
BOT_BRACKET_TP=0.40              # Take profit at +40% of entry price
BOT_BRACKET_SL=0.25              # Stop loss at -25% of entry price
BOT_BRACKET_SL_ARM_SEC=10        # SL inert for first 10s after entry
```

- [ ] **Step 2: Add a bracket tuning subsection**

In the same file, locate the "### BTC Volatility" subsection. Immediately BEFORE it, insert a new subsection:

```markdown
### Bracket Tuning

Brackets are the primary exit mechanism. Start with the defaults and tune after observing paper results.

| Variable | Default | Tighter (faster exits) | Looser (let positions run) |
|---|---|---|---|
| `BOT_BRACKET_TP` | `0.40` | Lower (0.25 = exit at +25%) | Higher (0.60 = exit at +60%) |
| `BOT_BRACKET_SL` | `0.25` | Lower (0.15 = cut earlier) | Higher (0.35 = cut later) |
| `BOT_BRACKET_SL_ARM_SEC` | `10` | Lower (5 = arm faster) | Higher (15 = absorb more noise) |

Set either threshold to `0` to disable that side entirely. Disabling both turns off bracket exits — the bot then falls back to hedge-only behavior.

**Tuning workflow:**
1. Run paper mode for at least 50 markets with defaults
2. Query the DB for EXIT trade outcomes: `sqlite3 btcbot.db "SELECT result, COUNT(*) FROM market_results GROUP BY outcome_correct"`
3. If TP rarely fires (<10% of trades), lower `BOT_BRACKET_TP`
4. If SL fires too often on noise (>30% within the first 20s), raise `BOT_BRACKET_SL_ARM_SEC`
5. If net P&L is negative, widen both thresholds proportionally
```

- [ ] **Step 3: Commit**

```bash
git add SETUP.md
git commit -m "SETUP: add bracket tuning section and env vars"
```

---

## Task 12: Manual paper validation

**Files:** None (runtime validation only)

- [ ] **Step 1: Ensure `.env` has bracket vars set**

Check your local `.env` file includes:
```bash
BOT_BRACKET_TP=0.40
BOT_BRACKET_SL=0.25
BOT_BRACKET_SL_ARM_SEC=10
BOT_PAPER_MODE=true
```

If they aren't there, add them.

- [ ] **Step 2: Restart the bot in paper mode**

Stop any running bot instance. Then start fresh:

```bash
python -m btcbot run
```

Let it run for at least 90 minutes (targeting 15-20 markets minimum for initial sanity check).

- [ ] **Step 3: Query the DB for EXIT trades**

```bash
sqlite3 -header -column btcbot.db "
SELECT 
  market_slug,
  trade_type,
  direction,
  ROUND(amount_usd, 2) as amt,
  ROUND(fill_price, 3) as price,
  datetime(created_at, 'unixepoch', 'localtime') as ts
FROM trades
WHERE trade_type IN ('ENTRY', 'EXIT', 'HEDGE')
ORDER BY created_at DESC
LIMIT 40;"
```

Expected output signals:
- `EXIT` rows appear alongside `ENTRY` rows
- No `HEDGE` rows (in paper mode, hedge fallback should never fire)
- EXIT fill prices look reasonable relative to ENTRY fill prices

- [ ] **Step 4: Query aggregate results**

```bash
sqlite3 -header -column btcbot.db "
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END) as wins,
  SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END) as losses,
  SUM(CASE WHEN outcome_correct IS NULL THEN 1 ELSE 0 END) as hedged,
  ROUND(SUM(net_pnl_usd), 2) as net_pnl
FROM market_results
WHERE resolved_at >= strftime('%s', 'now', '-3 hours');"
```

Expected signals (compared to the pre-bracket baseline of −$192 across 30 trades):
- `hedged` count should be 0 (or very low)
- `wins` should include bracket TP exits
- `losses` should include bracket SL exits
- `net_pnl` should be meaningfully better than the hedged-heavy baseline

- [ ] **Step 5: Report findings**

Summarize:
- How many trades ran in the sample window
- TP/SL split and average P&L of each
- Any hedge fallback fires (should be zero)
- Overall net P&L vs baseline

If hedge fallbacks are nonzero in paper mode: that's a bug — investigate before continuing to live.

- [ ] **Step 6: Tune thresholds if needed**

Based on the sample:
- If TP rarely fires → lower `BOT_BRACKET_TP`
- If SL fires too early → raise `BOT_BRACKET_SL_ARM_SEC`  
- If net P&L still negative → widen both thresholds

Restart and re-run the sample if any changes are made.

---

## Rollout checklist (post-implementation)

- [ ] All 47 tests pass
- [ ] Paper mode has run for 50+ markets with new defaults
- [ ] No hedge fallbacks fired in paper mode
- [ ] Net P&L in paper is meaningfully better than the −$192 baseline
- [ ] README and SETUP docs reflect the new feature
- [ ] `.env.example` (if it exists) includes the three new bracket vars
- [ ] Decision: tune defaults and commit the winning values to `.env.example`, or keep current defaults
