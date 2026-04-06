"""Typed query helpers — keeps SQL out of business logic."""

from __future__ import annotations

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
