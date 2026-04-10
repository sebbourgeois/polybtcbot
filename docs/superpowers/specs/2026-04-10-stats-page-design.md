# Stats Page — Design Spec

**Date:** 2026-04-10
**Status:** Approved, ready for implementation plan
**Scope:** New `/stats` page on the web dashboard showing realized P&L across selectable time periods (Day, Week, Month, All-Time) with summary tiles and three charts.

---

## Goals

1. Give the operator a glanceable, period-scoped view of realized P&L beyond what `/` and `/history` already show.
2. Expose rollups (day/week/month/all-time) without replacing the existing day-by-day `/history` page.
3. Reuse existing infrastructure: Chart.js 4.4.1, the custom dark-theme CSS tokens, the `market_results` table, and the existing HTMX/Jinja2/FastAPI patterns.
4. Surface both aggregate numbers (tiles) and shape-over-time (equity curve, per-bucket bars, win/loss distribution) for the selected period.

## Non-goals

- Unrealized P&L or open-position tracking (realized only — source is `market_results`, same as existing stats).
- Replacing `/history`. The day-by-day breakdown stays there.
- Cross-period comparisons (e.g., "this week vs last week") — not in scope for v1.
- Client-side date manipulation or timezone toggles. All bucketing is local-system-TZ via SQLite `strftime('…', 'unixepoch', 'localtime')`, matching existing conventions.
- Historical-snapshot caching or a new aggregation table. All queries run against `market_results` on demand.
- Browser/JS test harness. The JS layer is thin and verified manually; Python logic is unit-tested.

## Architecture overview

```
                   ┌──────────────────────────┐
                   │  GET /stats              │
                   │  (renders stats.html)    │
                   └────────────┬─────────────┘
                                │ first-paint: initial_stats embedded as JSON
                                ▼
┌──────────────────────────────────────────────────┐
│  stats.html  (extends base.html)                 │
│   • Header + period toggle                       │
│   • 8 stat tiles                                 │
│   • 3 chart panels (equity / bars / distribution)│
│   • Inline <script> for period switching         │
└────────────┬─────────────────────────────────────┘
             │ fetch('/api/stats?period=X')
             ▼
┌──────────────────────────────────────────────────┐
│  GET /api/stats?period=day|week|month|all        │
│    → repo.period_bounds(period)                  │
│    → repo.stats_summary(conn, since_ts)          │
│    → repo.stats_buckets(conn, since_ts, grain)   │
│    → repo.stats_equity(buckets)  # pure Python   │
│    → JSON blob                                   │
└────────────┬─────────────────────────────────────┘
             │ SQL on market_results
             ▼
           SQLite
```

**Why one endpoint and not four:** all four data products (tiles, equity, bars, distribution) come from the same two underlying queries. Splitting into four endpoints would mean 4× round-trips per period switch and duplicated query surface for no benefit.

**Why derive the equity curve from the bucket rows instead of a separate query:** one fewer SQL round-trip, and the equity curve lines up exactly with the bar chart's x-axis because it's built from the same buckets.

## File changes

**New files:**
- `btcbot/web/templates/stats.html` — Stats page template, extends `base.html`.
- `tests/test_stats.py` — Unit + repo-integration tests for the new aggregation helpers.
- `tests/test_web_stats.py` — FastAPI `TestClient` tests for the `/stats` and `/api/stats` routes. (Or appended to an existing web test file if one already exists — current layout has no `test_web_*`, so a new file.)

**Modified files:**
- `btcbot/web/routes.py` — two new routes: `GET /stats`, `GET /api/stats`.
- `btcbot/storage/repo.py` — new helpers: `period_bounds`, `stats_summary`, `stats_buckets`, `stats_equity`, plus dataclasses `StatsSummaryRow`, `BucketRow`, `EquityPoint`.
- `btcbot/web/templates/base.html` — add a "Stats" link to the top nav.
- `tests/conftest.py` — possibly a new `stats_db` fixture (temp SQLite with `schema.sql` applied and seedable via helper), if reusable across the two new test files.

## Period semantics

Period boundaries are **calendar-based, in local system time**. This matches the existing `_enrich_live_ctx()` "today" calculation at `btcbot/web/routes.py:108-111` and the engine's `today_start` logic at `btcbot/engine.py:416-420`.

| Period   | `since_ts`                                              | Default bucket grain                |
|----------|---------------------------------------------------------|-------------------------------------|
| `day`    | Midnight today (local)                                  | `hour`                              |
| `week`   | Monday 00:00 of current ISO week (local)                | `day`                               |
| `month`  | Day 1 00:00 of current month (local)                    | `day`                               |
| `all`    | `0` (epoch)                                             | `week` if span ≤ 2 years else `month` |

The "all-time" grain is decided at query time by looking at the oldest `resolved_at` in `market_results`. If there are ≤ ~104 weeks of history, use weekly bars; otherwise switch to monthly. Empty DB → return empty buckets and empty equity curve.

## Backend helpers (`btcbot/storage/repo.py`)

### Dataclasses

```python
@dataclass
class StatsSummaryRow:
    net_pnl: float
    trades: int        # count of resolved markets in period
    wins: int          # outcome_correct = 1
    losses: int        # outcome_correct = 0
    hedged: int        # outcome_correct IS NULL (hedge_cost > 0 path)
    win_rate: float    # wins / (wins + losses), 0.0 if denom = 0
    best_market: Optional[tuple[str, float]]   # (slug, pnl) or None
    worst_market: Optional[tuple[str, float]]  # (slug, pnl) or None

@dataclass
class BucketRow:
    bucket: str        # display label, e.g. "2026-04-10", "2026-04-10 14:00", "2026-W15", "2026-04"
    net_pnl: float
    trades: int

@dataclass
class EquityPoint:
    bucket: str        # mirrors BucketRow.bucket
    value: float       # cumulative P&L through this bucket
```

### `period_bounds(period: str) -> tuple[int, str]`

Pure helper — no DB access. Uses `datetime.datetime.now().astimezone()` and `isocalendar()`.

- `day`   → `(midnight_today_ts, "hour")`
- `week`  → `(monday_00_00_ts, "day")`
- `month` → `(first_of_month_00_00_ts, "day")`
- `all`   → `(0, "week")` — a default; the route handler may override the grain to `"month"` after peeking at DB span (see below).

Raises `ValueError` for any other input.

### `oldest_resolved_at(conn) -> Optional[int]`

One-line helper. `SELECT MIN(resolved_at) FROM market_results`. Returns `None` if empty. Used by the route handler to decide the `all` grain and by `stats_buckets` to know where to start zero-filling when `since_ts == 0`.

### `stats_summary(conn, since_ts: int) -> StatsSummaryRow`

One SELECT query against `market_results` filtered by `resolved_at >= since_ts`:

```sql
SELECT
  COALESCE(SUM(net_pnl_usd), 0.0) AS net_pnl,
  COUNT(*) AS trades,
  COALESCE(SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END), 0) AS wins,
  COALESCE(SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END), 0) AS losses,
  COALESCE(SUM(CASE WHEN outcome_correct IS NULL THEN 1 ELSE 0 END), 0) AS hedged
FROM market_results
WHERE resolved_at >= ?
```

Best/worst markets come from two small follow-up queries (`ORDER BY net_pnl_usd DESC LIMIT 1` and `ASC LIMIT 1`) against the same filter. If the period has zero rows, both return `None`.

`win_rate` is computed in Python: `wins / (wins + losses)` if denominator > 0, else `0.0`.

### `stats_buckets(conn, since_ts: int, grain: Literal["hour","day","week","month"]) -> list[BucketRow]`

Grain → SQLite `strftime` format:
- `hour`  → `%Y-%m-%d %H:00`
- `day`   → `%Y-%m-%d`
- `week`  → `%Y-W%W`
- `month` → `%Y-%m`

Query shape (parameterized by grain format):

```sql
SELECT strftime(?, resolved_at, 'unixepoch', 'localtime') AS bucket,
       COALESCE(SUM(net_pnl_usd), 0.0) AS net_pnl,
       COUNT(*) AS trades
FROM market_results
WHERE resolved_at >= ?
GROUP BY bucket
ORDER BY bucket ASC
```

**Zero-filling:** after reading the rows, the helper walks forward in grain-sized steps up to "now" and inserts `BucketRow(bucket=label, net_pnl=0.0, trades=0)` for every bucket missing from the SQL result. The walk start point is:
- `since_ts` if `since_ts > 0` (normal case for day/week/month)
- the result of `oldest_resolved_at(conn)` if `since_ts == 0` (all-time case)
- if `since_ts == 0` **and** the DB is empty, return `[]` (no walk).

Zero-filling matters most for the Day view where early-morning hours would otherwise produce a sparse x-axis.

### `stats_equity(buckets: list[BucketRow]) -> list[EquityPoint]`

Pure Python. Running cumulative sum over `bucket.net_pnl`. Returns one `EquityPoint` per input bucket. Empty input → empty output.

## API endpoint (`btcbot/web/routes.py`)

### `GET /api/stats?period=day|week|month|all`

Handler steps:

1. Read `period` from query params, default `"day"`.
2. If `period not in {"day","week","month","all"}` → `raise HTTPException(400, "invalid period")`.
3. `since_ts, grain = repo.period_bounds(period)`.
4. Open connection.
5. If `period == "all"`: call `oldest_resolved_at(conn)`; if the returned timestamp places the oldest data more than ~2 years in the past (`now - oldest > 2 * 365 * 86400`), override `grain = "month"`. Otherwise keep the default `"week"`.
6. Call `stats_summary(conn, since_ts)` and `stats_buckets(conn, since_ts, grain)` sequentially.
7. `equity = stats_equity(buckets)`.
8. Return JSON (shape below).

### Response shape

```json
{
  "period": "week",
  "since_ts": 1712620800,
  "grain": "day",
  "tiles": {
    "net_pnl": 142.37,
    "trades": 58,
    "win_rate": 0.6379,
    "wins": 37,
    "losses": 21,
    "hedged": 0,
    "best_market": {"slug": "btc-up-or-down-2026-04-09-14-00", "pnl": 18.22},
    "worst_market": {"slug": "btc-up-or-down-2026-04-08-22-05", "pnl": -9.44}
  },
  "equity": [
    {"bucket": "2026-04-06", "value": 0.0},
    {"bucket": "2026-04-07", "value": 24.10}
  ],
  "bars": [
    {"bucket": "2026-04-06", "net_pnl": 0.0, "trades": 0},
    {"bucket": "2026-04-07", "net_pnl": 24.10, "trades": 12}
  ],
  "distribution": {"wins": 37, "losses": 21, "hedged": 0}
}
```

- `best_market` / `worst_market` are `null` when the period has no resolved markets.
- `win_rate` is a float in [0, 1]. Client formats as percentage.
- `bucket` strings are already display-ready — client uses them as x-axis labels as-is, no date parsing.
- Empty period → `equity` and `bars` are empty arrays; tiles contain zeros and null market fields.

### `GET /stats`

Server-renders `stats.html`. Computes the initial (Day) payload by calling the same backend helpers, serializes it to JSON, and passes it to the template as `initial_stats`. The template embeds it as:

```html
<script type="application/json" id="initial-stats">{{ initial_stats|tojson|safe }}</script>
```

The JS reads this on load instead of firing an initial `fetch` — avoids a flash of empty content on first paint.

## Template (`btcbot/web/templates/stats.html`)

Extends `base.html`. Vertical flow:

1. **Header**
   ```html
   <h1>Stats</h1>
   <h2 class="muted">Realized P&L across selected period</h2>
   ```

2. **Period toggle** — segmented button group directly under the header:
   ```html
   <div class="period-toggle" role="tablist">
     <button data-period="day"   class="active">Day</button>
     <button data-period="week">Week</button>
     <button data-period="month">Month</button>
     <button data-period="all">All-Time</button>
   </div>
   ```
   Styling reuses existing CSS tokens (`--panel`, `--border`, `--accent`). Active state = blue accent background.

3. **Stat tiles row** — uses the existing `.stats-row` / `.stat` pattern. Eight tiles in this order, each with a stable `id` so JS updates the value in place:

   | # | id                   | Label        | Format               | Color source             |
   |---|----------------------|--------------|----------------------|--------------------------|
   | 1 | `tile-net-pnl`       | Net P&L      | `+$142.37` / `−$9.44`| `.pos` / `.neg`          |
   | 2 | `tile-trades`        | Trades       | `58`                 | default                  |
   | 3 | `tile-win-rate`      | Win Rate     | `63.8%`              | default                  |
   | 4 | `tile-wins`          | Wins         | `37`                 | `.pos`                   |
   | 5 | `tile-losses`        | Losses       | `21`                 | `.neg`                   |
   | 6 | `tile-hedged`        | Hedged       | `0`                  | `.warn`                  |
   | 7 | `tile-best-market`   | Best Market  | `+$18.22` + slug     | `.pos`                   |
   | 8 | `tile-worst-market`  | Worst Market | `−$9.44` + slug      | `.neg`                   |

   "Trades" counts resolved markets in the period (i.e., rows in `market_results`), not individual `trades` table rows. This matches the semantics of `daily_pnl.trades_count` and the existing `trades_today` tile on the dashboard.

   Tiles 7 and 8 are two-line: value on top (standard `.value`), slug below in monospace muted text truncated with `text-overflow: ellipsis`. Null market → show `—` for the value and clear the slug line.

4. **Chart panels** — three stacked panels, each a `.chart-wrap`:
   - **Equity Curve** (`<canvas id="chart-equity">`): line chart with area fill, dark theme, matches `dashboard.html:32-90` styling.
   - **P&L Buckets** (`<canvas id="chart-bars">`): bar chart. Title text per period: "Hourly P&L" / "Daily P&L" / "Daily P&L" / "Weekly P&L" or "Monthly P&L". Bar color per data point: `--pos` if `net_pnl >= 0`, else `--neg`.
   - **Win / Loss / Hedged Distribution** (`<canvas id="chart-distribution">`): Chart.js doughnut. Three segments in `--pos`, `--neg`, `--warn`. Constrained to `max-width: 320px`, centered in its panel. Legend on the right showing "Wins 37 (64%)" etc.

5. **Empty state** — a sibling `<div class="empty-state">No trades in this period</div>` inside each chart panel, hidden by default (`display: none`). When `data.bars.length === 0`, JS hides the `<canvas>` and shows the sibling; reverses on the next non-empty period. Tiles still update to zero values.

6. **Initial data embed** — `<script type="application/json" id="initial-stats">` at the bottom of the content block.

7. **Inline JS** — at the bottom of the content block (see next section).

### Base nav update

In `btcbot/web/templates/base.html`, add a new `<a>` link in the top nav between "History" and any existing right-side items. Label: `Stats`. Route: `/stats`. Uses the existing nav link styling.

## Frontend JS

Single inline `<script>` block at the bottom of `stats.html`. Plain ES modules / vanilla JS, no bundler. Mirrors the inline pattern used in `dashboard.html`.

### State

```js
const charts = { equity: null, bars: null, distribution: null };
let currentPeriod = "day";
```

### Entry point (runs on DOMContentLoaded)

1. `const initial = JSON.parse(document.getElementById("initial-stats").textContent);`
2. `render(initial);` — builds all three charts for the first time and fills the tiles.
3. Attach `click` handlers to every `.period-toggle button` → `switchPeriod(btn.dataset.period)`.

### `switchPeriod(period)`

1. Short-circuit if `period === currentPeriod`.
2. Toggle `.active` classes on the button group.
3. Add `.is-loading` to the chart panels (CSS sets `opacity: 0.5`, disables pointer events). No spinner.
4. `fetch('/api/stats?period=' + period)` → `res.json()`.
5. On success: `render(data)`, remove `.is-loading`, `currentPeriod = period`.
6. On error: remove `.is-loading`, show an inline error message (e.g., a `.error` element near the header), keep previous data in place, `console.error(err)`.

### `render(data)`

Two sub-functions:

**`updateTiles(tiles)`** — direct `textContent` writes on each `tile-*` element. Formatting helpers:
- `formatUsd(n)` → `'+$142.37'` for positive, `'−$9.44'` for negative, `'$0.00'` for zero. Also toggles `.pos` / `.neg` on the `.value` element.
- `formatPct(n)` → `(n * 100).toFixed(1) + '%'`.
- For `best_market` / `worst_market`: if `null`, set value to `—` and clear the slug line. Otherwise set value via `formatUsd` and slug via `textContent`.

**`updateCharts(data)`** — for each chart:
- If `charts.<name> === null`, create it via the corresponding factory and store the instance.
- Otherwise, mutate `chart.data.labels` and `chart.data.datasets[0].data` in place and call `chart.update()`. Chart.js gives smooth animated transitions this way — no destroy/recreate.
- Before updating, check `data.bars.length === 0`; if empty, hide canvases and show `.empty-state` elements instead.

### Chart factories

Three small functions:

- **`createEquityChart(ctx, equity)`** — line chart, filled area, `borderColor: var(--accent)`, gradient fill. `data.labels = equity.map(e => e.bucket)`, `data.datasets[0].data = equity.map(e => e.value)`.
- **`createBarsChart(ctx, bars)`** — bar chart. `backgroundColor` is a per-point function reading `--pos` / `--neg` from computed styles.
- **`createDistributionChart(ctx, dist)`** — doughnut. Labels `['Wins','Losses','Hedged']`, data `[dist.wins, dist.losses, dist.hedged]`, backgrounds `[--pos, --neg, --warn]`. Legend plugin on the right with percentage formatter.

All three use a shared `baseOptions()` helper that returns the dark-theme tooltip / axis config copied from `dashboard.html:48-90`. Inlined, not extracted to a shared JS file — matches the current project convention.

### CSS additions

Minor additions in `btcbot/web/static/app.css` (or inline `<style>` in `stats.html`, whichever matches existing practice; the repo uses `app.css` so additions go there):
- `.period-toggle` — flex row, border, rounded corners, `gap: 0`, button children get hover/active states using existing tokens.
- `.chart-wrap.is-loading` — `opacity: 0.5; pointer-events: none;`.
- `.empty-state` — centered, `color: var(--muted)`, `padding: 40px 0`.
- Two-line tile variant for Best/Worst market (mainly font-size tweaks on the slug subtitle).

## Testing

### New file: `tests/test_stats.py`

**Pure / semi-pure helpers:**

1. `test_period_bounds_day` — monkeypatch `datetime.datetime.now` to a known local instant (e.g., `2026-04-08 14:32`), assert `since_ts` equals midnight of that day and grain is `"hour"`.
2. `test_period_bounds_week` — same pattern; assert `since_ts` equals Monday 00:00 of the ISO week.
3. `test_period_bounds_month` — assert `since_ts` equals day-1 00:00.
4. `test_period_bounds_all` — assert `since_ts == 0`.
5. `test_period_bounds_invalid` — `ValueError` on unknown period.
6. `test_stats_equity_cumulative` — given `[BucketRow("a", 10.0, 1), BucketRow("b", -3.0, 2), BucketRow("c", 5.0, 0)]`, assert equity is `[10.0, 7.0, 12.0]` with matching bucket labels.
7. `test_stats_equity_empty` — empty input → empty output.

**DB integration (temp SQLite):**

8. `test_stats_summary_happy_path` — seed 5 `market_results` rows with known P&L and outcomes, call `stats_summary`, assert totals, win/loss/hedge counts, win rate, best/worst slugs.
9. `test_stats_summary_empty` — empty DB → zeros and `best_market is None`.
10. `test_stats_summary_time_filter` — seed rows both before and after `since_ts`, assert only newer rows are counted.
11. `test_stats_buckets_zero_fill` — seed rows on days 1, 3, 5 with `grain="day"` and a `since_ts` at day 1; assert the returned buckets include day 2 and day 4 with zero P&L / zero trades.
12. `test_stats_buckets_hour_grain` — seed rows across multiple hours on a single day; assert hourly bucketing works and zero-fills missing hours.

### New file: `tests/test_web_stats.py`

FastAPI `TestClient`. Uses a temp DB fixture. Tests:

13. `test_get_stats_html` — `GET /stats` → 200, response contains `id="initial-stats"` and a valid JSON blob inside.
14. `test_get_api_stats_day` — seed a couple of `market_results` rows from today, `GET /api/stats?period=day`, assert schema matches the contract and values are correct.
15. `test_get_api_stats_all` — seed rows across multiple weeks, `GET /api/stats?period=all`, assert the bucket grain is one of `week`/`month` depending on span.
16. `test_get_api_stats_empty` — empty DB, `GET /api/stats?period=week`, assert tiles are zeros, `equity == []`, `bars == []`, `best_market is None`.
17. `test_get_api_stats_invalid_period` — `GET /api/stats?period=forever` → 400.

### Conftest additions

A new fixture in `tests/conftest.py` (or a co-located one in the new test files) that:
- Creates a tmp-path SQLite file.
- Applies `btcbot/storage/schema.sql`.
- Yields an open async connection.
- Provides a small helper `insert_market_result(conn, slug, net_pnl, outcome_correct, resolved_at)` to keep seed code terse.

### Not tested

- Chart.js rendering (no browser harness in the project; not worth adding one for this).
- The JS period-switching logic (thin, mostly delegates to Chart.js; regressions would be visible on first open).
- CSS / styling.

### Manual verification (per `CLAUDE.md` "trading bot" rule — validate against live data)

1. Rebuild / restart the FastAPI server.
2. Open `/stats`. Confirm the Day view renders with data from the actual DB.
3. Toggle through Week → Month → All-Time. Confirm tiles and all three charts update smoothly.
4. Confirm a period with no trades shows the empty state on all three charts and zero tiles.
5. Confirm the "Stats" link appears in the top nav on every page (`/`, `/trades`, `/history`, `/stats`).
6. Confirm tile values match what the existing `/` and `/history` pages show for the same time window (sanity cross-check).

## Open questions / follow-ups

None blocking implementation. Potential v2 work (not in scope):
- Cross-period comparison (e.g., "this week vs last week delta").
- Export period data as CSV.
- Include unrealized P&L for currently-open markets.
- A "custom range" picker instead of fixed periods.
