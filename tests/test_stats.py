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
    await conn.execute("PRAGMA foreign_keys = ON")
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
    # 3 wins, 2 losses, 1 hedged
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
    assert row.win_rate == pytest.approx(3 / (3 + 2))
    assert row.best_market == ("win-big", 18.22)
    assert row.worst_market == ("loss-big", -9.44)


@pytest.mark.asyncio
async def test_stats_summary_time_filter(stats_db):
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
