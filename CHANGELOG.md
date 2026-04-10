# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-04-10

### Added
- Stats page at `/stats` with day/week/month/all-time P&L rollups, 8 summary tiles (net P&L, trades, win rate, wins/losses/hedged, best/worst market), and three Chart.js panels (equity curve, per-bucket P&L bars, win/loss/hedged distribution doughnut).
- `GET /api/stats?period=day|week|month|all` JSON endpoint returning tiles, equity series, bucket bars, and distribution for the selected period.
- Backend aggregation helpers in `btcbot/storage/repo.py`: `period_bounds`, `oldest_resolved_at`, `stats_summary`, `stats_buckets`, `stats_equity` with zero-filled buckets and ISO-week rollup.
- Index on `market_results(resolved_at)` for period-filtered queries.
- "Stats" link in the top navigation on every page.
- `CHANGELOG.md` (this file).

### Changed
- Bumped project version from 0.1.0 to 1.0.0.
- Dashboard footer now shows `btcbot v1.0`.
- README: added Stats page to the dashboard table, documented `/api/stats`, fixed stale route counts (now 6 HTML + 5 JSON), updated test count from 29 to 56.

### Fixed
- Distribution chart legend labels now render in the light theme color (`#f0f6fc`) instead of the default black — Chart.js 4.x `LegendItem` objects returned from `generateLabels` need an explicit `fontColor` since `labels.color` doesn't cascade.
- Period-switch race: rapid clicks on the Stats page period toggle are now serialized via an in-flight guard so only one fetch runs at a time.
- `stats_db` test fixture now explicitly enables SQLite foreign key enforcement (`PRAGMA foreign_keys = ON`) on the aiosqlite connection, which was not inherited from the synchronous schema-setup connection.
- `/api/stats` invalid-period validation is now single-sourced through `repo.period_bounds` (catches `ValueError` and returns HTTP 400).
- Web test fixture also patches `btcbot.web.routes.CONFIG`, closing a latent bug where `routes.py`'s own `CONFIG` reference remained un-patched during tests.

### Removed
- `docs/` directory containing the stale superpowers specs and plans for bracket-orders (scrapped) and stats-page (shipped).
- Dead `grain == "day"` branch in `_label_from_day` that was unreachable from callers.

[Unreleased]: https://github.com/sebbourgeois/polybtcbot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/sebbourgeois/polybtcbot/releases/tag/v1.0.0
