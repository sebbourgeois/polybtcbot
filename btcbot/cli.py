"""Typer CLI — entry point for the bot."""

from __future__ import annotations

import asyncio
import logging
import sys

import typer

app = typer.Typer(help="Polymarket BTC 5-minute trading bot.", no_args_is_help=True)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


@app.command()
def run(
    paper: bool = typer.Option(True, "--live/--paper", help="Paper (default) or live trading"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Start the trading bot headless (no dashboard)."""
    _setup_logging(verbose)
    from .engine import Engine

    engine = Engine(paper_mode=paper)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        typer.echo("\nShutting down.")


@app.command()
def serve(
    host: str = typer.Option(None, help="Bind address (default from BOT_HOST)"),
    port: int = typer.Option(None, help="Port (default from BOT_PORT)"),
    paper: bool = typer.Option(True, "--live/--paper", help="Paper (default) or live trading"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on file changes"),
) -> None:
    """Start the bot with a live web dashboard."""
    _setup_logging(verbose)
    import os
    import uvicorn

    from .config import CONFIG

    # Pass paper mode to the engine via env var so the app lifespan picks it up
    if not paper:
        os.environ["BOT_PAPER_MODE"] = "false"

    uvicorn.run(
        "btcbot.web.app:app",
        host=host or CONFIG.host,
        port=port or CONFIG.port,
        log_level="debug" if verbose else "info",
        reload=reload,
    )


@app.command()
def status(
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Show current bot state: recent trades, daily P&L."""
    _setup_logging(verbose)
    asyncio.run(_show_status())


async def _show_status() -> None:
    from .storage.db import connect
    from .storage.repo import get_daily_pnl, recent_trades, total_pnl

    async with connect() as conn:
        pnl = await total_pnl(conn)
        days = await get_daily_pnl(conn, days=7)
        trades = await recent_trades(conn, limit=10)

    typer.echo(f"\n  Total P&L: ${pnl:+.2f}\n")
    if days:
        typer.echo("  Daily breakdown (last 7 days):")
        typer.echo(f"  {'Date':<12} {'Trades':>6} {'W':>4} {'L':>4} {'H':>4} {'P&L':>10}")
        for d in days:
            typer.echo(
                f"  {d.date:<12} {d.trades_count:>6} {d.wins:>4} "
                f"{d.losses:>4} {d.hedged:>4} ${d.net_pnl_usd:>+9.2f}"
            )
    if trades:
        typer.echo(f"\n  Last {len(trades)} trades:")
        for t in trades:
            paper_tag = " [paper]" if t.is_paper else ""
            typer.echo(
                f"  {t.trade_type:<6} {t.direction:<4} @ ${t.fill_price:.3f}  "
                f"${t.amount_usd:.2f}  edge={t.signal_edge or 0:+.3f}{paper_tag}"
            )
    typer.echo()


@app.command()
def history(
    days: int = typer.Option(7, help="Number of days to show"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Show trading history and P&L."""
    _setup_logging(verbose)
    asyncio.run(_show_history(days))


async def _show_history(days: int) -> None:
    from .storage.db import connect
    from .storage.repo import get_daily_pnl, total_pnl

    async with connect() as conn:
        pnl = await total_pnl(conn)
        rows = await get_daily_pnl(conn, days=days)

    typer.echo(f"\n  All-time P&L: ${pnl:+.2f}")
    if rows:
        total_trades = sum(d.trades_count for d in rows)
        total_wins = sum(d.wins for d in rows)
        wr = (total_wins / total_trades * 100) if total_trades else 0
        typer.echo(f"  Win rate ({days}d): {wr:.1f}% ({total_wins}/{total_trades})")
        typer.echo(f"\n  {'Date':<12} {'Trades':>6} {'W':>4} {'L':>4} {'H':>4} {'P&L':>10}")
        typer.echo(f"  {'─' * 44}")
        for d in rows:
            typer.echo(
                f"  {d.date:<12} {d.trades_count:>6} {d.wins:>4} "
                f"{d.losses:>4} {d.hedged:>4} ${d.net_pnl_usd:>+9.2f}"
            )
    else:
        typer.echo("  No trading history yet.")
    typer.echo()


@app.command()
def initdb() -> None:
    """Create the SQLite database."""
    asyncio.run(_init())


async def _init() -> None:
    from .storage.db import init_db

    await init_db()
    typer.echo("Database initialised.")
