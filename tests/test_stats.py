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
