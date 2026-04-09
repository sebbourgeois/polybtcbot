"""Regime detection — track whether the market is trending or mean-reverting."""

from __future__ import annotations

from collections import deque


class RegimeDetector:
    """Tracks intra-window reversal rate over recent markets.

    A reversal occurs when the first-half momentum direction doesn't match
    the final oracle outcome — indicating a mean-reverting (choppy) market.
    """

    def __init__(self, window: int = 20, min_samples: int = 5) -> None:
        self._buffer: deque[bool] = deque(maxlen=window)
        self._min_samples = min_samples

    def record(self, first_half_dir: str, outcome: str) -> None:
        """Record whether this window reversed (first-half != final)."""
        self._buffer.append(first_half_dir != outcome)

    @property
    def choppiness(self) -> float:
        """Reversal rate: 0.0 = pure trending, 1.0 = always reversing.

        Returns 0.5 (neutral) until min_samples are collected.
        """
        if len(self._buffer) < self._min_samples:
            return 0.5
        return sum(self._buffer) / len(self._buffer)
