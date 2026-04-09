# Regime-Aware Defense System

**Date:** 2026-04-09
**Problem:** The signal model is momentum-based and performs well in trending markets (72% WR on Apr 8) but fails badly in mean-reverting markets (42% WR on Apr 9). 65% of today's 5-minute windows had first-half momentum reverse by close. High-edge signals are confidently wrong — the model hits near-certainty too early and the price flips.

**Solution:** A 5-layer adaptive defense system driven by a regime detector that measures market choppiness and dynamically adjusts signal confidence, trade timing, position sizing, and hedge sensitivity.

---

## 1. RegimeDetector (`btcbot/regime.py`)

New class that tracks intra-window reversal rate over recent markets.

**State:**
- Rolling buffer of the last N market outcomes (default N=20, configurable via `BOT_REGIME_WINDOW`)
- Each entry: `bool` — True if first-half momentum direction reversed by window close

**Interface:**
- `record(first_half_dir: str, outcome: str) -> None` — called at resolution time. Appends `first_half_dir != outcome` to the buffer.
- `choppiness` property — returns `reversals / total` (float 0.0-1.0). Returns 0.5 (neutral) when fewer than 5 samples collected (warmup period).

**Data source:** The engine captures a Chainlink price snapshot at the midpoint of each market window (~150s in). At resolution, it compares the first-half direction (start price vs mid price) to the oracle outcome and feeds the detector.

## 2. Signal Adjustments (`btcbot/signal.py`)

### Dynamic fair_prob clamp

Current: `max(0.08, min(0.92, raw_prob))`

New: `evaluate()` accepts a `choppiness: float` parameter (default 0.0 for backwards compatibility).

```
max_clamp = 0.80 - (choppiness * 0.15)
# trending (choppiness=0.0) -> clamp at 0.80
# choppy   (choppiness=1.0) -> clamp at 0.65
min_clamp = 1.0 - max_clamp
clamped = max(min_clamp, min(max_clamp, raw_prob))
```

This limits overconfidence. Even in trending markets, the model can claim at most 80% probability (down from 92%). In choppy markets, max 65%.

### Dynamic warmup

Current: `CONFIG.warmup_sec` (30s) checked in `_calc_strength()`.

New: `_calc_strength()` uses an effective warmup passed from `evaluate()`:

```
effective_warmup = CONFIG.warmup_sec + choppiness * 60
# trending -> 30s warmup
# choppy   -> 90s warmup
```

Delays trade entry in choppy markets so momentum has more time to stabilize before the bot commits.

## 3. Risk Adjustments (`btcbot/risk.py`)

### Regime-scaled position sizing

`calc_position_size()` accepts a `choppiness: float` parameter (default 0.0).

```
regime_factor = 1.0 - (choppiness * 0.6)
# trending -> 100% of Kelly size
# choppy   -> 40% of Kelly size
size = CONFIG.bankroll * kelly * regime_factor
```

Still clamped by `min_position_usd` / `max_position_usd`.

### Dynamic hedge threshold

`should_hedge()` accepts a `choppiness: float` parameter (default 0.0).

```
effective_threshold = CONFIG.hedge_trigger_threshold - (choppiness * 0.07)
# trending -> 0.15 threshold (unchanged)
# choppy   -> 0.08 threshold (trigger earlier)
```

In choppy markets, hedges trigger when the token drops just 8 cents from entry instead of waiting for 15 cents.

## 4. Engine Plumbing (`btcbot/engine.py`)

### New state
- `self._regime = RegimeDetector(window=CONFIG.regime_window)`
- `self._mid_window_price: float | None = None`
- `self._mid_captured: bool = False` — flag to capture only once per window

### Mid-window price capture
In the `_on_btc_price` (or `_on_chainlink_price`) callback: when the current market is ~50% through its window (around 150s elapsed) and `_mid_captured` is False, snapshot `self._mid_window_price = chainlink_price` and set the flag.

### Resolution feed
In `_resolve_position()` and `_sweep_unresolved_loop()`:
- Compute `first_half_dir`: compare market start price vs mid-window price
- Call `self._regime.record(first_half_dir, outcome)`

For the sweep loop, if no mid-window price is available (market was missed), query `btc_prices` table for a price at the midpoint timestamp.

### Pass choppiness downstream
- `signal.evaluate(..., choppiness=self._regime.choppiness)`
- `risk.calc_position_size(signal, choppiness=self._regime.choppiness)`
- `risk.should_hedge(..., choppiness=self._regime.choppiness)`

### Reset on new market
On market discovery, reset `self._mid_window_price = None` and `self._mid_captured = False`.

## 5. Config (`btcbot/config.py`)

One new field:
- `regime_window: int` — number of recent markets the regime detector tracks. Env var: `BOT_REGIME_WINDOW`, default: 20.

No other config changes. All dynamic adjustments are derived from the choppiness score.

---

## Summary of effects by regime

| Layer | Trending (choppiness=0) | Choppy (choppiness=1) |
|-------|------------------------|-----------------------|
| Fair prob clamp | 0.80 | 0.65 |
| Warmup | 30s | 90s |
| Position size | 100% Kelly | 40% Kelly |
| Hedge threshold | 0.15 | 0.08 |

## Files changed

| File | Change |
|------|--------|
| `btcbot/regime.py` | New file — RegimeDetector class |
| `btcbot/signal.py` | Add choppiness param to evaluate(), dynamic clamp + warmup |
| `btcbot/risk.py` | Add choppiness param to calc_position_size() and should_hedge() |
| `btcbot/engine.py` | Instantiate RegimeDetector, capture mid-window price, feed detector at resolution, pass choppiness to signal/risk |
| `btcbot/config.py` | Add regime_window field |
