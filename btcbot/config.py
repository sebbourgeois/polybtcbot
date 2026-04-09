"""Centralised configuration — every knob is a BOT_ env-var."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Config:
    # --- Auth (required for live trading) ---
    private_key: str

    # --- API endpoints ---
    clob_api_base: str
    gamma_api_base: str
    clob_ws_url: str
    binance_ws_url: str

    # --- Storage ---
    db_path: Path

    # --- Trading parameters ---
    bankroll: float
    min_signal_strength: float
    min_edge: float
    btc_5m_volatility: float
    limit_slippage: float

    # --- Risk limits ---
    max_position_usd: float
    min_position_usd: float
    max_daily_loss_usd: float
    max_consecutive_losses: int
    max_price_to_pay: float
    hedge_trigger_threshold: float

    # --- Regime detection ---
    regime_window: int

    # --- Timing ---
    discovery_interval_sec: float
    warmup_sec: float
    cooldown_sec: float
    risk_check_interval_sec: float

    # --- Web ---
    host: str
    port: int

    # --- Mode ---
    paper_mode: bool
    log_level: str


def load_config() -> Config:
    return Config(
        private_key=_env_str("BOT_PRIVATE_KEY", ""),
        clob_api_base=_env_str("BOT_CLOB_API", "https://clob.polymarket.com"),
        gamma_api_base=_env_str("BOT_GAMMA_API", "https://gamma-api.polymarket.com"),
        clob_ws_url=_env_str(
            "BOT_CLOB_WS",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        binance_ws_url=_env_str(
            "BOT_BINANCE_WS",
            "wss://stream.binance.com:9443/ws/btcusdt@trade",
        ),
        db_path=Path(_env_str("BOT_DB_PATH", "./btcbot.db")),
        bankroll=_env_float("BOT_BANKROLL", 100.0),
        min_signal_strength=_env_float("BOT_MIN_SIGNAL_STRENGTH", 0.30),
        min_edge=_env_float("BOT_MIN_EDGE", 0.05),
        btc_5m_volatility=_env_float("BOT_BTC_5M_VOLATILITY", 30.0),
        limit_slippage=_env_float("BOT_LIMIT_SLIPPAGE", 0.02),
        max_position_usd=_env_float("BOT_MAX_POSITION_USD", 25.0),
        min_position_usd=_env_float("BOT_MIN_POSITION_USD", 2.0),
        max_daily_loss_usd=_env_float("BOT_MAX_DAILY_LOSS_USD", 50.0),
        max_consecutive_losses=_env_int("BOT_MAX_CONSECUTIVE_LOSSES", 5),
        max_price_to_pay=_env_float("BOT_MAX_PRICE_TO_PAY", 0.65),
        hedge_trigger_threshold=_env_float("BOT_HEDGE_TRIGGER", 0.15),
        regime_window=_env_int("BOT_REGIME_WINDOW", 20),
        discovery_interval_sec=_env_float("BOT_DISCOVERY_INTERVAL_SEC", 30.0),
        warmup_sec=_env_float("BOT_WARMUP_SEC", 30.0),
        cooldown_sec=_env_float("BOT_COOLDOWN_SEC", 60.0),
        risk_check_interval_sec=_env_float("BOT_RISK_CHECK_SEC", 2.0),
        host=_env_str("BOT_HOST", "0.0.0.0"),
        port=_env_int("BOT_PORT", 8500),
        paper_mode=_env_bool("BOT_PAPER_MODE", True),
        log_level=_env_str("BOT_LOG_LEVEL", "INFO"),
    )


_CONFIG: Config | None = None


def __getattr__(name: str):
    """Lazy-load CONFIG on first access so CLI flags and env vars are set."""
    global _CONFIG
    if name == "CONFIG":
        if _CONFIG is None:
            _CONFIG = load_config()
        return _CONFIG
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
