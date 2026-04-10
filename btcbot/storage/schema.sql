PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS markets (
    slug            TEXT PRIMARY KEY,
    condition_id    TEXT NOT NULL UNIQUE,
    up_token_id     TEXT NOT NULL,
    down_token_id   TEXT NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    start_btc_price REAL,
    end_btc_price   REAL,
    outcome         TEXT,
    discovered_at   INTEGER NOT NULL,
    resolved_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_markets_end_ts ON markets(end_ts);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT NOT NULL REFERENCES markets(slug),
    trade_type      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    amount_usd      REAL NOT NULL,
    fill_price      REAL NOT NULL,
    token_quantity  REAL NOT NULL,
    order_id        TEXT,
    signal_strength REAL,
    signal_edge     REAL,
    is_paper        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_slug);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);

CREATE TABLE IF NOT EXISTS market_results (
    market_slug     TEXT PRIMARY KEY REFERENCES markets(slug),
    entry_cost_usd  REAL NOT NULL,
    hedge_cost_usd  REAL NOT NULL DEFAULT 0,
    payout_usd      REAL NOT NULL DEFAULT 0,
    net_pnl_usd     REAL NOT NULL DEFAULT 0,
    outcome_correct INTEGER,
    resolved_at     INTEGER,
    redeemed_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_market_results_resolved_at ON market_results(resolved_at);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date            TEXT PRIMARY KEY,
    trades_count    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    hedged          INTEGER NOT NULL DEFAULT 0,
    gross_pnl_usd  REAL NOT NULL DEFAULT 0,
    net_pnl_usd    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS btc_prices (
    ts              INTEGER NOT NULL,
    price           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_btc_prices_ts ON btc_prices(ts);

CREATE TABLE IF NOT EXISTS poly_prices (
    token_id        TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    price           REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_poly_prices ON poly_prices(token_id, ts);

CREATE TABLE IF NOT EXISTS system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
);
