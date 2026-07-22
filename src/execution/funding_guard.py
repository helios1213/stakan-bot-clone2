"""
Funding guard — prevents entering positions near funding settlement.

MEXC futures funding occurs at 00:00, 08:00, 16:00 UTC by default.
At settlement, longs/shorts pay each other a funding rate (typically
±0.01% to ±0.05% per period). For our short-hold strategy this is
pure tax — we close before settlement.

Rules:
  - Do NOT open new positions within `cutoff_sec` of next funding
  - Force-close any open position within `cutoff_sec` of funding
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class FundingGuard:
    """Determines proximity to funding events."""

    def __init__(
        self,
        funding_intervals_utc: list[str] | None = None,
        cutoff_sec: int = 60,
    ) -> None:
        # Default MEXC schedule
        self.funding_times = funding_intervals_utc or ["00:00", "08:00", "16:00"]
        self.cutoff_sec = cutoff_sec

    def seconds_to_next_funding(self, now_utc: datetime | None = None) -> int:
        """Returns seconds until the next funding event."""
        now = now_utc or datetime.now(timezone.utc)
        candidates = []
        for t in self.funding_times:
            hh, mm = t.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            candidates.append((target - now).total_seconds())
        return int(min(candidates))

    def is_too_close(self, now_utc: datetime | None = None) -> bool:
        """True if we're within cutoff_sec of next funding."""
        return self.seconds_to_next_funding(now_utc) <= self.cutoff_sec

    def is_too_close_for_entry(self, now_utc: datetime | None = None) -> bool:
        """Same as is_too_close — alias for clarity at call sites."""
        return self.is_too_close(now_utc)
