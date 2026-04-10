# Stats Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `/stats` dashboard page showing realized P&L across four calendar-based time periods (Day, Week, Month, All-Time) with summary tiles and three charts (equity curve, P&L bars, win/loss doughnut).

**Architecture:** A single FastAPI JSON endpoint (`/api/stats?period=X`) returns all data needed to render the page. Backend aggregation lives in new helpers in `btcbot/storage/repo.py` (one SQL query for summary, one for buckets, one pure-Python pass for the equity curve). The page server-renders the Day view on first paint and uses plain vanilla JS + `fetch` to switch periods and redraw Chart.js instances in place.

**Tech Stack:** FastAPI, Jinja2, aiosqlite, Chart.js 4.4.1 (already loaded via CDN in `base.html`), pytest + FastAPI `TestClient`, vanilla JS (no bundler).

**Design spec:** `docs/superpowers/specs/2026-04-10-stats-page-design.md`

---

## File Structure

**New files:**
- `btcbot/web/templates/stats.html` — the Stats page template.
- `tests/test_stats.py` — unit + DB-integration tests for the new `repo.py` helpers.
- `tests/test_web_stats.py` — FastAPI `TestClient` tests for the new routes.

**Modified files:**
- `btcbot/storage/repo.py` — add dataclasses `StatsSummaryRow`, `BucketRow`, `EquityPoint`, and helpers `period_bounds`, `oldest_resolved_at`, `stats_summary`, `stats_buckets`, `stats_equity`.
- `btcbot/web/routes.py` — add `GET /stats` HTML route and `GET /api/stats` JSON endpoint.
- `btcbot/web/templates/base.html` — add a "Stats" link to the top nav.
- `btcbot/web/static/app.css` — add `.period-toggle` and `.chart-wrap.is-loading` styles.

**File size reasoning:** `repo.py` is the largest modification (~200 lines added). It already contains many similar helpers, so adding more fits the existing pattern. `stats.html` will be ~250 lines (template + inline JS). All other edits are small.

---

## Task 1: Add `period_bounds` helper (pure) with tests

**Files:**
- Create: `tests/test_stats.py`
- Modify: `btcbot/storage/repo.py` — append new section at end of file.

- [ ] **Step 1.1: Create the failing test file**

Create `tests/test_stats.py` with this initial content:

```python
"""Tests for the stats page backend helpers in btcbot.storage.repo."""

from __future__ import annotations

import datetime
import pytest

from btcbot.storage import repo


def _freeze(monkeypatch, iso: str) -> None:
    """Freeze repo's local-time "now" to the given ISO instant."""
    frozen = datetime.datetime.fromisoformat(iso)
    monkeypatch.setattr(repo, "_now_local", lambda: frozen)


# ── period_bounds ─────────────────────────────────────────────────────


def test_period_bounds_day(monkeypatch):
    # Wednesday 2026-04-08 14:32:11 local time
    _freeze(monkeypatch, "2026-04-08T14:32:11")
    since_ts, grain = repo.period_bounds("day")
    expected = int(datetime.datetime(2026, 4, 8, 0, 0, 0).timestamp())
    assert since_ts == expected
    assert grain == "hour"


def test_period_bounds_week(monkeypatch):
    # Wednesday 2026-04-08 14:32 — ISO week starts on Monday 2026-04-06
    _freeze(monkeypatch, "2026-04-08T14:32:11")
    since_ts, grain = repo.period_bounds("week")
    expected = int(datetime.datetime(2026, 4, 6, 0, 0, 0).timestamp())
    assert since_ts == expected
    assert grain == "day"


def test_period_bounds_month(monkeypatch):
    _freeze(monkeypatch, "2026-04-08T14:32:11")
    since_ts, grain = repo.period_bounds("month")
    expected = int(datetime.datetime(2026, 4, 1, 0, 0, 0).timestamp())
    assert since_ts == expected
    assert grain == "day"


def test_period_bounds_all(monkeypatch):
    _freeze(monkeypatch, "2026-04-08T14:32:11")
    since_ts, grain = repo.period_bounds("all")
    assert since_ts == 0
    assert grain == "week"  # default; route handler may override to "month"


def test_period_bounds_invalid():
    with pytest.raises(ValueError):
        repo.period_bounds("forever")
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/test_stats.py -v`
Expected: all five tests FAIL with `AttributeError: module 'btcbot.storage.repo' has no attribute 'period_bounds'` (or similar).

- [ ] **Step 1.3: Implement `period_bounds` and `_now_local`**

Append to `btcbot/storage/repo.py`:

```python
# ── Stats page helpers ─────────────────────────────────────────────────


def _now_local() -> datetime.datetime:
    """Return naive local-time "now". Isolated for test monkey-patching."""
    return datetime.datetime.now()


def period_bounds(period: str) -> tuple[int, str]:
    """Return (since_ts, default_grain) for a Stats-page period.

    Periods are calendar-based in local system time:
      - day   → midnight today → hourly buckets
      - week  → Monday 00:00 of current ISO week → daily buckets
      - month → day 1 00:00 of current month → daily buckets
      - all   → epoch (0) → weekly buckets (route handler may override to monthly)
    """
    if period == "day":
        now = _now_local()
        start = datetime.datetime(now.year, now.month, now.day)
        return int(start.timestamp()), "hour"
    if period == "week":
        now = _now_local()
        today = datetime.date(now.year, now.month, now.day)
        monday = today - datetime.timedelta(days=today.weekday())  # weekday: Mon=0
        start = datetime.datetime(monday.year, monday.month, monday.day)
        return int(start.timestamp()), "day"
    if period == "month":
        now = _now_local()
        start = datetime.datetime(now.year, now.month, 1)
        return int(start.timestamp()), "day"
    if period == "all":
        return 0, "week"
    raise ValueError(f"unknown period: {period!r}")
```

Also add `import datetime` near the top of `repo.py` if it's not already imported. Check by reading the current imports; `repo.py:1-12` already imports `time` but not `datetime`. Add `import datetime` on a new line after `import time`.

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/test_stats.py -v`
Expected: all five tests PASS.

- [ ] **Step 1.5: Commit**

```bash
git add tests/test_stats.py btcbot/storage/repo.py
git commit -m "stats: add period_bounds helper for calendar-based period boundaries"
```

---

## Task 2: Add `oldest_resolved_at`, dataclass, and `stats_summary` with tests

**Files:**
- Modify: `tests/test_stats.py` — append DB-integration tests and a `stats_db` fixture.
- Modify: `btcbot/storage/repo.py` — append dataclass and helpers.

- [ ] **Step 2.1: Add the `stats_db` fixture and `insert_result` helper to the test file**

Append to `tests/test_stats.py` (before the existing `_freeze` helper is fine — just put it before the first test that uses it):

```python
import sqlite3
from pathlib import Path

import aiosqlite
import pytest_asyncio

from btcbot.storage import db as storage_db


@pytest_asyncio.fixture
async def stats_db(tmp_path: Path):
    """Fresh SQLite DB with schema applied. Yields an open aiosqlite connection."""
    db_path = tmp_path / "stats_test.db"
    # Apply schema synchronously via stdlib sqlite3 to avoid event-loop complications
    schema = (Path(storage_db.__file__).parent / "schema.sql").read_text()
    conn_sync = sqlite3.connect(db_path)
    conn_sync.executescript(schema)
    conn_sync.commit()
    conn_sync.close()

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def _insert_market_and_result(
    conn: aiosqlite.Connection,
    slug: str,
    net_pnl: float,
    outcome_correct: int | None,
    resolved_at: int,
    *,
    hedge_cost: float = 0.0,
) -> None:
    """Insert a market row (required by FK) and its market_result."""
    await conn.execute(
        """INSERT INTO markets
             (slug, condition_id, up_token_id, down_token_id,
              start_ts, end_ts, discovered_at, resolved_at)
           VALUES (?, ?, 'utok', 'dtok', ?, ?, ?, ?)""",
        (slug, f"cond_{slug}", resolved_at - 300, resolved_at, resolved_at - 600, resolved_at),
    )
    await conn.execute(
        """INSERT INTO market_results
             (market_slug, entry_cost_usd, hedge_cost_usd, payout_usd,
              net_pnl_usd, outcome_correct, resolved_at)
           VALUES (?, 10.0, ?, ?, ?, ?, ?)""",
        (slug, hedge_cost, max(10.0 + net_pnl + hedge_cost, 0.0), net_pnl, outcome_correct, resolved_at),
    )
    await conn.commit()
```

Then append the new tests for `oldest_resolved_at` and `stats_summary`:

```python
# ── oldest_resolved_at ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_oldest_resolved_at_empty(stats_db):
    assert await repo.oldest_resolved_at(stats_db) is None


@pytest.mark.asyncio
async def test_oldest_resolved_at_returns_min(stats_db):
    await _insert_market_and_result(stats_db, "m1", 5.0, 1, 1700000000)
    await _insert_market_and_result(stats_db, "m2", -2.0, 0, 1700100000)
    await _insert_market_and_result(stats_db, "m3", 1.0, 1, 1699900000)
    assert await repo.oldest_resolved_at(stats_db) == 1699900000


# ── stats_summary ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_summary_empty(stats_db):
    row = await repo.stats_summary(stats_db, since_ts=0)
    assert row.net_pnl == 0.0
    assert row.trades == 0
    assert row.wins == 0
    assert row.losses == 0
    assert row.hedged == 0
    assert row.win_rate == 0.0
    assert row.best_market is None
    assert row.worst_market is None


@pytest.mark.asyncio
async def test_stats_summary_happy_path(stats_db):
    # 3 wins, 2 losses, 1 hedged (outcome_correct is NULL)
    await _insert_market_and_result(stats_db, "win-big", 18.22, 1, 1700000000)
    await _insert_market_and_result(stats_db, "win-small", 3.00, 1, 1700000100)
    await _insert_market_and_result(stats_db, "win-mid", 5.50, 1, 1700000200)
    await _insert_market_and_result(stats_db, "loss-big", -9.44, 0, 1700000300)
    await _insert_market_and_result(stats_db, "loss-small", -1.00, 0, 1700000400)
    await _insert_market_and_result(
        stats_db, "hedged-mkt", 0.10, None, 1700000500, hedge_cost=5.0
    )

    row = await repo.stats_summary(stats_db, since_ts=0)
    assert row.net_pnl == pytest.approx(18.22 + 3.00 + 5.50 - 9.44 - 1.00 + 0.10)
    assert row.trades == 6
    assert row.wins == 3
    assert row.losses == 2
    assert row.hedged == 1
    assert row.win_rate == pytest.approx(3 / (3 + 2))  # excludes hedged
    assert row.best_market == ("win-big", 18.22)
    assert row.worst_market == ("loss-big", -9.44)


@pytest.mark.asyncio
async def test_stats_summary_time_filter(stats_db):
    # Three rows, only the last two are after since_ts.
    await _insert_market_and_result(stats_db, "old", 100.0, 1, 1600000000)
    await _insert_market_and_result(stats_db, "new-a", 5.0, 1, 1700000000)
    await _insert_market_and_result(stats_db, "new-b", -2.0, 0, 1700100000)

    row = await repo.stats_summary(stats_db, since_ts=1699000000)
    assert row.net_pnl == pytest.approx(3.0)
    assert row.trades == 2
    assert row.wins == 1
    assert row.losses == 1
    assert row.hedged == 0
    assert row.best_market == ("new-a", 5.0)
    assert row.worst_market == ("new-b", -2.0)
```

Note: the project uses `pytest-asyncio`. If it's not yet a dependency, add a note below; otherwise the `@pytest.mark.asyncio` decorators will work.

- [ ] **Step 2.2: Verify pytest-asyncio is available**

Run: `python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"`
Expected: a version number prints.

If it errors with `ModuleNotFoundError`, install it via the project's normal dependency mechanism (`uv add pytest-asyncio` or `pip install pytest-asyncio` into the active venv) and also add `asyncio_mode = "auto"` to the `[tool.pytest.ini_options]` section of `pyproject.toml` if present, or add a `pytest.ini` / `pyproject.toml` entry. Ask the user before modifying project dependencies.

- [ ] **Step 2.3: Run tests to verify they fail**

Run: `pytest tests/test_stats.py -v -k "oldest or summary"`
Expected: the four new tests FAIL with `AttributeError: module 'btcbot.storage.repo' has no attribute 'oldest_resolved_at'` or `'stats_summary'`.

- [ ] **Step 2.4: Implement `oldest_resolved_at`, `StatsSummaryRow`, and `stats_summary`**

Append to `btcbot/storage/repo.py` (below the `period_bounds` function added in Task 1):

```python
@dataclass
class StatsSummaryRow:
    net_pnl: float
    trades: int
    wins: int
    losses: int
    hedged: int
    win_rate: float  # wins / (wins + losses), 0.0 if denom == 0
    best_market: tuple[str, float] | None  # (slug, net_pnl_usd) or None
    worst_market: tuple[str, float] | None


async def oldest_resolved_at(conn: aiosqlite.Connection) -> int | None:
    """Return the earliest resolved_at timestamp in market_results, or None if empty."""
    cur = await conn.execute(
        "SELECT MIN(resolved_at) FROM market_results WHERE resolved_at IS NOT NULL"
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def stats_summary(
    conn: aiosqlite.Connection, since_ts: int
) -> StatsSummaryRow:
    """Aggregate market_results for rows with resolved_at >= since_ts."""
    cur = await conn.execute(
        """SELECT
             COALESCE(SUM(net_pnl_usd), 0.0) AS net_pnl,
             COUNT(*) AS trades,
             COALESCE(SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END), 0) AS wins,
             COALESCE(SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END), 0) AS losses,
             COALESCE(SUM(CASE WHEN outcome_correct IS NULL THEN 1 ELSE 0 END), 0) AS hedged
           FROM market_results
           WHERE resolved_at >= ?""",
        (since_ts,),
    )
    row = await cur.fetchone()
    net_pnl = float(row["net_pnl"])
    trades = int(row["trades"])
    wins = int(row["wins"])
    losses = int(row["losses"])
    hedged = int(row["hedged"])
    denom = wins + losses
    win_rate = (wins / denom) if denom > 0 else 0.0

    best_market: tuple[str, float] | None = None
    worst_market: tuple[str, float] | None = None
    if trades > 0:
        cur_best = await conn.execute(
            """SELECT market_slug, net_pnl_usd FROM market_results
               WHERE resolved_at >= ?
               ORDER BY net_pnl_usd DESC LIMIT 1""",
            (since_ts,),
        )
        brow = await cur_best.fetchone()
        if brow is not None:
            best_market = (str(brow["market_slug"]), float(brow["net_pnl_usd"]))

        cur_worst = await conn.execute(
            """SELECT market_slug, net_pnl_usd FROM market_results
               WHERE resolved_at >= ?
               ORDER BY net_pnl_usd ASC LIMIT 1""",
            (since_ts,),
        )
        wrow = await cur_worst.fetchone()
        if wrow is not None:
            worst_market = (str(wrow["market_slug"]), float(wrow["net_pnl_usd"]))

    return StatsSummaryRow(
        net_pnl=round(net_pnl, 2),
        trades=trades,
        wins=wins,
        losses=losses,
        hedged=hedged,
        win_rate=win_rate,
        best_market=best_market,
        worst_market=worst_market,
    )
```

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `pytest tests/test_stats.py -v`
Expected: all 9 tests PASS (5 from Task 1 + 4 new).

- [ ] **Step 2.6: Commit**

```bash
git add tests/test_stats.py btcbot/storage/repo.py
git commit -m "stats: add oldest_resolved_at and stats_summary repo helpers"
```

---

## Task 3: Add `stats_buckets` and `stats_equity` with tests

**Files:**
- Modify: `tests/test_stats.py` — append bucket/equity tests.
- Modify: `btcbot/storage/repo.py` — append dataclasses and helpers.

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/test_stats.py`:

```python
# ── stats_equity (pure) ───────────────────────────────────────────────


def test_stats_equity_empty():
    assert repo.stats_equity([]) == []


def test_stats_equity_cumulative():
    buckets = [
        repo.BucketRow(bucket="a", net_pnl=10.0, trades=1),
        repo.BucketRow(bucket="b", net_pnl=-3.0, trades=2),
        repo.BucketRow(bucket="c", net_pnl=5.0, trades=0),
    ]
    equity = repo.stats_equity(buckets)
    assert equity == [
        repo.EquityPoint(bucket="a", value=10.0),
        repo.EquityPoint(bucket="b", value=7.0),
        repo.EquityPoint(bucket="c", value=12.0),
    ]


# ── stats_buckets (DB-backed) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_buckets_day_grain_zero_fills(stats_db, monkeypatch):
    """Seed trades on day 1 and day 3; day 2 should be zero-filled."""
    # Freeze "now" to a known instant so the zero-fill walk has a deterministic end.
    _freeze(monkeypatch, "2026-04-10T10:00:00")

    def ts(y, m, d, h=12):
        return int(datetime.datetime(y, m, d, h, 0, 0).timestamp())

    await _insert_market_and_result(stats_db, "a", 10.0, 1, ts(2026, 4, 8))
    await _insert_market_and_result(stats_db, "b", -4.0, 0, ts(2026, 4, 10))

    since_ts = int(datetime.datetime(2026, 4, 8, 0, 0, 0).timestamp())
    buckets = await repo.stats_buckets(stats_db, since_ts=since_ts, grain="day")

    labels = [b.bucket for b in buckets]
    assert labels == ["2026-04-08", "2026-04-09", "2026-04-10"]

    pnls = {b.bucket: b.net_pnl for b in buckets}
    assert pnls["2026-04-08"] == pytest.approx(10.0)
    assert pnls["2026-04-09"] == pytest.approx(0.0)
    assert pnls["2026-04-10"] == pytest.approx(-4.0)

    trades = {b.bucket: b.trades for b in buckets}
    assert trades["2026-04-09"] == 0


@pytest.mark.asyncio
async def test_stats_buckets_hour_grain(stats_db, monkeypatch):
    _freeze(monkeypatch, "2026-04-10T05:00:00")

    def ts(hour):
        return int(datetime.datetime(2026, 4, 10, hour, 15, 0).timestamp())

    await _insert_market_and_result(stats_db, "h1", 2.0, 1, ts(1))
    await _insert_market_and_result(stats_db, "h2", 3.0, 1, ts(1))  # same hour
    await _insert_market_and_result(stats_db, "h3", -1.5, 0, ts(3))

    since_ts = int(datetime.datetime(2026, 4, 10, 0, 0, 0).timestamp())
    buckets = await repo.stats_buckets(stats_db, since_ts=since_ts, grain="hour")

    labels = [b.bucket for b in buckets]
    # Zero-filled from 00:00 through 05:00 (frozen "now")
    assert labels == [
        "2026-04-10 00:00",
        "2026-04-10 01:00",
        "2026-04-10 02:00",
        "2026-04-10 03:00",
        "2026-04-10 04:00",
        "2026-04-10 05:00",
    ]

    pnls = {b.bucket: b.net_pnl for b in buckets}
    assert pnls["2026-04-10 01:00"] == pytest.approx(5.0)
    assert pnls["2026-04-10 03:00"] == pytest.approx(-1.5)
    assert pnls["2026-04-10 00:00"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_stats_buckets_week_grain_rollup(stats_db, monkeypatch):
    _freeze(monkeypatch, "2026-04-10T10:00:00")

    def ts(y, m, d):
        return int(datetime.datetime(y, m, d, 12, 0, 0).timestamp())

    # Two days in ISO week 2026-W14 (Mon 2026-03-30 .. Sun 2026-04-05)
    await _insert_market_and_result(stats_db, "w1a", 1.0, 1, ts(2026, 3, 30))
    await _insert_market_and_result(stats_db, "w1b", 4.0, 1, ts(2026, 4, 2))
    # One day in ISO week 2026-W15 (Mon 2026-04-06 .. Sun 2026-04-12)
    await _insert_market_and_result(stats_db, "w2", -2.0, 0, ts(2026, 4, 8))

    since_ts = int(datetime.datetime(2026, 3, 30, 0, 0, 0).timestamp())
    buckets = await repo.stats_buckets(stats_db, since_ts=since_ts, grain="week")

    labels = [b.bucket for b in buckets]
    assert labels == ["2026-W14", "2026-W15"]
    pnls = {b.bucket: b.net_pnl for b in buckets}
    assert pnls["2026-W14"] == pytest.approx(5.0)
    assert pnls["2026-W15"] == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_stats_buckets_empty_since_zero(stats_db, monkeypatch):
    """Empty DB with since_ts=0 returns an empty list (no walk)."""
    _freeze(monkeypatch, "2026-04-10T10:00:00")
    buckets = await repo.stats_buckets(stats_db, since_ts=0, grain="week")
    assert buckets == []
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/test_stats.py -v -k "equity or buckets"`
Expected: 6 new tests FAIL with `AttributeError: module 'btcbot.storage.repo' has no attribute 'stats_buckets'` (or similar). `test_stats_equity_*` will also fail on `repo.BucketRow` / `repo.EquityPoint` access.

- [ ] **Step 3.3: Implement `BucketRow`, `EquityPoint`, `stats_equity`, and `stats_buckets`**

Append to `btcbot/storage/repo.py` (below `stats_summary`):

```python
@dataclass
class BucketRow:
    bucket: str   # display label, e.g. "2026-04-10", "2026-04-10 14:00", "2026-W15", "2026-04"
    net_pnl: float
    trades: int


@dataclass
class EquityPoint:
    bucket: str
    value: float


def stats_equity(buckets: list[BucketRow]) -> list[EquityPoint]:
    """Running cumulative sum over bucket P&L. Pure function."""
    points: list[EquityPoint] = []
    running = 0.0
    for b in buckets:
        running += b.net_pnl
        points.append(EquityPoint(bucket=b.bucket, value=round(running, 2)))
    return points


def _expected_bucket_labels(
    walk_start_ts: int, now_ts: int, grain: str
) -> list[str]:
    """Enumerate every expected bucket label from walk_start through now (inclusive)."""
    start = datetime.datetime.fromtimestamp(walk_start_ts)
    end = datetime.datetime.fromtimestamp(now_ts)
    labels: list[str] = []

    if grain == "hour":
        t = start.replace(minute=0, second=0, microsecond=0)
        while t <= end:
            labels.append(t.strftime("%Y-%m-%d %H:00"))
            t += datetime.timedelta(hours=1)
        return labels

    if grain == "day":
        t = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while t <= end:
            labels.append(t.strftime("%Y-%m-%d"))
            t += datetime.timedelta(days=1)
        return labels

    if grain == "week":
        # Walk day-by-day and dedupe by ISO-week label. Simpler than aligning to Monday.
        t = start.replace(hour=0, minute=0, second=0, microsecond=0)
        seen: set[str] = set()
        while t <= end:
            iso_year, iso_week, _ = t.isocalendar()
            label = f"{iso_year}-W{iso_week:02d}"
            if label not in seen:
                seen.add(label)
                labels.append(label)
            t += datetime.timedelta(days=1)
        return labels

    if grain == "month":
        t = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while t <= end:
            labels.append(f"{t.year:04d}-{t.month:02d}")
            if t.month == 12:
                t = t.replace(year=t.year + 1, month=1)
            else:
                t = t.replace(month=t.month + 1)
        return labels

    raise ValueError(f"unknown grain: {grain!r}")


def _label_from_day(day_str: str, grain: str) -> str:
    """Given a '%Y-%m-%d' string, return the bucket label at the requested grain."""
    d = datetime.date.fromisoformat(day_str)
    if grain == "day":
        return day_str
    if grain == "week":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if grain == "month":
        return f"{d.year:04d}-{d.month:02d}"
    raise ValueError(f"unexpected grain for day rollup: {grain!r}")


async def stats_buckets(
    conn: aiosqlite.Connection, since_ts: int, grain: str
) -> list[BucketRow]:
    """Aggregate market_results into time buckets of the given grain.

    For hour grain, SQL groups directly by hour string.
    For day/week/month grains, SQL groups by day and Python rolls up into
    the requested grain (avoids SQLite strftime quirks for ISO weeks).
    Missing buckets are zero-filled so the chart has a continuous x-axis.
    """
    if grain not in ("hour", "day", "week", "month"):
        raise ValueError(f"unknown grain: {grain!r}")

    # 1) Query SQL at the correct base grain ──────────────────────────
    if grain == "hour":
        sql_fmt = "%Y-%m-%d %H:00"
    else:
        sql_fmt = "%Y-%m-%d"  # day-level; we roll up in Python for week/month

    cur = await conn.execute(
        f"""SELECT
              strftime(?, resolved_at, 'unixepoch', 'localtime') AS bucket,
              COALESCE(SUM(net_pnl_usd), 0.0) AS net_pnl,
              COUNT(*) AS trades
            FROM market_results
            WHERE resolved_at >= ?
            GROUP BY bucket
            ORDER BY bucket ASC""",
        (sql_fmt, since_ts),
    )
    raw_rows = await cur.fetchall()

    # 2) Roll up day-level rows into week/month if needed ─────────────
    agg: dict[str, tuple[float, int]] = {}  # label -> (net_pnl, trades)
    for row in raw_rows:
        raw_label = str(row["bucket"])
        if grain in ("hour", "day"):
            label = raw_label
        else:
            label = _label_from_day(raw_label, grain)
        pnl, tr = agg.get(label, (0.0, 0))
        agg[label] = (pnl + float(row["net_pnl"]), tr + int(row["trades"]))

    # 3) Determine walk start for zero-fill ───────────────────────────
    if since_ts > 0:
        walk_start = since_ts
    else:
        oldest = await oldest_resolved_at(conn)
        if oldest is None:
            return []  # empty DB and all-time view → no buckets
        walk_start = oldest

    now_ts = int(_now_local().timestamp())

    # 4) Generate expected labels and zero-fill ───────────────────────
    expected = _expected_bucket_labels(walk_start, now_ts, grain)
    out: list[BucketRow] = []
    for label in expected:
        pnl, tr = agg.get(label, (0.0, 0))
        out.append(BucketRow(bucket=label, net_pnl=round(pnl, 2), trades=tr))
    return out
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `pytest tests/test_stats.py -v`
Expected: all 15 tests PASS (5 + 4 + 6).

- [ ] **Step 3.5: Commit**

```bash
git add tests/test_stats.py btcbot/storage/repo.py
git commit -m "stats: add stats_buckets and stats_equity with zero-fill and ISO week rollup"
```

---

## Task 4: Add `/api/stats` JSON endpoint with tests

**Files:**
- Create: `tests/test_web_stats.py`
- Modify: `btcbot/web/routes.py` — add new route.

- [ ] **Step 4.1: Write the failing tests**

Create `tests/test_web_stats.py`:

```python
"""Tests for the /stats HTML page and /api/stats JSON endpoint."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from btcbot.storage import db as storage_db


def _seed(db_path: Path, rows: list[tuple[str, float, int | None, int]]) -> None:
    """Insert (slug, net_pnl, outcome_correct, resolved_at) rows into a DB.

    Uses INSERT OR IGNORE / OR REPLACE so tests can call this multiple times
    on the same DB path (e.g., first to create schema, then to add fixtures).
    """
    schema = (Path(storage_db.__file__).parent / "schema.sql").read_text()
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    for slug, pnl, outcome, resolved_at in rows:
        conn.execute(
            """INSERT OR IGNORE INTO markets
                 (slug, condition_id, up_token_id, down_token_id,
                  start_ts, end_ts, discovered_at, resolved_at)
               VALUES (?, ?, 'utok', 'dtok', ?, ?, ?, ?)""",
            (slug, f"cond_{slug}", resolved_at - 300, resolved_at, resolved_at - 600, resolved_at),
        )
        conn.execute(
            """INSERT OR REPLACE INTO market_results
                 (market_slug, entry_cost_usd, hedge_cost_usd, payout_usd,
                  net_pnl_usd, outcome_correct, resolved_at)
               VALUES (?, 10.0, 0.0, ?, ?, ?, ?)""",
            (slug, max(10.0 + pnl, 0.0), pnl, outcome, resolved_at),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient backed by an isolated SQLite DB.

    This fixture MUST skip the app lifespan. The real lifespan (see
    btcbot/web/app.py:_lifespan) constructs an Engine that connects to
    Binance + Polymarket — starting that during tests would be a disaster.
    TestClient only fires lifespan events when used as a context manager,
    so we instantiate it directly (no ``with`` block) to skip them.
    """
    db_path = tmp_path / "web_stats.db"

    # Point db.connect() at our temp DB. db.py does `from ..config import CONFIG`,
    # so patching db_module.CONFIG.db_path modifies the singleton db.py reads.
    from btcbot.storage import db as _db_module
    monkeypatch.setattr(_db_module.CONFIG, "db_path", db_path)

    # Create an empty DB with schema applied so routes that query succeed.
    _seed(db_path, [])

    from btcbot.web.app import app
    c = TestClient(app)  # NOTE: no `with` — skip lifespan, do not start engine
    c.db_path = db_path  # type: ignore[attr-defined]
    yield c


def test_api_stats_empty_returns_zeros(client):
    r = client.get("/api/stats?period=day")
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "day"
    assert body["grain"] == "hour"
    assert body["tiles"]["net_pnl"] == 0.0
    assert body["tiles"]["trades"] == 0
    assert body["tiles"]["best_market"] is None
    assert body["tiles"]["worst_market"] is None
    assert body["distribution"] == {"wins": 0, "losses": 0, "hedged": 0}
    # equity / bars are zero-filled across today's hours, not empty
    assert isinstance(body["equity"], list)
    assert isinstance(body["bars"], list)


def test_api_stats_all_time_empty_returns_empty_arrays(client):
    """With no data in DB, all-time should return empty arrays (no walk start)."""
    r = client.get("/api/stats?period=all")
    assert r.status_code == 200
    body = r.json()
    assert body["equity"] == []
    assert body["bars"] == []


def test_api_stats_day_happy_path(client):
    # Seed two resolved rows for today
    today_midnight = int(
        datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp()
    )
    _seed(
        client.db_path,
        [
            ("win1", 12.50, 1, today_midnight + 3600),
            ("loss1", -4.00, 0, today_midnight + 7200),
        ],
    )
    r = client.get("/api/stats?period=day")
    assert r.status_code == 200
    body = r.json()
    assert body["tiles"]["trades"] == 2
    assert body["tiles"]["net_pnl"] == pytest.approx(8.5)
    assert body["tiles"]["wins"] == 1
    assert body["tiles"]["losses"] == 1
    assert body["tiles"]["best_market"] == {"slug": "win1", "pnl": 12.5}
    assert body["tiles"]["worst_market"] == {"slug": "loss1", "pnl": -4.0}
    # Equity curve ends at net_pnl
    assert body["equity"][-1]["value"] == pytest.approx(8.5)


def test_api_stats_invalid_period_returns_400(client):
    r = client.get("/api/stats?period=forever")
    assert r.status_code == 400


def test_get_stats_page_returns_html_with_initial_data(client):
    r = client.get("/stats")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="initial-stats"' in r.text
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/test_web_stats.py -v`
Expected: all 5 tests FAIL with 404 on `/api/stats` and `/stats` (routes don't exist yet).

- [ ] **Step 4.3: Implement `/api/stats` and `/stats`**

First, update the imports at the top of `btcbot/web/routes.py` (around line 10). Change:

```python
from fastapi import APIRouter, Request
```

to:

```python
from fastapi import APIRouter, HTTPException, Request
```

Then, at the end of `btcbot/web/routes.py`, add:

```python
# ── Stats page ─────────────────────────────────────────────────────────


_ALL_TIME_MONTHLY_THRESHOLD_SEC = 2 * 365 * 86400  # switch to monthly grain if data spans > 2y


async def _build_stats_payload(period: str) -> dict:
    """Shared backend logic for both /stats (HTML) and /api/stats (JSON)."""
    since_ts, grain = repo.period_bounds(period)
    async with connect() as conn:
        if period == "all":
            oldest = await repo.oldest_resolved_at(conn)
            if oldest is not None and (int(time.time()) - oldest) > _ALL_TIME_MONTHLY_THRESHOLD_SEC:
                grain = "month"
        summary = await repo.stats_summary(conn, since_ts)
        buckets = await repo.stats_buckets(conn, since_ts, grain)
    equity = repo.stats_equity(buckets)

    tiles = {
        "net_pnl": round(summary.net_pnl, 2),
        "trades": summary.trades,
        "win_rate": round(summary.win_rate, 4),
        "wins": summary.wins,
        "losses": summary.losses,
        "hedged": summary.hedged,
        "best_market": (
            {"slug": summary.best_market[0], "pnl": round(summary.best_market[1], 2)}
            if summary.best_market is not None else None
        ),
        "worst_market": (
            {"slug": summary.worst_market[0], "pnl": round(summary.worst_market[1], 2)}
            if summary.worst_market is not None else None
        ),
    }
    return {
        "period": period,
        "since_ts": since_ts,
        "grain": grain,
        "tiles": tiles,
        "equity": [{"bucket": e.bucket, "value": e.value} for e in equity],
        "bars": [
            {"bucket": b.bucket, "net_pnl": b.net_pnl, "trades": b.trades} for b in buckets
        ],
        "distribution": {
            "wins": summary.wins,
            "losses": summary.losses,
            "hedged": summary.hedged,
        },
    }


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    ctx = _base_ctx(request, "stats")
    try:
        initial = await _build_stats_payload("day")
    except Exception:
        initial = {
            "period": "day",
            "since_ts": 0,
            "grain": "hour",
            "tiles": {
                "net_pnl": 0.0, "trades": 0, "win_rate": 0.0,
                "wins": 0, "losses": 0, "hedged": 0,
                "best_market": None, "worst_market": None,
            },
            "equity": [],
            "bars": [],
            "distribution": {"wins": 0, "losses": 0, "hedged": 0},
        }
    ctx["initial_stats"] = initial
    return templates.TemplateResponse(request, "stats.html", ctx)


@router.get("/api/stats")
async def api_stats(period: str = "day"):
    if period not in ("day", "week", "month", "all"):
        raise HTTPException(status_code=400, detail="invalid period")
    payload = await _build_stats_payload(period)
    return JSONResponse(payload)
```

- [ ] **Step 4.4: Create a minimal `stats.html` to unblock the `/stats` test**

The `/stats` route renders `stats.html` — without the template, the route test will error. Create a bare-minimum template at `btcbot/web/templates/stats.html`:

```html
{% extends "base.html" %}
{% block title %}Stats — btcbot{% endblock %}

{% block content %}
<h1>Stats</h1>
<script type="application/json" id="initial-stats">{{ initial_stats|tojson|safe }}</script>
{% endblock %}
```

This stub is replaced with the full template in Task 5.

- [ ] **Step 4.5: Run tests to verify they pass**

Run: `pytest tests/test_web_stats.py -v`
Expected: all 5 tests PASS.

Also run the full suite to make sure nothing regressed:
Run: `pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 4.6: Commit**

```bash
git add btcbot/web/routes.py btcbot/web/templates/stats.html tests/test_web_stats.py
git commit -m "stats: add /stats HTML route and /api/stats JSON endpoint"
```

---

## Task 5: Build the full `stats.html` template, CSS, and nav link

**Files:**
- Modify: `btcbot/web/templates/stats.html` — replace stub with full template.
- Modify: `btcbot/web/static/app.css` — append `.period-toggle` and `.chart-wrap.is-loading` styles.
- Modify: `btcbot/web/templates/base.html` — add Stats nav link.

- [ ] **Step 5.1: Add CSS**

Append to `btcbot/web/static/app.css` (after the existing `.progress-bar` block, end of file):

```css
/* ── Stats page period toggle ────────────────────────────────────── */
.period-toggle {
  display: inline-flex;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 4px;
  gap: 4px;
  margin-bottom: 32px;
}

.period-toggle button {
  background: transparent;
  color: var(--muted);
  border: none;
  font-family: var(--font);
  font-size: 13px;
  font-weight: 600;
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.period-toggle button:hover {
  color: var(--text);
  background: var(--panel-hover);
}

.period-toggle button.active {
  background: var(--accent);
  color: #0b0e14;
}

/* Loading overlay for chart panels during period switch */
.chart-wrap.is-loading {
  opacity: 0.4;
  pointer-events: none;
  transition: opacity 0.15s;
}

/* Two-line stat variant for Best/Worst market tiles */
.stat.two-line .slug {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--muted);
  margin-top: 2px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Distribution chart should not stretch full width */
.chart-wrap.distribution {
  max-width: 480px;
  margin-left: auto;
  margin-right: auto;
}

.chart-wrap.distribution canvas {
  max-height: 260px;
}
```

- [ ] **Step 5.2: Add the Stats nav link**

Edit `btcbot/web/templates/base.html`. Find the `<nav>` block (currently at lines 23-27):

```html
    <nav>
      <a href="/" {% if active_page == 'dashboard' %}class="active"{% endif %}>Dashboard</a>
      <a href="/trades" {% if active_page == 'trades' %}class="active"{% endif %}>Trades</a>
      <a href="/history" {% if active_page == 'history' %}class="active"{% endif %}>History</a>
    </nav>
```

Replace with:

```html
    <nav>
      <a href="/" {% if active_page == 'dashboard' %}class="active"{% endif %}>Dashboard</a>
      <a href="/trades" {% if active_page == 'trades' %}class="active"{% endif %}>Trades</a>
      <a href="/history" {% if active_page == 'history' %}class="active"{% endif %}>History</a>
      <a href="/stats" {% if active_page == 'stats' %}class="active"{% endif %}>Stats</a>
    </nav>
```

- [ ] **Step 5.3: Write the full `stats.html` template**

Replace the entire contents of `btcbot/web/templates/stats.html` with:

```html
{% extends "base.html" %}
{% block title %}Stats — btcbot{% endblock %}

{% block content %}
<h1>Stats</h1>
<h2>Realized P&amp;L by period</h2>

<div class="period-toggle" role="tablist" aria-label="Period">
  <button type="button" data-period="day" class="active">Day</button>
  <button type="button" data-period="week">Week</button>
  <button type="button" data-period="month">Month</button>
  <button type="button" data-period="all">All-Time</button>
</div>

<div class="stats-row">
  <div class="stat">
    <div class="label">Net P&amp;L</div>
    <div class="value" id="tile-net-pnl">$0.00</div>
  </div>
  <div class="stat">
    <div class="label">Trades</div>
    <div class="value" id="tile-trades">0</div>
  </div>
  <div class="stat">
    <div class="label">Win Rate</div>
    <div class="value" id="tile-win-rate" style="color: var(--accent)">0.0%</div>
  </div>
  <div class="stat">
    <div class="label">Wins</div>
    <div class="value pos" id="tile-wins">0</div>
  </div>
  <div class="stat">
    <div class="label">Losses</div>
    <div class="value neg" id="tile-losses">0</div>
  </div>
  <div class="stat">
    <div class="label">Hedged</div>
    <div class="value" id="tile-hedged" style="color: var(--warn)">0</div>
  </div>
  <div class="stat two-line">
    <div class="label">Best Market</div>
    <div class="value pos" id="tile-best-market">—</div>
    <div class="slug" id="tile-best-slug"></div>
  </div>
  <div class="stat two-line">
    <div class="label">Worst Market</div>
    <div class="value neg" id="tile-worst-market">—</div>
    <div class="slug" id="tile-worst-slug"></div>
  </div>
</div>

<section>
  <h2 id="equity-title">Equity Curve</h2>
  <div class="chart-wrap" id="equity-wrap">
    <canvas id="chart-equity" height="220"></canvas>
    <div class="empty-state" id="equity-empty" style="display:none">No trades in this period</div>
  </div>
</section>

<section>
  <h2 id="bars-title">Hourly P&amp;L</h2>
  <div class="chart-wrap" id="bars-wrap">
    <canvas id="chart-bars" height="220"></canvas>
    <div class="empty-state" id="bars-empty" style="display:none">No trades in this period</div>
  </div>
</section>

<section>
  <h2>Outcome Distribution</h2>
  <div class="chart-wrap distribution" id="distribution-wrap">
    <canvas id="chart-distribution"></canvas>
    <div class="empty-state" id="distribution-empty" style="display:none">No trades in this period</div>
  </div>
</section>

<script type="application/json" id="initial-stats">{{ initial_stats|tojson|safe }}</script>

<script>
/* Stats page — period switching + Chart.js rendering */
(function() {
  const charts = { equity: null, bars: null, distribution: null };
  let currentPeriod = "day";

  /* ── Formatting helpers ─────────────────────────────────────── */
  function formatUsd(n) {
    const v = Number(n) || 0;
    const sign = v > 0 ? "+" : v < 0 ? "−" : "";
    return sign + "$" + Math.abs(v).toFixed(2);
  }
  function formatPct(n) {
    return ((Number(n) || 0) * 100).toFixed(1) + "%";
  }
  function setPosNegClass(el, v) {
    el.classList.remove("pos", "neg");
    if (v > 0) el.classList.add("pos");
    else if (v < 0) el.classList.add("neg");
  }

  /* ── Short label for x-axis display ─────────────────────────── */
  function shortLabel(bucket, grain) {
    if (grain === "hour") {
      // "2026-04-10 14:00" → "14:00"
      const parts = bucket.split(" ");
      return parts.length === 2 ? parts[1] : bucket;
    }
    if (grain === "day") {
      // "2026-04-10" → "04-10"
      return bucket.length === 10 ? bucket.slice(5) : bucket;
    }
    return bucket;  // week / month: "2026-W15" / "2026-04"
  }

  const barsTitleByGrain = {
    hour: "Hourly P&L",
    day: "Daily P&L",
    week: "Weekly P&L",
    month: "Monthly P&L",
  };

  /* ── Tile update ────────────────────────────────────────────── */
  function updateTiles(tiles) {
    const netPnl = document.getElementById("tile-net-pnl");
    netPnl.textContent = formatUsd(tiles.net_pnl);
    setPosNegClass(netPnl, tiles.net_pnl);

    document.getElementById("tile-trades").textContent = String(tiles.trades);
    document.getElementById("tile-win-rate").textContent = formatPct(tiles.win_rate);
    document.getElementById("tile-wins").textContent = String(tiles.wins);
    document.getElementById("tile-losses").textContent = String(tiles.losses);
    document.getElementById("tile-hedged").textContent = String(tiles.hedged);

    const bestEl = document.getElementById("tile-best-market");
    const bestSlug = document.getElementById("tile-best-slug");
    if (tiles.best_market) {
      bestEl.textContent = formatUsd(tiles.best_market.pnl);
      bestSlug.textContent = tiles.best_market.slug;
    } else {
      bestEl.textContent = "—";
      bestSlug.textContent = "";
    }

    const worstEl = document.getElementById("tile-worst-market");
    const worstSlug = document.getElementById("tile-worst-slug");
    if (tiles.worst_market) {
      worstEl.textContent = formatUsd(tiles.worst_market.pnl);
      worstSlug.textContent = tiles.worst_market.slug;
    } else {
      worstEl.textContent = "—";
      worstSlug.textContent = "";
    }
  }

  /* ── Shared Chart.js base options ──────────────────────────── */
  const sharedScaleX = { ticks: { color: "#8b949e", font: { size: 10 } }, grid: { color: "#2d333b" } };
  const sharedTooltip = {
    backgroundColor: "#151921",
    titleColor: "#8b949e",
    bodyColor: "#f0f6fc",
    borderColor: "#2d333b",
    borderWidth: 1,
  };

  /* ── Chart factories ────────────────────────────────────────── */
  function createEquityChart(ctx, labels, values) {
    return new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "Cumulative P&L",
          data: values,
          borderColor: "#58a6ff",
          borderWidth: 3,
          pointRadius: 0,
          tension: 0.3,
          fill: {
            target: "origin",
            above: "rgba(88, 166, 255, 0.08)",
            below: "rgba(248, 81, 73, 0.08)",
          },
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: sharedScaleX,
          y: {
            ticks: { color: "#58a6ff", callback: v => "$" + v.toFixed(0) },
            grid: { color: "#2d333b" },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            ...sharedTooltip,
            callbacks: { label: (c) => "Equity: $" + c.parsed.y.toFixed(2) },
          },
        },
      },
    });
  }

  function createBarsChart(ctx, labels, values, trades) {
    return new Chart(ctx, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [{
          label: "Net P&L",
          data: values,
          backgroundColor: values.map(v => v >= 0 ? "rgba(63, 185, 80, 0.5)" : "rgba(248, 81, 73, 0.5)"),
          borderColor: values.map(v => v >= 0 ? "#3fb950" : "#f85149"),
          borderWidth: 1.5,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: sharedScaleX,
          y: {
            ticks: { color: "#8b949e", callback: v => "$" + v.toFixed(0) },
            grid: { color: "#2d333b" },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            ...sharedTooltip,
            callbacks: {
              label: (c) => "Net P&L: $" + c.parsed.y.toFixed(2),
              afterLabel: (c) => (trades[c.dataIndex] || 0) + " trade(s)",
            },
          },
        },
      },
    });
  }

  function createDistributionChart(ctx, wins, losses, hedged) {
    return new Chart(ctx, {
      type: "doughnut",
      data: {
        labels: ["Wins", "Losses", "Hedged"],
        datasets: [{
          data: [wins, losses, hedged],
          backgroundColor: ["#3fb950", "#f85149", "#d29922"],
          borderColor: "#151921",
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "60%",
        plugins: {
          legend: {
            position: "right",
            labels: {
              color: "#f0f6fc",
              font: { size: 12 },
              generateLabels: (chart) => {
                const data = chart.data.datasets[0].data;
                const total = data.reduce((a, b) => a + b, 0) || 1;
                return chart.data.labels.map((lbl, i) => ({
                  text: lbl + ": " + data[i] + " (" + ((data[i] / total) * 100).toFixed(0) + "%)",
                  fillStyle: chart.data.datasets[0].backgroundColor[i],
                  strokeStyle: chart.data.datasets[0].backgroundColor[i],
                  index: i,
                }));
              },
            },
          },
          tooltip: sharedTooltip,
        },
      },
    });
  }

  /* ── Chart update (mutate + .update()) ──────────────────────── */
  function renderCharts(data) {
    const grain = data.grain;
    const equityLabels = data.equity.map(e => shortLabel(e.bucket, grain));
    const equityValues = data.equity.map(e => e.value);
    const barLabels = data.bars.map(b => shortLabel(b.bucket, grain));
    const barValues = data.bars.map(b => b.net_pnl);
    const barTrades = data.bars.map(b => b.trades);

    const isEmpty = data.bars.length === 0;
    toggleEmpty("equity", isEmpty);
    toggleEmpty("bars", isEmpty);

    document.getElementById("bars-title").textContent = barsTitleByGrain[grain] || "P&L";

    const equityCanvas = document.getElementById("chart-equity");
    const barsCanvas = document.getElementById("chart-bars");
    const distCanvas = document.getElementById("chart-distribution");

    if (!isEmpty) {
      if (!charts.equity) {
        charts.equity = createEquityChart(equityCanvas, equityLabels, equityValues);
      } else {
        charts.equity.data.labels = equityLabels;
        charts.equity.data.datasets[0].data = equityValues;
        charts.equity.update();
      }

      if (!charts.bars) {
        charts.bars = createBarsChart(barsCanvas, barLabels, barValues, barTrades);
      } else {
        charts.bars.data.labels = barLabels;
        charts.bars.data.datasets[0].data = barValues;
        charts.bars.data.datasets[0].backgroundColor = barValues.map(v => v >= 0 ? "rgba(63, 185, 80, 0.5)" : "rgba(248, 81, 73, 0.5)");
        charts.bars.data.datasets[0].borderColor = barValues.map(v => v >= 0 ? "#3fb950" : "#f85149");
        charts.bars.update();
      }
    }

    // Distribution chart: hide only if all three counts are zero
    const dist = data.distribution;
    const distEmpty = (dist.wins + dist.losses + dist.hedged) === 0;
    toggleEmpty("distribution", distEmpty);
    if (!distEmpty) {
      if (!charts.distribution) {
        charts.distribution = createDistributionChart(distCanvas, dist.wins, dist.losses, dist.hedged);
      } else {
        charts.distribution.data.datasets[0].data = [dist.wins, dist.losses, dist.hedged];
        charts.distribution.update();
      }
    }
  }

  function toggleEmpty(name, empty) {
    const canvas = document.getElementById("chart-" + name);
    const emptyEl = document.getElementById(name + "-empty");
    if (empty) {
      canvas.style.display = "none";
      emptyEl.style.display = "block";
    } else {
      canvas.style.display = "";
      emptyEl.style.display = "none";
    }
  }

  /* ── Render (tiles + charts) ────────────────────────────────── */
  function render(data) {
    updateTiles(data.tiles);
    renderCharts(data);
  }

  /* ── Period switching ───────────────────────────────────────── */
  function setLoading(on) {
    ["equity-wrap", "bars-wrap", "distribution-wrap"].forEach(id => {
      const el = document.getElementById(id);
      if (on) el.classList.add("is-loading");
      else el.classList.remove("is-loading");
    });
  }

  async function switchPeriod(period) {
    if (period === currentPeriod) return;
    document.querySelectorAll(".period-toggle button").forEach(b => {
      b.classList.toggle("active", b.dataset.period === period);
    });
    setLoading(true);
    try {
      const res = await fetch("/api/stats?period=" + encodeURIComponent(period));
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      render(data);
      currentPeriod = period;
    } catch (err) {
      console.error("stats fetch failed", err);
    } finally {
      setLoading(false);
    }
  }

  /* ── Boot ───────────────────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", function() {
    const raw = document.getElementById("initial-stats").textContent;
    let initial;
    try {
      initial = JSON.parse(raw);
    } catch (e) {
      console.error("invalid initial-stats JSON", e);
      return;
    }
    render(initial);
    document.querySelectorAll(".period-toggle button").forEach(btn => {
      btn.addEventListener("click", () => switchPeriod(btn.dataset.period));
    });
  });
})();
</script>
{% endblock %}
```

- [ ] **Step 5.4: Re-run the web tests to make sure nothing regressed**

Run: `pytest tests/test_web_stats.py -v`
Expected: all 5 tests still PASS. The full template still contains the `id="initial-stats"` tag, so `test_get_stats_page_returns_html_with_initial_data` should still pass.

Also run the full suite:
Run: `pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 5.5: Commit**

```bash
git add btcbot/web/templates/stats.html btcbot/web/templates/base.html btcbot/web/static/app.css
git commit -m "stats: add stats.html template, period toggle CSS, and nav link"
```

---

## Task 6: Manual verification against the live DB

No automated tests for this task — per the project's `CLAUDE.md` rule "always validate against live/actual data sources," the final step is to load the page in a browser and sanity-check everything.

**Important:** tell the user to restart the server so the new template and routes are loaded.

- [ ] **Step 6.1: Tell the user to restart the server**

Print (do not execute — the user runs the server):

> The Stats page is implemented. Please restart the `btcbot` web server so the new `/stats` route and `stats.html` template are loaded:
>
> ```bash
> # stop the current server (Ctrl+C), then restart it with your usual command
> ```
>
> Once it's running, open `http://localhost:<port>/stats` and confirm the checklist below.

- [ ] **Step 6.2: Provide the verification checklist to the user**

Walk through these checks with the user and report what you see:

1. Open `/stats`. The Day view should render with tiles and three charts populated from today's real data.
2. Click Week. Tiles update; equity curve + bars show daily data for the current ISO week.
3. Click Month. Same, but for the current calendar month.
4. Click All-Time. Equity curve/bars should span your entire history. Check the bars title — should say "Weekly P&L" unless the DB spans > 2 years (then "Monthly").
5. Open a period with no data (e.g., Day on a fresh morning with no trades yet). All three chart panels should show the "No trades in this period" empty state and tiles should show zeros / `—`.
6. Cross-check against `/history` and `/`: the all-time Net P&L tile on `/stats?period=all` should match the "All-Time Net P&L" tile on `/history`. The Day tile should match the "Today" P&L tile on `/`.
7. The "Stats" link should be visible in the top nav on every page (`/`, `/trades`, `/history`, `/stats`), and should be highlighted as active when on `/stats`.
8. Open browser devtools → Network tab → click between periods and confirm each switch fires exactly one `GET /api/stats?period=X` and completes in < 200ms.

- [ ] **Step 6.3: Fix any issues found, then commit if corrections were made**

If the user reports issues, debug and fix them inline. Each fix gets its own commit with a message like `stats: fix <description>`. Follow the project rule: verify fixes against the actual live DB, not mock data.

---

## Self-Review Notes

All design-spec requirements are covered:

| Spec requirement                                    | Implemented in |
|----------------------------------------------------|----------------|
| New `/stats` route alongside `/history`             | Task 4, 5      |
| Period toggle (Day/Week/Month/All-Time)             | Task 5         |
| 8 stat tiles                                         | Task 5         |
| Equity curve + P&L bars + doughnut distribution      | Task 5         |
| Calendar-based period boundaries                    | Task 1         |
| Auto bucket granularity per period                  | Task 3, 4      |
| Single `/api/stats` JSON endpoint                   | Task 4         |
| Server-rendered initial data (no flash)             | Task 4         |
| Chart.js mutate-and-update (no destroy/recreate)     | Task 5         |
| `oldest_resolved_at` helper + all-time grain switch | Task 2, 4      |
| Zero-filled buckets                                 | Task 3         |
| Empty-state handling                                | Task 5         |
| Nav link                                             | Task 5         |
| CSS additions (toggle, loading, two-line tiles)     | Task 5         |
| Manual verification                                  | Task 6         |

Function/type names are consistent across tasks: `StatsSummaryRow`, `BucketRow`, `EquityPoint`, `period_bounds`, `oldest_resolved_at`, `stats_summary`, `stats_buckets`, `stats_equity`, `_build_stats_payload`. The `_build_stats_payload` helper is used by both `/stats` and `/api/stats` to avoid duplication (DRY).

No placeholders, no "TBD", every step has concrete code or an exact command.
