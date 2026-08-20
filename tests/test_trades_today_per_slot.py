"""Trades Today: LIVE must be broken down PER SLOT.

The bug this pins: one pair can be assigned to slot 1 and slot 2 at the same
time. Summing them into a single row per symbol hides which ACCOUNT made or
lost the money — and on real data the two slots differ a lot on the very same
pair. `shadow_engine`'s heartbeat already keys by (symbol, account_label) for
exactly this reason; this is the same fix for the Telegram view.
"""
from __future__ import annotations

import pytest

from src.telegram_bot.bot import render_live_section, slot_no


def row(symbol, label, n, wins, pnl):
    return {"symbol": symbol, "account_label": label, "n": n, "wins": wins, "pnl": pnl}


# ---- slot_no: the two tables spell the slot differently -------------------

def test_slot_no_reads_both_spellings():
    # live_trades.account_label is a STRING, live_open_misses.slot_id an INT.
    assert slot_no("slot2") == "2"
    assert slot_no(2) == "2"
    assert slot_no("2") == "2"


def test_slot_no_never_returns_none():
    """An unlabelled trade must still be shown, never silently dropped."""
    assert slot_no(None) == "?"
    assert slot_no("") == "?"
    assert slot_no("no-digits-here") == "?"


# ---- the actual feature --------------------------------------------------

def test_same_pair_on_two_slots_is_not_merged():
    rows = [
        row("1000PEPEUSDT", "slot1", 424, 267, 9.475),
        row("1000PEPEUSDT", "slot2", 30, 11, 0.426),
    ]
    out = render_live_section("LIVE", rows, {})
    joined = "\n".join(out)
    assert "slot 1" in joined and "slot 2" in joined
    # the SAME symbol appears once under each slot, with its own PnL
    assert joined.count("1000PEPEUSDT") == 2
    assert "$+9.475" in joined
    assert "$+0.426" in joined
    # The merged 9.901 is legitimate in the SECTION header, but must never be
    # the number shown against the pair itself — that is the bug being fixed.
    pair_lines = [l for l in out if "1000PEPEUSDT" in l]
    assert len(pair_lines) == 2
    assert not any("$+9.901" in l for l in pair_lines)


def test_per_slot_subtotals_add_up_to_the_section_total():
    rows = [
        row("SOXLUSDT", "slot1", 277, 122, 79.847),
        row("1000PEPEUSDT", "slot1", 424, 267, 9.475),
        row("SOXLUSDT", "slot2", 369, 122, 27.180),
    ]
    out = render_live_section("LIVE", rows, {})
    assert "1070 trades" in out[0]            # 277+424+369
    assert "$+116.502" in out[0]              # 79.847+9.475+27.180
    slot_lines = [l for l in out if "slot " in l]
    assert "701 trades" in slot_lines[0]      # slot 1
    assert "$+89.322" in slot_lines[0]
    assert "369 trades" in slot_lines[1]      # slot 2
    assert "$+27.180" in slot_lines[1]


def test_expired_pct_is_attributed_to_the_RIGHT_slot():
    """exp% keyed by symbol alone would smear one slot's misses onto the other."""
    rows = [
        row("SOXLUSDT", "slot1", 10, 5, 1.0),
        row("SOXLUSDT", "slot2", 10, 5, 1.0),
    ]
    # every miss belongs to slot 2
    out = "\n".join(render_live_section("LIVE", rows, {("SOXLUSDT", "2"): 90}))
    lines = [l for l in out.split("\n") if "SOXLUSDT" in l]
    assert "exp=  0%" in lines[0], "slot 1 had no misses"
    assert "exp= 90%" in lines[1], "slot 2 had 90 of 100 attempts expire"


def test_unlabelled_trades_are_shown_not_dropped():
    rows = [row("HYPEUSDT", "slot1", 5, 3, 1.0), row("HYPEUSDT", None, 2, 1, -0.5)]
    out = render_live_section("LIVE", rows, {})
    assert "7 trades" in out[0], "the unlabelled pair must still count in the total"
    joined = "\n".join(out)
    assert "unlabelled" in joined
    # real slots read first, '?' last
    assert joined.index("slot 1") < joined.index("unlabelled")


def test_empty_section_still_renders():
    assert render_live_section("LIVE", [], {}) == ["LIVE — <i>none</i>"]


def test_null_pnl_does_not_crash():
    """SUM() over an empty group yields NULL; the view must survive it."""
    out = render_live_section("LIVE", [row("XRPUSDT", "slot1", 1, 0, None)], {})
    assert "$+0.000" in "\n".join(out)


@pytest.mark.parametrize("pnl,marker", [(1.0, "🟢"), (-1.0, "🔴"), (0.0, "➖")])
def test_slot_subtotal_colour_follows_its_own_pnl(pnl, marker):
    out = render_live_section("LIVE", [row("ZECUSDT", "slot1", 1, 1, pnl)], {})
    slot_line = [l for l in out if "slot 1" in l][0]
    assert slot_line.lstrip().startswith(marker)
