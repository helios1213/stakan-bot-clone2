"""Wire the soft-start button to the running bot.

One loop watches `webkey_slots.soft_start_enabled` and keeps a warming engine
alive per enabled slot. Flipping the Telegram button is all it takes — no
restart, no redeploy.

Per slot it runs both halves of the spec:
  * SpotSoftStart     — 1-4 tokens/day, 0-10 buys, 0-10 sells, 1-150 USDT,
                        always keeping a baseline hold of each token.
  * FuturesSoftStart  — 1-3 orders/day, hold 10-300min, 3-10h apart, and ONLY
                        on pairs this account trades at 0% (checked per open).

Safety:
  * DRY-RUN unless `SOFT_START_LIVE=1` is set in the environment. The button
    alone can never start sending orders — two independent gates.
  * Turning the button OFF closes any futures position the slot is holding
    before the engine is dropped. Otherwise the button would orphan a live
    position on the exchange.
  * A slot whose engine raises is isolated: it is logged and skipped, and the
    other slots keep running.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

from .fee_gate import FeeGate
from .futures_soft_start import FuturesSoftStart, FuturesSoftStartConfig, live_allowed
from .soft_start_budget import (DEFAULT_MAX_COST_USDT, MIN_VIABLE_BALANCE_USDT,
                                SoftStartBudget, scale_spot_config)
from .soft_start_campaign import DEFAULT_CAMPAIGN_DAYS, SoftStartCampaign
from .spot_soft_start import SoftStartConfig, SpotSoftStart
from .webkey.spot_client import SpotWebClient

logger = logging.getLogger(__name__)

POLL_SEC = 60

# USDT on MEXC spot. Known constant — the balances endpoint is addressed by
# currencyId, not by ticker.
USDT_CURRENCY_ID = "128f589271cb4951b03e71e6323eb7be"


class SlotWarmer:
    """Both warming engines for one slot, sized from the slot's real balances
    and sharing one spend ceiling."""

    def __init__(self, slot_id: int, webkey: str, client, universe: list[str],
                 *, dry_run: bool, data_dir: str = "/app/data",
                 max_cost_usdt: float = DEFAULT_MAX_COST_USDT,
                 campaign_days: int = DEFAULT_CAMPAIGN_DAYS) -> None:
        self.slot_id = slot_id
        self.client = client
        self.universe = universe
        self.dry_run = dry_run
        self.data_dir = data_dir
        self.fee_gate = FeeGate(client)
        # ONE ceiling for both halves: the operator's limit is on warming as a
        # whole, not per venue.
        self.budget = SoftStartBudget(
            f"{data_dir}/soft_start_budget_slot{slot_id}.json", max_cost_usdt)
        # Warming is a finite 3-day job, not a permanent mode.
        self.campaign = SoftStartCampaign(
            f"{data_dir}/soft_start_campaign_slot{slot_id}.json", campaign_days)
        self.spot_client = SpotWebClient(webkey, dry_run=dry_run)
        self.spot: SpotSoftStart | None = None
        self.futures: FuturesSoftStart | None = None

    async def _read_balances(self) -> tuple[float, float]:
        """(spot_usdt, futures_usdt). A read failure returns 0.0, and 0.0 means
        'too small to warm' downstream — failing closed rather than guessing a
        balance and sizing orders off a fiction."""
        spot = fut = 0.0
        try:
            bals = await self.spot_client.balances([USDT_CURRENCY_ID])
            spot = float(bals.get("USDT", {}).get("available", 0) or 0)
        except Exception as e:
            logger.warning("soft-start slot %d: spot balance read failed: %s",
                           self.slot_id, e)
        try:
            r = await self.client._request("GET", "/account/assets")
            for row in (r or {}).get("data") or []:
                if str(row.get("currency")).upper() == "USDT":
                    fut = float(row.get("availableBalance")
                                or row.get("availableCash") or 0)
                    break
        except Exception as e:
            logger.warning("soft-start slot %d: futures balance read failed: %s",
                           self.slot_id, e)
        return spot, fut

    async def start(self) -> None:
        spot_bal, fut_bal = await self._read_balances()
        logger.info("soft-start slot %d: balances spot=%.2f futures=%.2f USDT",
                    self.slot_id, spot_bal, fut_bal)

        # Sizing scales with the balance: 25 USDT warms gently, 50 warms harder,
        # same config object either way.
        sizing = scale_spot_config(spot_bal)
        spot_cfg = SoftStartConfig(
            state_path=f"{self.data_dir}/spot_soft_start_slot{self.slot_id}.json",
            order_usdt_min=sizing.order_usdt_min,
            order_usdt_max=sizing.order_usdt_max,
            baseline_usdt_per_token=sizing.baseline_usdt_per_token,
            daily_buy_usdt_ceiling=max(sizing.daily_buy_usdt_ceiling,
                                       sizing.order_usdt_max),
        )
        self.spot = SpotSoftStart(self.spot_client, spot_cfg,
                                  rng=random.Random(), budget=self.budget)
        self.futures = FuturesSoftStart(
            self.client, self.fee_gate, self.universe,
            FuturesSoftStartConfig(
                state_path=f"{self.data_dir}/futures_soft_start_slot{self.slot_id}.json"),
            dry_run=self.dry_run, rng=random.Random(),
            budget=self.budget, balance_usdt=fut_bal,
        )
        self._spot_viable = spot_bal >= MIN_VIABLE_BALANCE_USDT
        self._fut_viable = fut_bal >= MIN_VIABLE_BALANCE_USDT
        for name, ok, bal in (("spot", self._spot_viable, spot_bal),
                              ("futures", self._fut_viable, fut_bal)):
            if not ok:
                logger.warning("soft-start slot %d: %s balance %.2f < %.0f USDT "
                               "— that half stays idle", self.slot_id, name, bal,
                               MIN_VIABLE_BALANCE_USDT)

        self.campaign.start_if_new()
        logger.info("soft-start slot %d: campaign day %d/%d, %.2f day(s) left",
                    self.slot_id, self.campaign.state.day_index() + 1,
                    self.campaign.state.days, self.campaign.state.remaining_days())
        await self.futures.recover()

    def _apply_day_weight(self) -> None:
        """Reshape today's targets by the day's randomly drawn activity weight.

        Drawn once per campaign-day and persisted, so some days are busy and
        some are nearly quiet — and a restart cannot reroll a quiet day into a
        busy one and double the activity.
        """
        w = self.campaign.day_weight()
        sp, fu = self.spot.plan, self.futures.state
        sp.buys_target = min(sp.buys_target, max(0, int(round(10 * w))))
        sp.sells_target = min(sp.sells_target, max(0, int(round(10 * w))))
        fu.orders_target = min(fu.orders_target, max(0, int(round(3 * w))))

    async def tick(self) -> None:
        if self.spot is None or self.futures is None:
            return                                  # start() has not run yet

        if self.campaign.expired():
            # Finite job: stop acting the moment the campaign is over. The loop
            # flips the DB button off so the UI stops claiming it is warming.
            self.campaign.finish()
            return

        if self.budget.exhausted():
            return                                  # ceiling reached; stay quiet

        self._apply_day_weight()
        if self._spot_viable:
            await self.spot.tick()
        if self._fut_viable:
            await self.futures.tick()

    def finished(self) -> bool:
        """True when this slot has nothing left to do: campaign over or budget spent."""
        return self.campaign.expired() or self.budget.exhausted()

    async def stop(self) -> None:
        """Close anything still open before this slot stops being warmed."""
        if self.futures is not None and self.futures.state.position is not None:
            logger.warning("soft-start slot %d: switching OFF with an open "
                           "position — closing it first", self.slot_id)
            await self.futures.close_position(forced=True)


async def soft_start_loop(store, client_pool, universe_provider,
                          poll_sec: int = POLL_SEC) -> None:
    """Keep warming engines in sync with the per-slot button.

    `universe_provider()` returns the candidate symbols; the FeeGate narrows
    them to the 0%-fee ones at open time.
    """
    warmers: dict[int, SlotWarmer] = {}
    dry_run = not live_allowed()
    logger.info("soft-start runner: started (%s)",
                "DRY-RUN — set SOFT_START_LIVE=1 to arm" if dry_run else "LIVE")

    while True:
        try:
            slots = await store.list_all()
            wanted = {s.slot_id for s in slots
                      if getattr(s, "soft_start_enabled", False) and s.webkey}

            # Stop warmers whose button was switched off.
            for slot_id in list(warmers):
                if slot_id not in wanted:
                    try:
                        await warmers[slot_id].stop()
                    except Exception:
                        logger.exception("soft-start slot %d: stop failed", slot_id)
                    warmers.pop(slot_id, None)
                    logger.info("soft-start slot %d: OFF", slot_id)

            # Start newly enabled ones.
            for slot in slots:
                sid = slot.slot_id
                if sid not in wanted or sid in warmers:
                    continue
                try:
                    client = await client_pool.get(sid)
                    warmers[sid] = SlotWarmer(sid, slot.webkey, client,
                                              universe_provider(), dry_run=dry_run)
                    await warmers[sid].start()
                    logger.info("soft-start slot %d: ON (%s)", sid,
                                "dry-run" if dry_run else "LIVE")
                except Exception:
                    logger.exception("soft-start slot %d: failed to start", sid)

            # Tick each independently — one bad slot must not stop the others.
            for sid, w in list(warmers.items()):
                try:
                    await w.tick()
                except Exception:
                    logger.exception("soft-start slot %d: tick failed", sid)
                    continue

                # A finished campaign (or a spent budget) switches ITSELF off in
                # the DB, so the button reflects reality rather than claiming to
                # warm an account nothing is happening on.
                if w.finished():
                    reason = ("campaign finished" if w.campaign.expired()
                              else "spend ceiling reached")
                    logger.info("soft-start slot %d: %s — switching the slot off",
                                sid, reason)
                    try:
                        await w.stop()
                        await store.set_soft_start(sid, False)
                    except Exception:
                        logger.exception("soft-start slot %d: auto-off failed", sid)
                    warmers.pop(sid, None)

        except asyncio.CancelledError:
            for w in warmers.values():
                try:
                    await w.stop()
                except Exception:
                    logger.exception("soft-start: shutdown close failed")
            raise
        except Exception:
            logger.exception("soft-start runner: loop error (contained)")

        await asyncio.sleep(poll_sec)
