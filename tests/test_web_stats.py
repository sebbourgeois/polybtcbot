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

    # Point db.connect() at our temp DB. db.py does `from ..config import CONFIG`
    # which binds the frozen Config singleton into db.py's namespace. Since the
    # dataclass is frozen we cannot mutate it — instead we replace the module-level
    # name with a new Config instance that has our test db_path.
    import dataclasses
    from btcbot.storage import db as _db_module
    test_config = dataclasses.replace(_db_module.CONFIG, db_path=db_path)
    monkeypatch.setattr(_db_module, "CONFIG", test_config)

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
