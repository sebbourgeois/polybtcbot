"""Typed query helpers — keeps SQL out of business logic."""

from __future__ import annotations

import datetime
import json
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

from ..models import Market, TradeRecord


# ── Row wrappers ───────────────────────────────────────────────────────


@dataclass
class MarketRow:
    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    start_ts: int
    end_ts: int
    start_btc_price: float | None
    end_btc_price: float | None
    outcome: str | None
    discovered_at: int
    resolved_at: int | None


@dataclass
class TradeRow:
    id: int
    market_slug: str
    trade_type: str
    direction: str
    token_id: str
    side: str
    amount_usd: float
    fill_price: float
    token_quantity: float
    order_id: str | None
    signal_strength: float | None
    signal_edge: float | None
    is_paper: bool
    created_at: int


@dataclass
class ResultRow:
    market_slug: str
    entry_cost_usd: float
    hedge_cost_usd: float
    payout_usd: float
    net_pnl_usd: float
    outcome_correct: int | None
    resolved_at: int | None


@dataclass
class DailyPnlRow:
    date: str
    trades_count: int
    wins: int
    losses: int
    hedged: int
    gross_pnl_usd: float
    net_pnl_usd: float


# ── Markets ────────────────────────────────────────────────────────────


async def upsert_market(conn: aiosqlite.Connection, market: Market) -> None:
    await conn.execute(
        """INSERT INTO markets (slug, condition_id, up_token_id, down_token_id,
                                start_ts, end_ts, discovered_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(slug) DO UPDATE SET
               condition_id = excluded.condition_id,
               up_token_id  = excluded.up_token_id,
               down_token_id = excluded.down_token_id
        """,
        (
            market.slug,
            market.condition_id,
            market.up_token_id,
            market.down_token_id,
            market.start_ts,
            market.end_ts,
            int(time.time()),
        ),
    )
    await conn.commit()


async def set_market_outcome(
    conn: aiosqlite.Connection,
    slug: str,
    outcome: str,
    end_btc_price: float | None = None,
) -> None:
    await conn.execute(
        """UPDATE markets
           SET outcome = ?, end_btc_price = ?, resolved_at = ?
           WHERE slug = ?""",
        (outcome, end_btc_price, int(time.time()), slug),
    )
    await conn.commit()


async def set_market_start_price(
    conn: aiosqlite.Connection, slug: str, price: float
) -> None:
    await conn.execute(
        "UPDATE markets SET start_btc_price = ? WHERE slug = ?", (price, slug)
    )
    await conn.commit()


async def get_market(conn: aiosqlite.Connection, slug: str) -> MarketRow | None:
    cur = await conn.execute("SELECT * FROM markets WHERE slug = ?", (slug,))
    row = await cur.fetchone()
    return MarketRow(**dict(row)) if row else None


# ── Trades ─────────────────────────────────────────────────────────────


async def insert_trade(conn: aiosqlite.Connection, t: TradeRecord) -> int:
    cur = await conn.execute(
        """INSERT INTO trades
           (market_slug, trade_type, direction, token_id, side,
            amount_usd, fill_price, token_quantity, order_id,
            signal_strength, signal_edge, is_paper, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            t.market_slug,
            t.trade_type,
            t.direction,
            t.token_id,
            t.side,
            t.amount_usd,
            t.fill_price,
            t.token_quantity,
            t.order_id,
            t.signal_strength,
            t.signal_edge,
            int(t.is_paper),
            t.created_at,
        ),
    )
    await conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def trades_for_market(
    conn: aiosqlite.Connection, slug: str
) -> list[TradeRow]:
    cur = await conn.execute(
        "SELECT * FROM trades WHERE market_slug = ? ORDER BY created_at", (slug,)
    )
    return [TradeRow(**dict(r)) for r in await cur.fetchall()]


async def recent_trades(
    conn: aiosqlite.Connection, limit: int = 50
) -> list[TradeRow]:
    cur = await conn.execute(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    return [TradeRow(**dict(r)) for r in await cur.fetchall()]


# ── Live stats (queried from trades + market_results directly) ─────────


async def count_trades_since(
    conn: aiosqlite.Connection, since_ts: int
) -> int:
    """Count trades created after a given timestamp."""
    cur = await conn.execute(
        "SELECT COUNT(*) FROM trades WHERE created_at >= ?", (since_ts,)
    )
    row = await cur.fetchone()
    return int(row[0])


async def win_loss_counts(
    conn: aiosqlite.Connection, since_ts: int | None = None
) -> tuple[int, int, int]:
    """Return (wins, losses, hedged) from market_results.

    If since_ts is provided, only count results resolved after that time.
    """
    if since_ts is not None:
        cur = await conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN outcome_correct IS NULL THEN 1 ELSE 0 END), 0)
               FROM market_results WHERE resolved_at >= ?""",
            (since_ts,),
        )
    else:
        cur = await conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN outcome_correct IS NULL THEN 1 ELSE 0 END), 0)
               FROM market_results"""
        )
    row = await cur.fetchone()
    return int(row[0]), int(row[1]), int(row[2])


async def trailing_loss_streak(conn: aiosqlite.Connection) -> int:
    """Count consecutive losses from the most recent ENTRY trades (by trade time).

    Stops counting at the first win, so the result is the current trailing
    loss streak in chronological order — immune to async resolution races.
    """
    cur = await conn.execute(
        """SELECT mr.outcome_correct
           FROM trades t
           JOIN market_results mr ON mr.market_slug = t.market_slug
           WHERE t.trade_type = 'ENTRY' AND mr.outcome_correct IS NOT NULL
           ORDER BY t.created_at DESC
           LIMIT 20""",
    )
    streak = 0
    for row in await cur.fetchall():
        if row[0] == 0:
            streak += 1
        else:
            break
    return streak


async def hourly_pnl(
    conn: aiosqlite.Connection, hours: int = 24
) -> list[dict]:
    """Return per-hour P&L aggregated from market_results.

    Each row: {hour: "YYYY-MM-DD HH:00", trades: N, wins: N, losses: N, net_pnl: float}
    """
    since_ts = int(time.time()) - hours * 3600
    cur = await conn.execute(
        """SELECT
             strftime('%Y-%m-%d %H:00', resolved_at, 'unixepoch', 'localtime') AS hour,
             COUNT(*) AS trades,
             COALESCE(SUM(CASE WHEN outcome_correct = 1 THEN 1 ELSE 0 END), 0) AS wins,
             COALESCE(SUM(CASE WHEN outcome_correct = 0 THEN 1 ELSE 0 END), 0) AS losses,
             COALESCE(SUM(net_pnl_usd), 0) AS net_pnl
           FROM market_results
           WHERE resolved_at >= ?
           GROUP BY hour
           ORDER BY hour""",
        (since_ts,),
    )
    return [
        {
            "hour": row["hour"],
            "trades": row["trades"],
            "wins": row["wins"],
            "losses": row["losses"],
            "net_pnl_usd": round(float(row["net_pnl"]), 2),
        }
        for row in await cur.fetchall()
    ]


# ── Results ────────────────────────────────────────────────────────────


async def upsert_result(
    conn: aiosqlite.Connection,
    slug: str,
    entry_cost: float,
    hedge_cost: float,
    payout: float,
    net_pnl: float,
    outcome_correct: int | None,
) -> None:
    await conn.execute(
        """INSERT INTO market_results
           (market_slug, entry_cost_usd, hedge_cost_usd, payout_usd,
            net_pnl_usd, outcome_correct, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(market_slug) DO UPDATE SET
               payout_usd = excluded.payout_usd,
               net_pnl_usd = excluded.net_pnl_usd,
               outcome_correct = excluded.outcome_correct,
               resolved_at = excluded.resolved_at
        """,
        (slug, entry_cost, hedge_cost, payout, net_pnl, outcome_correct, int(time.time())),
    )
    await conn.commit()


async def total_pnl(conn: aiosqlite.Connection) -> float:
    cur = await conn.execute("SELECT COALESCE(SUM(net_pnl_usd), 0) FROM market_results")
    row = await cur.fetchone()
    return float(row[0])


async def pnl_since(conn: aiosqlite.Connection, since_ts: int) -> float:
    """Sum of net P&L from market_results resolved after since_ts."""
    cur = await conn.execute(
        "SELECT COALESCE(SUM(net_pnl_usd), 0) FROM market_results WHERE resolved_at >= ?",
        (since_ts,),
    )
    row = await cur.fetchone()
    return float(row[0])


# ── Daily P&L ──────────────────────────────────────────────────────────


async def update_daily_pnl(
    conn: aiosqlite.Connection,
    date: str,
    *,
    trades_count: int,
    wins: int,
    losses: int,
    hedged: int,
    gross_pnl: float,
    net_pnl: float,
) -> None:
    await conn.execute(
        """INSERT INTO daily_pnl (date, trades_count, wins, losses, hedged,
                                   gross_pnl_usd, net_pnl_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               trades_count = excluded.trades_count,
               wins = excluded.wins,
               losses = excluded.losses,
               hedged = excluded.hedged,
               gross_pnl_usd = excluded.gross_pnl_usd,
               net_pnl_usd = excluded.net_pnl_usd
        """,
        (date, trades_count, wins, losses, hedged, gross_pnl, net_pnl),
    )
    await conn.commit()


async def get_daily_pnl(
    conn: aiosqlite.Connection, days: int = 7
) -> list[DailyPnlRow]:
    cur = await conn.execute(
        "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT ?", (days,)
    )
    return [DailyPnlRow(**dict(r)) for r in await cur.fetchall()]


# ── BTC prices ─────────────────────────────────────────────────────────


async def insert_btc_price(
    conn: aiosqlite.Connection, price: float, ts: int | None = None
) -> None:
    await conn.execute(
        "INSERT INTO btc_prices (ts, price) VALUES (?, ?)",
        (ts or int(time.time()), price),
    )
    await conn.commit()


# ── Poly prices ────────────────────────────────────────────────────────


async def insert_poly_price(
    conn: aiosqlite.Connection, token_id: str, price: float, ts: int | None = None
) -> None:
    await conn.execute(
        "INSERT INTO poly_prices (token_id, ts, price) VALUES (?, ?, ?)",
        (token_id, ts or int(time.time()), price),
    )
    await conn.commit()


# ── System state ───────────────────────────────────────────────────────


async def save_open_position(
    conn: aiosqlite.Connection,
    market_slug: str,
    direction: str,
    token_id: str,
    fill_price: float,
    token_quantity: float,
    entry_time: float,
    hedge_count: int = 0,
    hedge_amount_usd: float = 0.0,
    hedge_token_quantity: float = 0.0,
    hedge_fill_price: float | None = None,
) -> None:
    """Persist the current open position so it survives restarts."""
    payload = json.dumps({
        "market_slug": market_slug,
        "direction": direction,
        "token_id": token_id,
        "fill_price": fill_price,
        "token_quantity": token_quantity,
        "entry_time": entry_time,
        "hedge_count": hedge_count,
        "hedge_amount_usd": hedge_amount_usd,
        "hedge_token_quantity": hedge_token_quantity,
        "hedge_fill_price": hedge_fill_price,
    })
    await set_state(conn, "open_position", payload)


async def load_open_position(
    conn: aiosqlite.Connection,
) -> dict | None:
    """Load persisted open position, or None if no position is saved."""
    result = await get_state(conn, "open_position")
    if result is None:
        return None
    return json.loads(result[0])


async def clear_open_position(conn: aiosqlite.Connection) -> None:
    """Remove persisted open position (after resolution or cleanup)."""
    await conn.execute("DELETE FROM system_state WHERE key = ?", ("open_position",))
    await conn.commit()


async def set_state(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        """INSERT INTO system_state (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, value, int(time.time())),
    )
    await conn.commit()


async def get_state(
    conn: aiosqlite.Connection, key: str
) -> tuple[str, int] | None:
    cur = await conn.execute(
        "SELECT value, updated_at FROM system_state WHERE key = ?", (key,)
    )
    row = await cur.fetchone()
    return (row["value"], row["updated_at"]) if row else None


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
