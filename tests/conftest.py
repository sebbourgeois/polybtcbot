"""Ensure tests run with default config values, not .env overrides."""

import os

import pytest

import btcbot.config as _cfg
import btcbot.engine as _engine
import btcbot.risk as _risk
import btcbot.signal as _signal


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Remove all BOT_ env vars and inject a fresh default config everywhere."""
    for key in list(os.environ):
        if key.startswith("BOT_"):
            monkeypatch.delenv(key)

    # Build a fresh config from defaults (env vars are now cleared)
    fresh = _cfg.load_config()

    # Inject into config module and all consuming modules
    monkeypatch.setattr(_cfg, "_CONFIG", fresh)
    monkeypatch.setattr(_engine, "CONFIG", fresh)
    monkeypatch.setattr(_risk, "CONFIG", fresh)
    monkeypatch.setattr(_signal, "CONFIG", fresh)
