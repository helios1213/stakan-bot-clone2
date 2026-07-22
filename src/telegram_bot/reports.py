"""
Periodic reports — scheduled tasks.

Runs in a background asyncio loop:
  - Daily summary at configured UTC hour (default: 09:00 UTC = 12:00 Kyiv)
  - Weekly report on Sundays
  - Hourly heartbeat (only during quiet hours: skipped)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from src.storage.db import Database
from src.telegram_bot.alerts import TelegramAlerts

logger = logging.getLogger(__name__)


class ReportScheduler:
    """Generates and sends periodic reports."""

    def __init__(
        self,
        db: Database,
        alerts: TelegramAlerts,
        daily_report_hour_utc: int = 6,  # 06:00 UTC = 09:00 Kyiv winter / 09:00 EET summer
    ) -> None:
        self.db = db
        self.alerts = alerts
        self.daily_report_hour_utc = daily_report_hour_utc

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_daily_sent_date: str | None = None
        self._last_weekly_sent_date: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="report_scheduler")
        logger.info("ReportScheduler started (daily at %02d:00 UTC)", self.daily_report_hour_utc)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self) -> None:
        """Check every minute if we need to send a report."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(60)
                try:
                    now = datetime.now(timezone.utc)
                    today_str = now.strftime("%Y-%m-%d")

                    # Daily report at configured UTC hour
                    if (now.hour == self.daily_report_hour_utc
                            and now.minute < 5
                            and self._last_daily_sent_date != today_str):
                        await self.send_daily_report()
                        self._last_daily_sent_date = today_str

                        # Weekly report on Sundays
                        if now.weekday() == 6:  # Sunday
                            await self.send_weekly_report()
                            self._last_weekly_sent_date = today_str

                except Exception as e:
                    logger.exception("Report loop error: %s", e)
        except asyncio.CancelledError:
            return

    async def send_daily_report(self) -> None:
        """Build and send daily summary."""
        try:
            text = await self._build_daily_text()
            await self.alerts.send(text, category="daily_report",
                                   throttle_sec=0, suppress_during_quiet=False)
        except Exception as e:
            logger.exception("Daily report failed: %s", e)

    async def send_weekly_report(self) -> None:
        try:
            text = await self._build_weekly_text()
            await self.alerts.send(text, category="weekly_report",
                                   throttle_sec=0, suppress_during_quiet=False)
        except Exception as e:
            logger.exception("Weekly report failed: %s", e)

    async def _build_daily_text(self) -> str:
        now_ts = int(time.time())
        day_ago = now_ts - 86400

        # Per-pair stats
        rows = await self.db.fetchall(
            """SELECT symbol, COUNT(*) as n,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(net_pnl_usdt) as pnl
               FROM shadow_trades
               WHERE closed_at >= ? AND closed_at IS NOT NULL
               GROUP BY symbol ORDER BY pnl DESC""",
            (day_ago,),
        )

        # Total summary
        totals = await self.db.fetchone(
            """SELECT COUNT(*) as n,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(net_pnl_usdt) as pnl,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN net_pnl_usdt ELSE 0 END) as gp,
                      SUM(CASE WHEN net_pnl_usdt < 0 THEN ABS(net_pnl_usdt) ELSE 0 END) as gl
               FROM shadow_trades
               WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (day_ago,),
        )

        # State transitions in last 24h
        transitions = await self.db.fetchall(
            """SELECT symbol, from_state, to_state, reason
               FROM state_transitions WHERE created_at >= ? ORDER BY created_at DESC""",
            (day_ago,),
        )

        # Current state distribution
        state_counts = await self.db.fetchall(
            "SELECT state, COUNT(*) as n FROM pair_states GROUP BY state"
        )

        lines = ["📅 <b>Daily Report</b> (last 24h)\n"]

        if not totals or totals["n"] == 0:
            lines.append("📭 <i>No trades in the last 24h.</i>")
        else:
            n = totals["n"]
            wr = totals["wins"]/n*100 if n > 0 else 0
            pf = totals["gp"]/totals["gl"] if totals["gl"] > 0 else 0
            lines.extend([
                f"Trades: <b>{n}</b>",
                f"Win Rate: <b>{wr:.1f}%</b>",
                f"PnL: <b>${totals['pnl']:+.4f}</b>",
                f"Profit Factor: <b>{pf:.2f}</b>",
            ])

        # Top 5 pairs
        if rows:
            lines.append("\n<b>Top 5 pairs:</b>")
            for r in rows[:5]:
                wr = r["wins"]/r["n"]*100 if r["n"] > 0 else 0
                emoji = "🟢" if r["pnl"] > 0 else "🔴" if r["pnl"] < 0 else "➖"
                lines.append(
                    f"  {emoji} <code>{r['symbol']:<10}</code> "
                    f"n={r['n']:<3} WR={wr:.0f}% PnL=<b>${r['pnl']:+.3f}</b>"
                )

        # State transitions summary
        if transitions:
            lines.append(f"\n<b>State changes:</b> {len(transitions)}")
            for t in transitions[:5]:  # Show first 5
                lines.append(
                    f"  <code>{t['symbol']:<10}</code> {t['from_state']}→{t['to_state']}"
                )
            if len(transitions) > 5:
                lines.append(f"  <i>...and {len(transitions)-5} more</i>")

        # Current state distribution
        if state_counts:
            lines.append("\n<b>Current state:</b>")
            for r in state_counts:
                lines.append(f"  {r['state']}: <b>{r['n']}</b>")

        return "\n".join(lines)

    async def _build_weekly_text(self) -> str:
        now_ts = int(time.time())
        week_ago = now_ts - 86400 * 7

        # Total weekly stats
        totals = await self.db.fetchone(
            """SELECT COUNT(*) as n,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(net_pnl_usdt) as pnl,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN net_pnl_usdt ELSE 0 END) as gp,
                      SUM(CASE WHEN net_pnl_usdt < 0 THEN ABS(net_pnl_usdt) ELSE 0 END) as gl
               FROM shadow_trades
               WHERE closed_at >= ? AND closed_at IS NOT NULL""",
            (week_ago,),
        )

        # Top performers
        rows = await self.db.fetchall(
            """SELECT symbol, COUNT(*) as n,
                      SUM(CASE WHEN net_pnl_usdt > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(net_pnl_usdt) as pnl,
                      AVG(roi_pct) as avg_roi
               FROM shadow_trades
               WHERE closed_at >= ? AND closed_at IS NOT NULL
               GROUP BY symbol ORDER BY pnl DESC""",
            (week_ago,),
        )

        # Exit reason breakdown
        exit_rows = await self.db.fetchall(
            """SELECT exit_reason, COUNT(*) as n, AVG(roi_pct) as avg_roi
               FROM shadow_trades
               WHERE closed_at >= ? AND closed_at IS NOT NULL
               GROUP BY exit_reason ORDER BY n DESC""",
            (week_ago,),
        )

        lines = ["📊 <b>Weekly Report</b> (last 7 days)\n"]

        if totals and totals["n"] > 0:
            n = totals["n"]
            wr = totals["wins"]/n*100 if n > 0 else 0
            pf = totals["gp"]/totals["gl"] if totals["gl"] > 0 else 0
            lines.extend([
                f"Total trades: <b>{n}</b>",
                f"Win Rate: <b>{wr:.1f}%</b>",
                f"PnL: <b>${totals['pnl']:+.4f}</b>",
                f"Profit Factor: <b>{pf:.2f}</b>",
            ])

        if rows:
            lines.append("\n<b>Top 10 pairs by PnL:</b>")
            for r in rows[:10]:
                wr = r["wins"]/r["n"]*100 if r["n"] > 0 else 0
                emoji = "🟢" if r["pnl"] > 0 else "🔴" if r["pnl"] < 0 else "➖"
                lines.append(
                    f"  {emoji} <code>{r['symbol']:<10}</code> "
                    f"n={r['n']:<4} WR={wr:.0f}% PnL=<b>${r['pnl']:+.2f}</b>"
                )

        if exit_rows:
            lines.append("\n<b>Exit reasons:</b>")
            total_exits = sum(r["n"] for r in exit_rows)
            for r in exit_rows:
                pct = r["n"]/total_exits*100 if total_exits > 0 else 0
                lines.append(
                    f"  <code>{r['exit_reason']:<18}</code> "
                    f"{r['n']:<5} ({pct:.1f}%) avg_roi={r['avg_roi']:+.2f}%"
                )

        return "\n".join(lines)
