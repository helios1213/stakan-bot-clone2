"""
/balance command — query MEXC futures balance for all slots.

Output example:

  💰 BALANCE (per slot)

  Slot 1 [main]
    USDT:    14.32 (avail: 8.15, frozen: 6.17)
    open positions: 1
      • ZEC_USDT LONG 58c @ 432.18  PnL: +$0.42 (+0.84%)

  Slot 2 [sub-test]
    USDT:    9.85 (avail: 9.85, frozen: 0.00)
    open positions: 0

  Slot 3..5: empty / disabled
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def _fmt_usdt(x: float) -> str:
    """Format USDT amount nicely."""
    if abs(x) < 0.01:
        return f"{x:.4f}"
    if abs(x) < 1:
        return f"{x:.3f}"
    return f"{x:,.2f}"


async def _query_slot_balance(client_pool, slot_id: int) -> dict | None:
    """Query a single slot. Returns dict with balance + positions, or None on failure."""
    try:
        client = await asyncio.wait_for(
            client_pool.get(slot_id),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning("Slot %d: failed to acquire client: %s", slot_id, e)
        return None

    if client is None:
        return None

    try:
        # Fetch in parallel: balance + positions
        balance_resp, positions_resp = await asyncio.gather(
            asyncio.wait_for(client.get_account_assets(), timeout=10.0),
            asyncio.wait_for(client.get_open_positions(), timeout=10.0),
            return_exceptions=True,
        )
    except Exception as e:
        logger.warning("Slot %d: query failed: %s", slot_id, e)
        return None

    # Handle individual exceptions
    if isinstance(balance_resp, Exception):
        return {"error": f"balance: {type(balance_resp).__name__}"}
    if isinstance(positions_resp, Exception):
        # Positions failed but we got balance — partial info
        positions_resp = {"code": -1, "data": []}

    if balance_resp.get("code") != 0:
        return {"error": f"API code={balance_resp.get('code')} msg={balance_resp.get('msg')}"}

    # Parse balance — find USDT entry
    usdt_balance = {"available": 0.0, "frozen": 0.0, "total": 0.0}
    for asset in balance_resp.get("data", []) or []:
        if asset.get("currency") == "USDT":
            avail = float(asset.get("availableBalance", 0))
            frozen = float(asset.get("frozenBalance", 0))
            usdt_balance = {
                "available": avail,
                "frozen": frozen,
                "total": avail + frozen,
            }
            break

    # Parse positions
    positions = []
    if positions_resp.get("code") == 0:
        for pos in positions_resp.get("data", []) or []:
            try:
                hold_vol = int(float(pos.get("holdVol", 0) or 0))
                if hold_vol == 0:
                    continue  # closed position, skip
                positions.append({
                    "symbol": pos.get("symbol", "?"),
                    "side": "LONG" if pos.get("positionType") == 1 else "SHORT",
                    "vol": hold_vol,
                    "leverage": int(pos.get("leverage", 0)),
                    "avg_open_price": float(pos.get("openAvgPrice", 0)),
                    "mark_price": float(pos.get("holdAvgPrice", 0)),  # approximation
                    "unrealized_pnl": float(pos.get("realised", 0)),  # MEXC field
                    "im": float(pos.get("im", 0)),  # initial margin
                })
            except (ValueError, TypeError) as e:
                logger.debug("Position parse error: %s", e)
                continue

    return {
        "balance": usdt_balance,
        "positions": positions,
    }


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /balance — show USDT balance + open positions for all webkey slots.

    Available via the bot only when webkey_client_pool is wired in context.bot_data.
    """
    # Get pool from bot_data (set in bot.py setup)
    client_pool = context.bot_data.get("webkey_client_pool")
    webkey_store = context.bot_data.get("webkey_store")

    if client_pool is None or webkey_store is None:
        await update.message.reply_text(
            "❌ Webkey pool/store not configured.\n"
            "Set up at least one webkey: /webkey_setup 1"
        )
        return

    # Get all slots
    all_slots = await webkey_store.list_all()
    if not all_slots:
        await update.message.reply_text(
            "📭 No webkey slots configured.\n\n"
            "Set up the first one: /webkey_setup 1"
        )
        return

    # Send loading indicator
    msg = await update.message.reply_text("💰 Querying balances...")

    # Query each slot concurrently
    enabled_slots = [s for s in all_slots if s.enabled and s.webkey is not None]

    if not enabled_slots:
        await msg.edit_text(
            "📭 No enabled slots with webkey configured.\n\n"
            "View status: /webkey"
        )
        return

    queries = await asyncio.gather(
        *[_query_slot_balance(client_pool, s.slot_id) for s in enabled_slots],
        return_exceptions=True,
    )

    # Build response
    lines = ["💰 <b>BALANCE</b> (per slot)\n"]
    total_usdt_all_slots = 0.0
    total_positions_count = 0

    for slot, result in zip(enabled_slots, queries):
        label = f" [{slot.label}]" if slot.label else ""
        lines.append(f"\n<b>Slot {slot.slot_id}</b>{label}")

        # Show persistent slot-level error if present (face verification, risk control)
        if slot.last_error and slot.last_error.startswith("⚠️"):
            lines.append(f"  {slot.last_error}")
            lines.append("  <i>(resolve on MEXC, then bot will resume)</i>")

        if isinstance(result, Exception):
            lines.append(f"  ❌ Error: {type(result).__name__}")
            continue

        if result is None:
            lines.append("  ❌ Slot unavailable (timeout / not warmed up)")
            continue

        if "error" in result:
            lines.append(f"  ❌ {result['error']}")
            continue

        bal = result["balance"]
        positions = result["positions"]

        total_usdt_all_slots += bal["total"]
        total_positions_count += len(positions)

        lines.append(
            f"  💵 USDT: <b>{_fmt_usdt(bal['total'])}</b> "
            f"(avail: {_fmt_usdt(bal['available'])}, "
            f"frozen: {_fmt_usdt(bal['frozen'])})"
        )

        if positions:
            lines.append(f"  📊 Open positions: {len(positions)}")
            for p in positions:
                pnl_str = f"{p['unrealized_pnl']:+.4f}" if p['unrealized_pnl'] else "0"
                lines.append(
                    f"    • {p['symbol']} {p['side']} "
                    f"{p['vol']}c lev={p['leverage']}x "
                    f"@ {p['avg_open_price']:.6g} "
                    f"PnL: ${pnl_str} (im=${_fmt_usdt(p['im'])})"
                )
        else:
            lines.append("  📊 No open positions")

    # Show empty/disabled slots briefly
    other_slots = [s for s in all_slots if s not in enabled_slots]
    if other_slots:
        empty_ids = ", ".join(str(s.slot_id) for s in other_slots)
        lines.append(f"\n<i>Slots {empty_ids}: empty/disabled</i>")

    # Summary
    if len(enabled_slots) > 1:
        lines.append(
            f"\n<b>📈 Total across {len(enabled_slots)} slots: "
            f"${_fmt_usdt(total_usdt_all_slots)}</b> "
            f"({total_positions_count} open pos)"
        )

    text = "\n".join(lines)

    # Telegram has 4096 char limit — truncate if too long
    if len(text) > 3900:
        text = text[:3900] + "\n\n<i>(truncated)</i>"

    await msg.edit_text(text, parse_mode="HTML")
