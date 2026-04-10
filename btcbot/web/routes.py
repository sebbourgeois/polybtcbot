"""Web routes — HTML pages + JSON API endpoints."""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..config import CONFIG
from ..storage.db import connect
from ..storage import repo

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────


def _engine(request: Request) -> Any:
    """Get the engine instance from app state (may be None)."""
    return getattr(request.app.state, "engine", None)


def _time_ago(ts: int) -> str:
    diff = int(time.time()) - ts
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _base_ctx(request: Request, active_page: str) -> dict:
    engine = _engine(request)
    return {
        "request": request,
        "active_page": active_page,
        "paper_mode": engine._paper_mode if engine else CONFIG.paper_mode,
        "engine_running": engine is not None,
    }


def _live_ctx(request: Request) -> dict:
    """Build the live dashboard context from engine state."""
    engine = _engine(request)

    ctx: dict[str, Any] = {
        "request": request,
        "btc_price": 0.0,
        "btc_trend": None,
        "market": None,
        "market_remaining": 0,
        "market_start_price": None,
        "signal": None,
        "position": None,
        "poly_up": None,
        "poly_down": None,
        "engine_running": engine is not None,
        "total_pnl": 0.0,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "win_rate": 0.0,
        "consec_losses": 0,
    }

    if not engine:
        return ctx

    ctx["btc_price"] = engine._binance.latest_price
    ctx["btc_trend"] = engine._binance.trend

    mkt = engine._current_market
    if mkt:
        ctx["market"] = mkt
        ctx["market_remaining"] = mkt.seconds_remaining
        ctx["poly_up"] = engine._polymarket.get_price(mkt.up_token_id)
        ctx["poly_down"] = engine._polymarket.get_price(mkt.down_token_id)

    # Last signal evaluation
    sig = engine._signal_gen.evaluate(
        btc_price=engine._binance.latest_price,
        poly_up_price=ctx["poly_up"],
        poly_down_price=ctx["poly_down"],
        time_remaining_sec=mkt.seconds_remaining if mkt else 0,
    ) if mkt and engine._binance.latest_price > 0 else None
    ctx["signal"] = sig

    ctx["position"] = engine._position
    ctx["consec_losses"] = engine._risk.consecutive_losses

    return ctx


async def _enrich_live_ctx(ctx: dict) -> dict:
    """Add DB-sourced data to the live context."""
    try:
        today_start = int(
            datetime.datetime.combine(
                datetime.date.today(), datetime.time.min
            ).timestamp()
        )
        async with connect() as conn:
            ctx["total_pnl"] = await repo.total_pnl(conn)
            ctx["daily_pnl"] = await repo.pnl_since(conn, today_start)
            ctx["trades_today"] = await repo.count_trades_since(conn, today_start)

            # Win rate — from market_results (all time)
            wins, losses, _ = await repo.win_loss_counts(conn)
            total = wins + losses
            ctx["win_rate"] = (wins / total * 100) if total > 0 else 0

            # Market start price
            mkt = ctx.get("market")
            if mkt:
                row = await repo.get_market(conn, mkt.slug)
                if row and row.start_btc_price:
                    ctx["market_start_price"] = row.start_btc_price
    except Exception:
        pass
    return ctx


async def _recent_trades_ctx(request: Request, limit: int = 15) -> dict:
    """Build context for the recent trades partial."""
    trades_out = []
    try:
        async with connect() as conn:
            trades = await repo.recent_trades(conn, limit=limit)
        for t in trades:
            trades_out.append({
                "market_slug": t.market_slug,
                "trade_type": t.trade_type,
                "direction": t.direction,
                "fill_price": t.fill_price,
                "amount_usd": t.amount_usd,
                "signal_edge": t.signal_edge,
                "signal_strength": t.signal_strength,
                "is_paper": t.is_paper,
                "time_ago": _time_ago(t.created_at),
            })
    except Exception:
        pass
    return {"request": request, "trades": trades_out}


# ── HTML Routes ────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = _base_ctx(request, "dashboard")
    live = _live_ctx(request)
    live = await _enrich_live_ctx(live)
    ctx.update(live)
    ctx.update(await _recent_trades_ctx(request))
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/live", response_class=HTMLResponse)
async def partial_live(request: Request):
    """htmx partial: live status panels + stats."""
    ctx = _live_ctx(request)
    ctx = await _enrich_live_ctx(ctx)
    return templates.TemplateResponse(request, "partials/live.html", ctx)


@router.get("/partials/recent-trades", response_class=HTMLResponse)
async def partial_recent_trades(request: Request):
    """htmx partial: recent trades table."""
    ctx = await _recent_trades_ctx(request)
    return templates.TemplateResponse(request, "partials/recent_trades.html", ctx)


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    ctx = _base_ctx(request, "trades")
    trades_out = []
    try:
        async with connect() as conn:
            trades = await repo.recent_trades(conn, limit=200)
            # Get results for each trade's market
            results = {}
            slugs = list({t.market_slug for t in trades})
            for slug in slugs:
                cur = await conn.execute(
                    "SELECT outcome_correct, hedge_cost_usd FROM market_results WHERE market_slug = ?",
                    (slug,),
                )
                row = await cur.fetchone()
                if row:
                    results[slug] = (row[0], float(row[1] or 0.0))

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

            trades_out.append({
                "market_slug": t.market_slug,
                "trade_type": t.trade_type,
                "direction": t.direction,
                "fill_price": t.fill_price,
                "token_quantity": t.token_quantity,
                "amount_usd": t.amount_usd,
                "signal_edge": t.signal_edge,
                "signal_strength": t.signal_strength,
                "is_paper": t.is_paper,
                "time_str": datetime.datetime.fromtimestamp(t.created_at).strftime("%m-%d %H:%M:%S"),
                "result": result,
            })
    except Exception:
        pass

    entries = sum(1 for t in trades_out if t["trade_type"] == "ENTRY")
    hedges = sum(1 for t in trades_out if t["trade_type"] == "HEDGE")
    total_amt = sum(t["amount_usd"] for t in trades_out)

    ctx["trades"] = trades_out
    ctx["total_trades"] = len(trades_out)
    ctx["entries"] = entries
    ctx["hedges"] = hedges
    ctx["avg_size"] = total_amt / len(trades_out) if trades_out else 0
    return templates.TemplateResponse(request, "trades.html", ctx)


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    ctx = _base_ctx(request, "history")
    try:
        async with connect() as conn:
            days = await repo.get_daily_pnl(conn, days=90)
            total_pnl = await repo.total_pnl(conn)
    except Exception:
        days = []
        total_pnl = 0.0

    total_trades = sum(d.wins + d.losses for d in days)
    total_wins = sum(d.wins for d in days)
    win_rate = (total_wins / total_trades * 100) if total_trades else 0
    best_day = max((d.net_pnl_usd for d in days), default=0)
    worst_day = min((d.net_pnl_usd for d in days), default=0)

    # Add win_rate per day
    enriched_days = []
    for d in days:
        total = d.wins + d.losses
        enriched_days.append(type("Day", (), {
            **{k: getattr(d, k) for k in d.__dataclass_fields__},
            "win_rate": (d.wins / total * 100) if total > 0 else 0,
        })())

    ctx["days"] = enriched_days
    ctx["total_pnl"] = total_pnl
    ctx["total_trades"] = total_trades
    ctx["win_rate"] = win_rate
    ctx["best_day"] = best_day
    ctx["worst_day"] = worst_day
    return templates.TemplateResponse(request, "history.html", ctx)


# ── JSON API ───────────────────────────────────────────────────────────


@router.get("/api/live")
async def api_live(request: Request):
    """Live engine state as JSON."""
    ctx = _live_ctx(request)
    ctx = await _enrich_live_ctx(ctx)
    return JSONResponse({
        "btc_price": ctx["btc_price"],
        "btc_trend": ctx["btc_trend"],
        "market": ctx["market"].slug if ctx["market"] else None,
        "market_remaining": ctx["market_remaining"],
        "poly_up": ctx["poly_up"],
        "poly_down": ctx["poly_down"],
        "signal": {
            "direction": ctx["signal"].direction,
            "strength": round(ctx["signal"].strength, 3),
            "edge": round(ctx["signal"].edge, 4),
            "fair_prob": round(ctx["signal"].fair_prob, 4),
        } if ctx["signal"] and ctx["signal"].strength > 0 else None,
        "position": {
            "direction": ctx["position"].direction,
            "fill_price": ctx["position"].fill_price,
            "quantity": ctx["position"].token_quantity,
        } if ctx["position"] else None,
        "total_pnl": round(ctx["total_pnl"], 2),
        "daily_pnl": round(ctx["daily_pnl"], 2),
        "trades_today": ctx["trades_today"],
        "win_rate": round(ctx["win_rate"], 1),
    })


@router.get("/api/trades")
async def api_trades(limit: int = 50):
    async with connect() as conn:
        trades = await repo.recent_trades(conn, limit=limit)
    return JSONResponse([
        {
            "market_slug": t.market_slug,
            "trade_type": t.trade_type,
            "direction": t.direction,
            "fill_price": t.fill_price,
            "amount_usd": round(t.amount_usd, 2),
            "signal_edge": round(t.signal_edge, 4) if t.signal_edge else None,
            "signal_strength": round(t.signal_strength, 3) if t.signal_strength else None,
            "is_paper": bool(t.is_paper),
            "created_at": t.created_at,
        }
        for t in trades
    ])


@router.get("/api/daily-pnl")
async def api_daily_pnl(days: int = 30):
    async with connect() as conn:
        rows = await repo.get_daily_pnl(conn, days=days)
    # Reverse so oldest first (for charts)
    rows = list(reversed(rows))
    return JSONResponse([
        {
            "date": d.date,
            "trades_count": d.trades_count,
            "wins": d.wins,
            "losses": d.losses,
            "hedged": d.hedged,
            "net_pnl_usd": round(d.net_pnl_usd, 2),
        }
        for d in rows
    ])


@router.get("/api/hourly-pnl")
async def api_hourly_pnl(hours: int = 24):
    async with connect() as conn:
        rows = await repo.hourly_pnl(conn, hours=hours)
    return JSONResponse(rows)


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
    try:
        payload = await _build_stats_payload(period)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(payload)
