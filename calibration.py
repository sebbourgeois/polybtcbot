"""Calibration report: are the bot's signals actually predictive?

Breaks down ENTRY trades by predicted edge, signal strength, time-in-window,
direction, and hedge status, showing realized win rate and P&L per bucket.
A well-calibrated signal should show a monotonic relationship: higher
predicted edge => higher realized win rate. If 20%-edge trades only win 50%
of the time, `_estimate_fair_prob` in btcbot/signal.py is over-confident.

Usage:
    python calibration.py                # all trades (live + paper)
    python calibration.py --mode live    # live only
    python calibration.py --mode paper   # paper only
"""

import argparse
import os
import sqlite3
from dataclasses import dataclass

DB_PATH = os.environ.get("BOT_DB_PATH", "./btcbot.db")

EDGE_BUCKETS = [
    (0.00, 0.05), (0.05, 0.10), (0.10, 0.15),
    (0.15, 0.20), (0.20, 0.30), (0.30, 1.00),
]
STRENGTH_BUCKETS = [
    (0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01),
]
# Seconds remaining in the 5-min window when the ENTRY was placed.
TIME_REMAINING_BUCKETS = [
    (270, 301), (240, 270), (210, 240), (180, 210), (150, 180),
    (120, 150), (90, 120), (60, 90), (30, 60), (0, 30),
]


@dataclass
class Entry:
    market_slug: str
    direction: str
    amount_usd: float
    signal_edge: float
    signal_strength: float
    created_at: int
    start_ts: int
    is_paper: int
    outcome_correct: int
    net_pnl: float
    hedge_cost: float

    @property
    def time_remaining_at_entry(self) -> float:
        """Seconds left in the 5-min window when we entered."""
        return 300.0 - (self.created_at - self.start_ts)


def load_entries(mode: str) -> list[Entry]:
    mode_clause = ""
    if mode == "live":
        mode_clause = "AND t.is_paper = 0"
    elif mode == "paper":
        mode_clause = "AND t.is_paper = 1"

    # outcome_correct is recomputed from entry direction vs market.outcome:
    # older DBs stored NULL for hedged markets, which causes severe survivorship
    # bias if you filter on it.
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT t.market_slug, t.direction, t.amount_usd,
                   t.signal_edge, t.signal_strength, t.created_at, t.is_paper,
                   m.start_ts, m.outcome,
                   mr.net_pnl_usd, mr.hedge_cost_usd
            FROM trades t
            JOIN markets m ON m.slug = t.market_slug
            LEFT JOIN market_results mr ON mr.market_slug = t.market_slug
            WHERE t.trade_type = 'ENTRY'
              AND m.outcome IS NOT NULL
              {mode_clause}
            ORDER BY t.created_at
        """).fetchall()

    return [
        Entry(
            market_slug=r["market_slug"],
            direction=r["direction"],
            amount_usd=r["amount_usd"],
            signal_edge=r["signal_edge"] or 0.0,
            signal_strength=r["signal_strength"] or 0.0,
            created_at=r["created_at"],
            start_ts=r["start_ts"],
            is_paper=r["is_paper"],
            outcome_correct=1 if r["direction"] == r["outcome"] else 0,
            net_pnl=(r["net_pnl_usd"] or 0.0),
            hedge_cost=(r["hedge_cost_usd"] or 0.0),
        )
        for r in rows
    ]


def bucket(entries: list[Entry], key, ranges: list[tuple[float, float]]):
    return [
        (f"[{lo:>5.2f}, {hi:>5.2f})", [e for e in entries if lo <= key(e) < hi])
        for lo, hi in ranges
    ]


def _row(label: str, es: list[Entry]) -> str:
    n = len(es)
    if n == 0:
        return f"  {label:<18} {0:>4}     —       —          —          —"
    wins = sum(1 for e in es if e.outcome_correct == 1)
    wr = wins / n
    pnl = sum(e.net_pnl for e in es)
    avg_edge = sum(e.signal_edge for e in es) / n
    return f"  {label:<18} {n:>4} {wins:>5} {wr:>6.1%} {pnl:>+9.2f} {avg_edge:>10.3f}"


def print_table(title: str, buckets) -> None:
    print(f"\n{title}")
    print(f"  {'Bucket':<18} {'n':>4} {'wins':>5} {'WR':>6} {'P&L ($)':>9} {'avg edge':>10}")
    print(f"  {'-' * 58}")
    for label, es in buckets:
        print(_row(label, es))


def print_summary(title: str, entries: list[Entry]) -> None:
    n = len(entries)
    print(f"\n{title}")
    if n == 0:
        print("  (no resolved trades)")
        return
    wins = sum(1 for e in entries if e.outcome_correct == 1)
    hedged = sum(1 for e in entries if e.hedge_cost > 0)
    total_pnl = sum(e.net_pnl for e in entries)
    total_risked = sum(e.amount_usd + e.hedge_cost for e in entries)
    roi = total_pnl / total_risked if total_risked > 0 else 0.0
    avg_edge = sum(e.signal_edge for e in entries) / n
    avg_str = sum(e.signal_strength for e in entries) / n
    print(f"  Trades:          {n} ({wins}W / {n - wins}L, {hedged} hedged)")
    print(f"  Win rate:        {wins / n:.1%}")
    print(f"  Net P&L:         ${total_pnl:+.2f}")
    print(f"  Total risked:    ${total_risked:.2f}")
    print(f"  ROI:             {roi:+.1%}")
    print(f"  Avg pred. edge:  {avg_edge:.3f}")
    print(f"  Avg strength:    {avg_str:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC bot signal calibration report")
    parser.add_argument("--mode", choices=["live", "paper", "all"], default="all")
    args = parser.parse_args()

    entries = load_entries(args.mode)

    print(f"DB:   {DB_PATH}")
    print(f"Mode: {args.mode}")

    print_summary(f"Overall ({args.mode})", entries)

    if not entries:
        print("\nNo resolved trades yet. Run the bot and wait for markets to resolve.")
        return

    print_table(
        "Calibration by predicted edge  (want: WR rising with edge)",
        bucket(entries, lambda e: e.signal_edge, EDGE_BUCKETS),
    )

    print_table(
        "Calibration by signal strength",
        bucket(entries, lambda e: e.signal_strength, STRENGTH_BUCKETS),
    )

    print_table(
        "By time remaining at entry  (5-min window, s)",
        bucket(entries, lambda e: e.time_remaining_at_entry, TIME_REMAINING_BUCKETS),
    )

    print("\nBy direction")
    print(f"  {'Direction':<18} {'n':>4} {'wins':>5} {'WR':>6} {'P&L ($)':>9} {'avg edge':>10}")
    print(f"  {'-' * 58}")
    for direction in ("UP", "DOWN"):
        print(_row(direction, [e for e in entries if e.direction == direction]))

    print("\nHedged vs unhedged")
    print(f"  {'Kind':<18} {'n':>4} {'wins':>5} {'WR':>6} {'P&L ($)':>9} {'avg edge':>10}")
    print(f"  {'-' * 58}")
    print(_row("Unhedged", [e for e in entries if e.hedge_cost == 0]))
    print(_row("Hedged",   [e for e in entries if e.hedge_cost > 0]))

    if len(entries) < 30:
        print(
            f"\nNote: sample size is {len(entries)}. "
            "Per-bucket numbers are noisy until you reach ~50+ resolved trades."
        )


if __name__ == "__main__":
    main()
