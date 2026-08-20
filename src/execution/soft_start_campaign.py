"""Soft-start is a 3-DAY CAMPAIGN, not a permanent mode.

Warming an account is a finite job: it runs for a few days, looks like a human
poking at the account, and then stops. Leaving it on forever would keep bleeding
the spend ceiling and keep placing orders nobody is watching.

So the runner owns a campaign with:
  * a start timestamp and a length in days (default 3),
  * automatic shutdown when it expires — the per-slot button is flipped OFF in
    the DB, so the UI reflects reality instead of claiming it is still warming,
  * per-day randomisation drawn ONCE per day and persisted, so a restart does
    not reroll the plan and accidentally double a day's activity.

RANDOMISATION
-------------
"Random" here means every observable dimension varies, not just the amounts:

  * how many actions happen on a given day (and some days are quiet),
  * WHICH actions and in what ORDER — buys and sells are shuffled together, so
    the sequence is not a predictable buy-then-sell rhythm,
  * when in the day they happen (jittered gaps, active-hours window),
  * sizes, tokens, sides, hold times, pauses (owned by the engines).

A warm-up that fires the same shape every day is a pattern, which is exactly
what it is meant not to be.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CAMPAIGN_DAYS = 3
DAY_SEC = 86400


@dataclass
class CampaignState:
    started_at: float = 0.0
    days: int = DEFAULT_CAMPAIGN_DAYS
    finished: bool = False
    # day index -> the activity weight rolled for that day (0.0-1.0). Persisted
    # so a restart resumes the same plan instead of rerolling it.
    day_weights: dict = field(default_factory=dict)

    def elapsed_days(self, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        return ((now or time.time()) - self.started_at) / DAY_SEC

    def day_index(self, now: float | None = None) -> int:
        return int(self.elapsed_days(now))

    def expired(self, now: float | None = None) -> bool:
        return self.finished or self.elapsed_days(now) >= self.days

    def remaining_days(self, now: float | None = None) -> float:
        return max(0.0, self.days - self.elapsed_days(now))


class SoftStartCampaign:
    """A finite, randomised warming run for one slot."""

    def __init__(self, path: str, days: int = DEFAULT_CAMPAIGN_DAYS,
                 rng: random.Random | None = None) -> None:
        self.path = path
        self.rng = rng or random.Random()
        self.state = self._load(path, days)

    @staticmethod
    def _load(path: str, days: int) -> CampaignState:
        p = Path(path)
        if p.exists():
            try:
                return CampaignState(**json.loads(p.read_text()))
            except Exception as e:
                logger.warning("campaign state unreadable (%s) — starting fresh", e)
        return CampaignState(days=days)

    def _save(self) -> None:
        try:
            Path(self.path).write_text(json.dumps(asdict(self.state), indent=2))
        except Exception as e:
            logger.error("campaign state SAVE FAILED (%s) — a restart may "
                         "restart the campaign", e)

    # ---- lifecycle ------------------------------------------------------

    def start_if_new(self) -> bool:
        """Begin the campaign if it has not begun. True if this call started it."""
        if self.state.started_at:
            return False
        self.state.started_at = time.time()
        self.state.finished = False
        self.state.day_weights = {}
        self._save()
        logger.info("soft-start campaign: started, %d day(s)", self.state.days)
        return True

    def expired(self) -> bool:
        return self.state.expired()

    def finish(self) -> None:
        if self.state.finished:
            return
        self.state.finished = True
        self._save()
        logger.info("soft-start campaign: finished after %.2f day(s)",
                    self.state.elapsed_days())

    def reset(self) -> None:
        self.state = CampaignState(days=self.state.days)
        self._save()

    # ---- per-day randomisation ------------------------------------------

    def day_weight(self) -> float:
        """Activity weight for today, in [0.15, 1.0], rolled once and kept.

        A low weight makes a quiet day: fewer actions, sometimes none. Rolled
        per campaign-day rather than per tick so the day has a shape instead of
        flickering, and persisted so a restart cannot reroll it into a busier
        day and double the activity.
        """
        key = str(self.state.day_index())
        if key not in self.state.day_weights:
            self.state.day_weights[key] = round(self.rng.uniform(0.15, 1.0), 3)
            self._save()
            logger.info("soft-start campaign: day %s weight %.2f",
                        key, self.state.day_weights[key])
        return float(self.state.day_weights[key])

    def scale_target(self, base_max: int) -> int:
        """Scale a per-day target (buys, sells, futures orders) by today's weight."""
        return max(0, int(round(base_max * self.day_weight())))


def shuffled_actions(rng: random.Random, **available: bool) -> list[str]:
    """Actions that are possible right now, in random order.

    Returning a shuffled list — rather than checking buy then sell in a fixed
    sequence — is what stops the warm-up having a recognisable rhythm.
    """
    acts = [name for name, ok in available.items() if ok]
    rng.shuffle(acts)
    return acts
