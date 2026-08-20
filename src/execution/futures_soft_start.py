"""Futures account warming: open a position, hold it, close it. Slowly, rarely.

Spec (operator's, verbatim intent):
  * open ONLY on pairs with 0% fee                  -> enforced by FeeGate
  * a position stays open 10-300 minutes, never less than 10
  * 3-10 hours of pause BETWEEN orders
  * 1-3 orders per day

This is the module that puts real money at risk, so the safety design is
deliberately heavier than the spot one:

  * **Two independent live switches.** `dry_run=False` AND env `SOFT_START_LIVE=1`.
    Either one missing means nothing is sent.
  * **Fee is re-checked immediately before EVERY open**, not once at planning
    time. A tier can change between the plan and the order.
  * **zero_both by default**, not just zero maker: a warm-up position is opened
    AND closed, and a close can land as taker. `BTC_USDT` (maker=0, taker=0.0002)
    is correctly excluded under this rule.
  * **The open position is persisted the instant it is opened**, with its close
    deadline. If the bot restarts mid-hold, `recover()` finds the position and
    closes it on time instead of leaving it to sit.
  * **One position at a time.** No stacking.
  * Every order is wrapped: a failure logs and skips, never cascades.

State lives next to the spot state, in data/, so a rebuild does not lose it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LIVE_ENV = "SOFT_START_LIVE"

# MEXC order sides. 1 = open long, 3 = open short (the bot's own convention,
# matching submit_order(side=...) elsewhere in this package).
SIDE_LONG = 1
SIDE_SHORT = 3

ORDER_TYPE_MARKET = "5"


@dataclass
class FuturesSoftStartConfig:
    orders_per_day_min: int = 1
    orders_per_day_max: int = 3
    hold_minutes_min: int = 10           # spec: never less than 10
    hold_minutes_max: int = 300
    pause_hours_min: float = 3.0
    pause_hours_max: float = 10.0
    margin_usdt_min: float = 5.0
    margin_usdt_max: float = 20.0
    leverage_min: int = 5
    leverage_max: int = 20
    long_ratio: float = 0.5              # share of opens that go long
    require_zero_taker: bool = True      # a close can be taker — demand both
    state_path: str = "/app/data/futures_soft_start_state.json"

    def validate(self) -> None:
        if self.hold_minutes_min < 10:
            raise ValueError("hold_minutes_min < 10 violates the spec")
        if self.hold_minutes_max < self.hold_minutes_min:
            raise ValueError("hold window inverted")
        if self.pause_hours_min < 0 or self.pause_hours_max < self.pause_hours_min:
            raise ValueError("pause window invalid")
        if not 1 <= self.orders_per_day_min <= self.orders_per_day_max:
            raise ValueError("orders per day invalid")
        if self.margin_usdt_min <= 0 or self.margin_usdt_max < self.margin_usdt_min:
            raise ValueError("margin range invalid")
        if not 1 <= self.leverage_min <= self.leverage_max:
            raise ValueError("leverage range invalid")


@dataclass
class OpenPosition:
    symbol: str
    side: int
    vol: int
    leverage: int
    opened_at: float
    close_after: float          # epoch seconds — the hold deadline

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.close_after

    def held_minutes(self, now: float | None = None) -> float:
        return ((now or time.time()) - self.opened_at) / 60.0


@dataclass
class FuturesState:
    date: str = ""
    orders_target: int = 0
    orders_done: int = 0
    next_open_at: float = 0.0            # epoch seconds; the 3-10h pause
    position: dict | None = None         # asdict(OpenPosition) while holding

    def is_today(self) -> bool:
        return self.date == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state(path: str) -> FuturesState | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return FuturesState(**json.loads(p.read_text()))
    except Exception as e:
        logger.warning("futures soft-start state unreadable (%s)", e)
        return None


def save_state(path: str, st: FuturesState) -> None:
    try:
        Path(path).write_text(json.dumps(asdict(st), indent=2))
    except Exception as e:
        # A lost state file means a forgotten open position, so this is loud.
        logger.error("futures soft-start: STATE SAVE FAILED (%s) — "
                     "an open position may not be recovered after a restart", e)


def live_allowed() -> bool:
    return os.environ.get(LIVE_ENV, "") in ("1", "true", "yes")


class FuturesSoftStart:
    """Open -> hold 10-300min -> close, 1-3 times a day, 3-10h apart.

        ss = FuturesSoftStart(client, fee_gate, universe=[...])
        await ss.recover()          # close anything left open by a restart
        while True:
            await ss.tick()
            await asyncio.sleep(60)
    """

    def __init__(self, client, fee_gate, universe: list[str],
                 cfg: FuturesSoftStartConfig | None = None,
                 *, dry_run: bool = True, rng: random.Random | None = None,
                 budget=None, balance_usdt: float | None = None) -> None:
        # `budget`: shared spend ceiling (None = uncapped). `balance_usdt`: the
        # futures wallet, used to size the position and to refuse one that would
        # tie up too much of the account. Both optional so existing callers and
        # tests keep working unchanged.
        self.budget = budget
        self.balance_usdt = balance_usdt
        self._meta_cache: dict[str, tuple[float, float]] = {}
        self.cfg = cfg or FuturesSoftStartConfig()
        self.cfg.validate()
        self.client = client
        self.fee_gate = fee_gate
        self.universe = list(universe)
        self.rng = rng or random.Random()
        self.dry_run = bool(dry_run)
        self.state = load_state(self.cfg.state_path) or FuturesState()
        if not self.state.is_today():
            self._roll_day()
        if self.dry_run:
            logger.info("FuturesSoftStart: DRY-RUN — nothing will be sent")
        elif not live_allowed():
            logger.warning("FuturesSoftStart: dry_run=False but %s is not set — "
                           "staying inert", LIVE_ENV)
        else:
            logger.warning("FuturesSoftStart: LIVE — real positions will be opened")

    @property
    def sending(self) -> bool:
        """Both switches must agree before anything leaves the process."""
        return (not self.dry_run) and live_allowed()

    # ---- planning -------------------------------------------------------

    def _roll_day(self) -> None:
        c = self.cfg
        self.state = FuturesState(
            date=_today(),
            orders_target=self.rng.randint(c.orders_per_day_min, c.orders_per_day_max),
            orders_done=0,
            next_open_at=0.0,
            position=self.state.position if self.state else None,
        )
        save_state(c.state_path, self.state)
        logger.info("futures soft-start: %s — %d order(s) planned today",
                    self.state.date, self.state.orders_target)

    def _schedule_pause(self) -> None:
        c = self.cfg
        hours = self.rng.uniform(c.pause_hours_min, c.pause_hours_max)
        self.state.next_open_at = time.time() + hours * 3600
        save_state(c.state_path, self.state)
        logger.info("futures soft-start: next open in %.1fh", hours)

    # ---- pair choice ----------------------------------------------------

    def _size_position(self, sym: str, leverage: int):
        """(contracts, margin_usdt, notional_usdt) or (None, ..) to skip.

        Skips when: the contract metadata is unreadable, a single contract would
        tie up too much of the wallet, or the round-trip cost would breach the
        spend ceiling. Every skip is a refusal to trade — never a silent
        fallback to some other size.
        """
        from .soft_start_budget import (affordable, contracts_for_margin,
                                        futures_round_trip_cost, futures_target_margin)
        from src.exchanges.mexc_rest import to_mexc

        cs, px = self.contract_meta(to_mexc(sym))
        if cs <= 0 or px <= 0:
            logger.warning("futures soft-start: no contract meta for %s — skipping", sym)
            return None, 0.0, 0.0

        if self.balance_usdt is None:
            # No wallet reading available: keep the historical minimal behaviour.
            vol = 1
            target = self.rng.uniform(self.cfg.margin_usdt_min, self.cfg.margin_usdt_max)
        else:
            target = futures_target_margin(self.balance_usdt)
            vol = contracts_for_margin(target, leverage, cs, px)

        notional = vol * cs * px
        margin = notional / leverage

        if self.balance_usdt is not None and not affordable(margin, self.balance_usdt):
            logger.info("futures soft-start: %s needs %.2f margin of a %.2f wallet "
                        "— too big, skipping", sym, margin, self.balance_usdt)
            return None, 0.0, 0.0

        if self.budget is not None:
            cost = futures_round_trip_cost(notional)
            if not self.budget.can_afford(cost):
                logger.info("futures soft-start: %s round-trip cost %.4f would "
                            "exceed the budget (%.4f left) — skipping",
                            sym, cost, self.budget.remaining)
                return None, 0.0, 0.0

        return vol, margin, notional

    async def pick_pair(self) -> str | None:
        """A random pair that is 0% for THIS account, verified right now."""
        pool = list(self.universe)
        self.rng.shuffle(pool)
        for sym in pool:
            try:
                fee = await self.fee_gate.fee(sym)
            except Exception as e:
                logger.warning("futures soft-start: fee lookup %s failed (%s)", sym, e)
                continue
            if fee is None:
                continue                       # fail-closed
            if not fee.zero_maker:
                continue
            if self.cfg.require_zero_taker and not fee.zero_both:
                logger.debug("skip %s — taker fee %s", sym, fee.taker)
                continue
            return sym
        logger.warning("futures soft-start: no 0%%-fee pair available — skipping")
        return None

    # ---- contract metadata ----------------------------------------------

    def contract_meta(self, contract_symbol: str) -> tuple[float, float]:
        """(contract_size, last_price) for a contract, from the PUBLIC API.

        Needed to turn "I want ~N USDT of margin" into a contract count. Cached
        per process: contract size never changes and the price only needs to be
        roughly right for sizing.
        """
        hit = self._meta_cache.get(contract_symbol)
        if hit:
            return hit
        import json
        import urllib.request

        def _get(url):
            req = urllib.request.Request(url, headers={"accept": "*/*"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())

        size = price = 0.0
        try:
            d = _get("https://contract.mexc.com/api/v1/contract/detail"
                     f"?symbol={contract_symbol}").get("data") or {}
            size = float(d.get("contractSize") or 0)
        except Exception as e:
            logger.warning("contract detail %s failed: %s", contract_symbol, e)
        try:
            t = _get("https://contract.mexc.com/api/v1/contract/ticker"
                     f"?symbol={contract_symbol}").get("data") or {}
            price = float(t.get("lastPrice") or 0)
        except Exception as e:
            logger.warning("contract ticker %s failed: %s", contract_symbol, e)

        self._meta_cache[contract_symbol] = (size, price)
        return size, price

    # ---- open / close ---------------------------------------------------

    async def open_position(self) -> bool:
        c = self.cfg
        if self.state.position is not None:
            return False                        # one at a time
        if self.budget is not None and self.budget.exhausted():
            logger.info("futures soft-start: budget exhausted — not opening")
            return False
        sym = await self.pick_pair()
        if sym is None:
            return False

        leverage = self.rng.randint(c.leverage_min, c.leverage_max)
        side = SIDE_LONG if self.rng.random() < c.long_ratio else SIDE_SHORT
        hold_min = self.rng.randint(c.hold_minutes_min, c.hold_minutes_max)

        # Size from the WALLET, not from a hardcoded 1 contract. Previously
        # `margin_usdt_*` was computed and then ignored while vol was pinned to
        # 1 — the config looked like it controlled size but did not.
        vol, margin, notional = self._size_position(sym, leverage)
        if vol is None:
            return False

        if not self.sending:
            logger.info("[DRY futures] OPEN %s side=%d vol=%d lev=%dx margin~%.2f "
                        "hold=%dmin (nothing sent)", sym, side, vol, leverage,
                        margin, hold_min)
            self.state.orders_done += 1
            self._schedule_pause()
            return True

        try:
            from src.exchanges.mexc_rest import to_mexc
            resp = await self.client.submit_order(
                symbol=to_mexc(sym), side=side, vol=vol,
                leverage=leverage, order_type=ORDER_TYPE_MARKET)
        except Exception as e:
            logger.warning("[futures] OPEN %s FAILED: %s — skipped", sym, e)
            return False

        if str((resp or {}).get("code")) != "0":
            logger.warning("[futures] OPEN %s rejected: %s", sym,
                           json.dumps(resp or {})[:200])
            return False

        pos = OpenPosition(symbol=sym, side=side, vol=vol, leverage=leverage,
                           opened_at=time.time(),
                           close_after=time.time() + hold_min * 60)
        # Persist BEFORE anything else can fail: an unrecorded open position is
        # the worst outcome this module can produce.
        self.state.position = asdict(pos)
        self.state.orders_done += 1
        save_state(c.state_path, self.state)
        # Charge the round trip up front: both crossings and a possible funding
        # settlement are known now, and booking them at open means the ceiling
        # cannot be blown by a position we have already committed to.
        if self.budget is not None:
            from .soft_start_budget import futures_round_trip_cost
            self.budget.charge(futures_round_trip_cost(notional),
                               f"futures round-trip {sym}")
        logger.info("[futures] OPENED %s side=%d lev=%dx vol=%d "
                    "(margin~%.2f, notional~%.2f) — closing in %dmin",
                    sym, side, leverage, vol, margin, notional, hold_min)
        self._schedule_pause()
        return True

    async def close_position(self, *, forced: bool = False) -> bool:
        if self.state.position is None:
            return False
        pos = OpenPosition(**self.state.position)

        if not self.sending:
            logger.info("[DRY futures] CLOSE %s after %.1fmin (nothing sent)",
                        pos.symbol, pos.held_minutes())
            self.state.position = None
            save_state(self.cfg.state_path, self.state)
            return True

        try:
            from src.exchanges.mexc_rest import to_mexc
            resp = await self.client.close_all_positions(to_mexc(pos.symbol))
        except Exception as e:
            # Keep the state so the next tick retries — never drop an open position.
            logger.error("[futures] CLOSE %s FAILED: %s — will retry next tick",
                         pos.symbol, e)
            return False

        if str((resp or {}).get("code")) != "0":
            logger.error("[futures] CLOSE %s rejected: %s — will retry",
                         pos.symbol, json.dumps(resp or {})[:200])
            return False

        logger.info("[futures] CLOSED %s after %.1fmin%s",
                    pos.symbol, pos.held_minutes(), " (forced)" if forced else "")
        self.state.position = None
        save_state(self.cfg.state_path, self.state)
        return True

    async def recover(self) -> None:
        """Called at startup: adopt or close whatever a restart left behind."""
        if self.state.position is None:
            return
        pos = OpenPosition(**self.state.position)
        if pos.due():
            logger.warning("futures soft-start: position %s was left open past its "
                           "deadline (%.1fmin) — closing now", pos.symbol,
                           pos.held_minutes())
            await self.close_position(forced=True)
        else:
            remaining = (pos.close_after - time.time()) / 60
            logger.info("futures soft-start: resuming hold on %s, %.1fmin left",
                        pos.symbol, remaining)

    # ---- loop -----------------------------------------------------------

    async def tick(self) -> None:
        try:
            if not self.state.is_today():
                self._roll_day()

            if self.state.position is not None:
                if OpenPosition(**self.state.position).due():
                    await self.close_position()
                return                           # never open while holding

            if self.state.orders_done >= self.state.orders_target:
                return
            if time.time() < self.state.next_open_at:
                return
            await self.open_position()
        except Exception as e:
            logger.warning("futures soft-start tick error (contained): %s", e)

    async def run(self, poll_sec: int = 60) -> None:
        await self.recover()
        while True:
            await self.tick()
            await asyncio.sleep(poll_sec)
