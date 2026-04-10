# Bracket Orders — Design Spec

**Status:** Draft — pending implementation
**Author:** Brainstormed with Claude (Opus 4.6) on 2026-04-10
**Target:** `btcbot` (polybtcbot repo)

## Motivation

Today's bot exits losing positions via a hedge (buying the opposite side to neutralize). Paper data from 2026-04-10 shows this is destroying profitability:

- 30 markets traded, 23 hedged (77%), 4 pure wins, 3 pure losses
- Pure-directional trades: +$85 net, 57% win rate — **the signal works**
- Hedged trades: −$278 net, avg −$12 per hedge — **hedging destroys the edge**
- Total paper P&L: −$192

The hedge always locks in a guaranteed loss because `entry_price + opposite_price > $1.00` (spread). It also provides no upside capture: if the token recovers after the hedge fires, we're stuck with a neutralized position.

**Bracket orders** (take-profit + stop-loss exits) fix both problems:
- **TP locks in gains** on favorable moves the current bot lets slip away
- **SL caps losses** without tying up capital in a hedge
- Unlike hedging, a bracket exit is a clean close — the position is gone, capital is returned, and (in a future v2) re-entry becomes possible

## Goals

1. Add take-profit and stop-loss bracket exits to the trading loop
2. Make brackets the primary exit mechanism, with hedging retained as a live-mode safety net
3. Ship with sensible defaults tuned to today's observed price distributions
4. Work identically in paper and live modes (same code path, same decision logic)
5. No schema changes, no new async tasks, no new WebSocket subscriptions

## Non-goals (explicitly out of scope for v1)

- **Re-entry after bracket close** — logic will be revisited in a v2 once brackets are validated
- **Real CLOB limit orders** — v1 uses virtual brackets (monitored + market sell); real limit orders are a v2 optimization if polling latency proves costly
- **Partial-fill reconciliation** — FOK orders are atomic; defensive handling is logged but not actioned
- **Arb-First / multi-position strategies** — separate concerns
- **Trailing stops, scale-out, multiple TP levels** — YAGNI for v1

## Design Decisions (Summary)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Architecture | Virtual brackets (monitored in existing `_risk_monitor_loop`) | Mirrors hedge flow; no new tasks; same paper/live code |
| 2 | Brackets vs hedge | Brackets primary, hedge as fallback | Keeps hedge as live-mode lifeboat; user preference |
| 3 | Threshold type | Percentage of entry price | Symmetric with `BOT_HEDGE_TRIGGER`; scales naturally |
| 4 | Defaults | TP 40%, SL 25%; 0 disables each | Tuned to observed winner/loser price distributions |
| 5 | End-of-window behavior | Let it ride (no force-close at T−30s) | Matches existing hedge guard; capture natural resolution |
| 6 | Paper fill model | Observed price − `SIMULATED_SPREAD/2` | Symmetric with paper entry fills |
| 7 | Arming | TP armed immediately, SL delayed by `BOT_BRACKET_SL_ARM_SEC` (default 10s) | Avoids noise-triggered stops, captures fast winners |
| 8 | Hedge fallback | Only on bracket SELL exception; bypasses normal hedge guards | Lifeboat semantics — guards don't apply when exit path failed |
| 9 | Bracket finalization | `upsert_result` called immediately on successful exit | Atomic bracket trades, immediate P&L booking |

## Architecture

### Control flow in `_risk_monitor_loop`

```
every tick:
  if no open position: sleep and continue

  current_price = poly_feed.get_price(position.token_id)
  seconds_since_entry = now - position.entry_time

  bracket_reason = risk.should_exit_bracket(position, current_price, seconds_since_entry)
  # Returns "TP" | "SL" | None

  if bracket_reason:
    try:
      exit_trade = await executor.place_exit(market, position, reason, current_price)
    except Exception:
      exit_trade = None  # triggers hedge fallback

    if exit_trade:
      await handle_bracket_exit(exit_trade, position, market, bracket_reason)
      sleep and continue  # position is closed, skip hedge path

  # Hedge path (normal OR fallback)
  opposite_price = poly_feed.get_price(opposite_token)
  if risk.should_hedge(...) or (bracket_reason and exit_trade is None):
    await place_hedge(...)   # normal guards apply unless bracket_fallback
```

### Bracket decision logic (`risk.py`)

New method on `RiskManager`:

```python
def should_exit_bracket(
    self,
    position: OpenPosition,
    current_price: float | None,
    seconds_since_entry: float,
) -> Literal["TP", "SL"] | None:
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

**Properties:**
- TP wins if both trigger the same tick (favorable outcomes take priority)
- No `seconds_remaining` guard — brackets fire any time in the window (unlike hedge)
- No choppiness adjustment — TP/SL are the user's deliberate risk preferences
- No `opposite_price` check — SL is a clean sell; opposite price is irrelevant

### Config additions (`btcbot/config.py`)

New fields on `Config` dataclass:

```python
bracket_tp: float              # percentage, 0 disables
bracket_sl: float              # percentage, 0 disables
bracket_sl_arm_sec: float      # grace period before SL arms
```

Loaded in `load_config()`:

```python
bracket_tp=_env_float("BOT_BRACKET_TP", 0.40),
bracket_sl=_env_float("BOT_BRACKET_SL", 0.25),
bracket_sl_arm_sec=_env_float("BOT_BRACKET_SL_ARM_SEC", 10.0),
```

### Executor changes

New method on both `PaperExecutor` and `Executor` with identical signature:

```python
async def place_exit(
    self,
    market: Market,
    position: OpenPosition,
    reason: Literal["TP", "SL"],
    observed_price: float,
) -> TradeRecord | None:
    """Close the position via a market sell."""
```

**Paper implementation (`btcbot/paper.py`):**

```python
async def place_exit(self, market, position, reason, observed_price):
    fill_price = max(0.01, observed_price - SIMULATED_SPREAD / 2)
    proceeds = position.token_quantity * fill_price
    log.info(
        "[PAPER] EXIT %s %s @ $%.3f — $%.2f (%s)",
        position.direction, market.slug, fill_price, proceeds, reason,
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

**Live implementation (`btcbot/execution.py`):**

```python
async def place_exit(self, market, position, reason, observed_price):
    if not self._client:
        return None
    try:
        resp = await asyncio.to_thread(
            self._fok_sell, position.token_id, position.token_quantity, observed_price
        )
        if resp and resp.get("orderID"):
            fill_price = float(resp.get("averagePrice", observed_price))
            proceeds = position.token_quantity * fill_price
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
    except Exception:
        log.error("Exit order failed", exc_info=True)
        raise  # re-raise so engine can trigger hedge fallback
    return None
```

**New helper `_fok_sell`** (parallel to existing `_fok_buy`): constructs a `MarketOrderArgs` with `side="SELL"` using the token quantity. Exact py-clob-client argument shape to be verified during implementation.

**Semantic note on `amount_usd` for EXIT trades:**
- For ENTRY/HEDGE (BUY): `amount_usd` = USDC **spent**
- For EXIT (SELL): `amount_usd` = USDC **received** (proceeds)

This mirrors the `side` semantics and is consumed correctly by the rewritten `_summarize_market_result`.

### Models (`btcbot/models.py`)

Single change: extend the `TradeRecord.trade_type` literal.

```python
# Before
trade_type: Literal["ENTRY", "HEDGE"]

# After
trade_type: Literal["ENTRY", "HEDGE", "EXIT"]
```

No changes to `OpenPosition` — arm state is computed from `entry_time` per-tick.

### Accounting — `_summarize_market_result` rewrite

The current function assumes every trade is a BUY and computes payout as "tokens matching outcome redeem at $1". With EXIT trades, we must track USDC flows in both directions and compute remaining holdings.

```python
def _summarize_market_result(
    trades: list[TradeRecord], outcome: str
) -> tuple[float, float, float, float, int | None]:
    """Return entry cost, hedge cost, payout, net P&L, and outcome classification.

    Handles three trade compositions:
    - ENTRY only:            held to resolution, classified by oracle outcome
    - ENTRY + HEDGE:         neutralized via opposite buy, classified as hedged (None)
    - ENTRY + EXIT:          bracket-closed, classified by net P&L sign
    """
    entry_cost = sum(t.amount_usd for t in trades if t.trade_type == "ENTRY")
    hedge_cost = sum(t.amount_usd for t in trades if t.trade_type == "HEDGE")
    exit_proceeds = sum(t.amount_usd for t in trades if t.trade_type == "EXIT")

    # Track remaining token holdings by direction for oracle redemption
    holdings: dict[str, float] = {"UP": 0.0, "DOWN": 0.0}
    for t in trades:
        if t.side == "BUY":
            holdings[t.direction] += t.token_quantity
        else:  # SELL (bracket exit)
            holdings[t.direction] -= t.token_quantity

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

**Worked examples:**

| Scenario | Trades | Holdings | Payout | Net P&L | Classification |
|---|---|---|---|---|---|
| Held, entry UP wins | ENTRY UP 95@$0.42 ($40) | UP=95 | 95 | +$55 | win (1) |
| Held, entry UP loses | ENTRY UP 95@$0.42 ($40) | UP=95 | 0 | −$40 | loss (0) |
| TP fires @$0.59, UP wins | ENTRY UP 95@$0.42 ($40), EXIT UP 95@$0.59 ($56) | UP=0 | $56 | +$16 | win (1) |
| SL fires @$0.315, DOWN wins | ENTRY UP 95@$0.42 ($40), EXIT UP 95@$0.315 ($30) | UP=0 | $30 | −$10 | loss (0) |
| SL fires @$0.315, UP wins (reversal) | same as above, outcome=UP | UP=0 | $30 | −$10 | loss (0) |
| Hedge fires DOWN @$0.65 | ENTRY UP 95@$0.42, HEDGE DOWN 95@$0.65 ($62) | UP=95, DOWN=95 | 95 | −$7 | None (hedged) |

**Key behaviors:**
- Bracket SL with late oracle reversal still counts as a loss (we're out, real USDC flow)
- Bracket TP counts as a real win in daily stats
- Hedged trades continue to classify as `None` (unchanged)
- The `payout` column now semantically means "total USDC returned" (redemption + exits); downstream consumers (daily_pnl, dashboard) only use it as a display field, and `net_pnl_usd` is the same

### Engine integration (`_risk_monitor_loop`)

**New helper `_handle_bracket_exit`:**

```python
async def _handle_bracket_exit(
    self,
    exit_trade: TradeRecord,
    position: OpenPosition,
    market: Market,
    reason: str,
) -> None:
    """Persist the exit, finalize the market result, and clear position state."""
    try:
        async with connect() as conn:
            await insert_trade(conn, exit_trade)
            # Compute result immediately — bracket trades are atomic
            market_trades = await trades_for_market(conn, market.slug)
            # Use entry direction as a proxy "outcome" for classification since
            # oracle hasn't resolved yet; _summarize handles bracket classification
            # based on net P&L sign regardless of outcome value
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
            # Sync risk manager from DB (daily P&L, loss streak)
            today_ts = int(datetime.datetime.combine(
                datetime.date.today(), datetime.time.min
            ).timestamp())
            self._risk.daily_pnl = await pnl_since(conn, today_ts)
            self._risk.sync_streak(await trailing_loss_streak(conn))
            await clear_open_position(conn)
    except Exception:
        log.warning("Failed to persist bracket exit", exc_info=True)

    self._position = None
    self._risk.open_positions.clear()

    log.info(
        "EXIT[%s] %s %s @ $%.3f — proceeds $%.2f",
        reason, position.direction, market.slug,
        exit_trade.fill_price, exit_trade.amount_usd,
    )
```

**Interaction with `_resolve_position` at market end:**

When the oracle eventually resolves a bracket-closed market, `_resolve_position` will:
1. Find `self._position is None` (already cleared) — early return, OR
2. If called via `_switch_market` with a stale reference, re-run `_summarize_market_result` on the same trade list — idempotent, produces the same result

Since `_handle_bracket_exit` already calls `upsert_result`, the `market_result` row exists before the oracle resolves. The resolution path will overwrite it with the same values (trades list is identical), which is safe.

`set_market_outcome` is NOT called by `_handle_bracket_exit` — the market slug has no oracle outcome yet. The natural `_resolve_position` flow later records the oracle outcome into the `markets` table separately. This is fine because `outcome_correct` classification for bracket trades doesn't depend on the oracle.

### Engine integration — `_risk_monitor_loop` updated

The loop gains a bracket-first check before the existing hedge logic. Bracket fallback to hedge happens only if `place_exit` throws:

```python
async def _risk_monitor_loop(self) -> None:
    while not self._stop.is_set():
        if self._position and self._current_market:
            pos = self._position
            mkt = self._current_market
            our_price = self._polymarket.get_price(pos.token_id)
            seconds_since_entry = time.time() - pos.entry_time

            # 1. Bracket check
            bracket_reason = self._risk.should_exit_bracket(
                pos, our_price, seconds_since_entry
            )
            exit_trade = None
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

                if exit_trade:
                    await self._handle_bracket_exit(exit_trade, pos, mkt, bracket_reason)
                    await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)
                    continue

            # 2. Hedge path — normal guards apply, unless this is a fallback
            opposite_dir = "DOWN" if pos.direction == "UP" else "UP"
            opposite_token = mkt.token_id_for(opposite_dir)
            opposite_price = self._polymarket.get_price(opposite_token)

            bracket_fallback = bracket_reason is not None and exit_trade is None
            should_hedge_normal = self._risk.should_hedge(
                pos, self._binance.latest_price, self._polymarket,
                choppiness=self._regime.choppiness, opposite_price=opposite_price,
            )
            if should_hedge_normal or bracket_fallback:
                # ... existing hedge placement flow ...
                pass

        await _sleep_or_stop(self._stop, CONFIG.risk_check_interval_sec)
```

### Web UI changes (`btcbot/web/routes.py`)

The trades page currently labels trades as `"hedge"`, `"win"`, `"loss"`, or `"pending"`. Add labels for bracket exits:

```python
if t.trade_type == "HEDGE":
    result = "hedge"
elif t.trade_type == "EXIT":
    # Bracket exit — classify by P&L sign from market_result
    result = "tp" if (r == 1) else "sl"
elif hedge_cost > 0:
    result = "hedge"
elif r == 1:
    result = "win"
elif r == 0:
    result = "loss"
else:
    result = "pending"
```

CSS classes for `"tp"` and `"sl"` may need to be added to the template (same color as `win`/`loss` or distinct).

## Error Handling

| Failure | Detection | Response |
|---|---|---|
| CLOB sell rejected (insufficient balance, bad signing) | Exception in `_fok_sell` | Re-raise; engine triggers hedge fallback bypassing guards |
| CLOB network timeout | `asyncio.to_thread` raises | Same as above |
| `place_exit` returns `None` without raising | Returned value is None | Treated as "exit did not happen" — hedge fallback fires |
| `current_price is None` | `should_exit_bracket` returns None | Skip tick, retry |
| `position.fill_price <= 0` | `should_exit_bracket` returns None | Skip bracket path; hedge path has same guard |
| Partial fill (SELL partially filled) | FOK should prevent; defensive | Log warning, treat as full exit (v1 limitation) |

**Restart safety:** If the bot crashes between `place_exit` and `insert_trade`, `load_open_position` still returns the open position on restart. The CLOB sell may or may not have executed. Next monitor tick re-evaluates brackets and may fire again. This matches the current hedge path's restart model.

## Testing Strategy

### Unit tests — `tests/test_risk.py` (new `TestBracketExit` class)

- `test_tp_fires_when_price_exceeds_target`
- `test_sl_fires_when_price_below_target_after_arm`
- `test_sl_blocked_during_arm_delay`
- `test_tp_armed_immediately_ignores_arm_delay`
- `test_tp_preferred_over_sl_same_tick` (degenerate edge case)
- `test_bracket_disabled_with_zero` (via monkeypatched config)
- `test_no_bracket_when_price_is_none`
- `test_no_bracket_when_fill_price_zero`

### Unit tests — `tests/test_engine.py` (additions to existing `TestSummarizeMarketResult`)

- `test_bracket_tp_counted_as_win`
- `test_bracket_sl_counted_as_loss`
- `test_bracket_sl_with_oracle_reversal_still_loss`
- Existing tests unchanged: `test_unhedged_win_is_counted_as_win`, `test_hedged_market_uses_both_legs_and_not_win_loss`

### Integration test — `tests/test_engine.py`

- `test_bracket_tp_fires_in_monitor_loop_and_clears_position` (mocked executor + poly feed)

### Manual validation plan

Before enabling in live mode:

1. Set `BOT_BRACKET_TP=0.30 BOT_BRACKET_SL=0.20` (aggressive defaults to maximize triggers)
2. Run paper mode for at least 30 markets (~2.5 hours)
3. Query DB: verify EXIT trades appear with correct direction and proceeds
4. Query DB: verify `market_results.outcome_correct` is `1` for TP exits, `0` for SL exits
5. Compare aggregate P&L against the previous hedged-heavy paper run (−$192 baseline)
6. Watch for any hedge fallback firing in paper — should be zero (bug if not)
7. If results are positive, tune to recommended defaults (TP 40% / SL 25%) and run another 30+ markets
8. Only then consider enabling in live mode

## Rollout Plan

1. Implement the full design with `BOT_BRACKET_TP=0 BOT_BRACKET_SL=0` in `.env.example` (disabled by default)
2. User enables brackets in their personal `.env`
3. Paper-mode validation (50+ trades minimum) with chosen thresholds
4. Review: compare P&L, hedge count, TP/SL ratios against baseline
5. Tune defaults in `.env.example` based on paper results
6. Live mode enabled only after conservative paper validation

## Files Modified

- `btcbot/config.py` — 3 new fields in `Config` + `load_config`
- `btcbot/models.py` — extend `TradeRecord.trade_type` literal
- `btcbot/risk.py` — new `should_exit_bracket` method
- `btcbot/execution.py` — new `place_exit` + `_fok_sell` helper
- `btcbot/paper.py` — new `place_exit`
- `btcbot/engine.py` — `_risk_monitor_loop` bracket-first check + new `_handle_bracket_exit`; `_summarize_market_result` rewritten
- `btcbot/web/routes.py` — trade page labels for EXIT rows
- `tests/test_risk.py` — `TestBracketExit` class (8 tests)
- `tests/test_engine.py` — 3 new summary tests + 1 integration test
- `README.md` — features list, risk controls table, "How Bracket Orders Work" section
- `SETUP.md` — env var reference, bracket tuning guide

## Files NOT Modified

- `btcbot/storage/repo.py` — no schema changes; EXIT trades reuse `trades` table
- `btcbot/feeds/*` — no new data needed
- `btcbot/signal.py` — unchanged
- `btcbot/cli.py` — existing trade listing already prints `trade_type`

## Known Limitations (accepted for v1)

1. **No partial-fill reconciliation** — FOK should prevent it; logged as warning if observed.
2. **No CLOB order state reconciliation on restart** — if the bot crashes mid-exit, restart re-evaluates and may re-fire.
3. **Polling latency ~2s** — fast spikes that reverse within the poll interval are missed. Acceptable for 5-minute markets; revisit if data shows impact.
4. **No re-entry after bracket close** — explicitly deferred to v2.
5. **Thresholds use current config values, not position-scoped** — if the user changes `.env` mid-run without restart, existing positions pick up the new thresholds. Acceptable; document in SETUP.md.
