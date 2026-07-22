"""Position-watcher upgrades: event-driven exits (#1), Binance-read gate (#2),
feed-staleness guard (#3). Tests the extracted pure/async helpers + the
per-position listener, which carry all three behaviours (the watch loop just
wires them together)."""
import asyncio

import src.strategy.shadow_engine as se


# ───────────────────────── #3 staleness guard ──────────────────────────
def test_feed_stale_false_before_first_update():
    # last_update_ts_ms == 0 → book never synced yet → never "stale"
    assert se._feed_is_stale(0, 1_000_000, 2000) is False


def test_feed_stale_false_when_guard_disabled():
    assert se._feed_is_stale(500, 10_000, 0) is False     # stale_ms=0 disables
    assert se._feed_is_stale(500, 10_000, -1) is False


def test_feed_stale_false_within_window():
    # 1500ms idle < 2000ms threshold
    assert se._feed_is_stale(1000, 2500, 2000) is False


def test_feed_stale_true_beyond_window():
    # 2500ms idle > 2000ms threshold → stale
    assert se._feed_is_stale(1000, 3500, 2000) is True


def test_feed_stale_boundary_is_exclusive():
    # exactly == threshold is NOT stale (uses strict >)
    assert se._feed_is_stale(1000, 3000, 2000) is False


# ───────────────────────── #2 Binance-read gate ────────────────────────
class _Cfg:
    def __init__(self, phase0):
        self.binance_reversal_ticks = phase0


def test_needs_binance_book_off_when_reversal_disabled():
    assert se._needs_binance_book(_Cfg(0)) is False


def test_needs_binance_book_on_when_reversal_enabled():
    assert se._needs_binance_book(_Cfg(25)) is True
    assert se._needs_binance_book(_Cfg(1)) is True


def test_needs_binance_book_missing_attr_defaults_off():
    assert se._needs_binance_book(object()) is False


# ───────────────────────── #1 event-driven wait ────────────────────────
def test_wait_none_event_falls_back_to_sleep():
    async def run():
        assert await se._wait_for_book_event(None, 0.01) is False
    asyncio.run(run())


def test_wait_preset_event_returns_true_and_clears():
    async def run():
        ev = asyncio.Event(); ev.set()
        woke = await se._wait_for_book_event(ev, 1.0)
        assert woke is True
        assert not ev.is_set()          # consumed/cleared for next iteration
    asyncio.run(run())


def test_wait_times_out_when_no_update():
    async def run():
        ev = asyncio.Event()            # never set
        woke = await se._wait_for_book_event(ev, 0.01)
        assert woke is False
    asyncio.run(run())


def test_wait_wakes_on_mid_wait_update():
    async def run():
        ev = asyncio.Event()
        async def _set_soon():
            await asyncio.sleep(0.01); ev.set()
        t = asyncio.create_task(_set_soon())
        woke = await se._wait_for_book_event(ev, 1.0)   # long timeout
        await t
        assert woke is True             # woken by event, not timeout
    asyncio.run(run())


# ───────────────────────── #1 per-position listener ────────────────────
class _FakeOB:
    def __init__(self, price):
        self._price = price
    def executable_exit_price(self, direction):
        return self._price


class _FakePos:
    def __init__(self, *, is_open=True, is_closing=False):
        self.symbol = "1000PEPEUSDT"
        self.direction = "long"
        self.is_open = is_open
        self.is_closing = is_closing
        self._book_event = asyncio.Event()
        self.peaks = []
    def update_peak_only(self, price):
        self.peaks.append(price)


def _build_listener(pos):
    # _make_position_listener doesn't use self → call unbound with None.
    return se.ShadowEngine._make_position_listener(None, pos)


def test_listener_updates_peak_and_wakes_loop_when_open():
    pos = _FakePos()
    _build_listener(pos)(_FakeOB(0.0029273))
    assert pos.peaks == [0.0029273]          # peak tracked at WS resolution
    assert pos._book_event.is_set()           # loop will wake → exit check


def test_listener_noop_when_position_closed():
    pos = _FakePos(is_open=False)
    _build_listener(pos)(_FakeOB(0.0029273))
    assert pos.peaks == []
    assert not pos._book_event.is_set()        # no spurious wake


def test_listener_noop_when_position_closing():
    pos = _FakePos(is_closing=True)
    _build_listener(pos)(_FakeOB(0.0029273))
    assert pos.peaks == []
    assert not pos._book_event.is_set()


def test_listener_skips_peak_on_empty_book_but_still_wakes():
    # price None (book not ready) → no peak update, but loop still wakes so it
    # can re-evaluate time-based exits / staleness on the next pass.
    pos = _FakePos()
    _build_listener(pos)(_FakeOB(None))
    assert pos.peaks == []
    assert pos._book_event.is_set()


# ───────────────────── entry-spread logging ─────────────────────
def test_entry_spread_bps_normal():
    # bid 100.00 / ask 100.03 → 0.03 / 100.015 * 1e4 ≈ 3.0 bps
    assert abs(se._entry_spread_bps(100.00, 100.03) - 2.9996) < 0.01


def test_entry_spread_bps_tight_book():
    # 1-tick book on PEPE-scale price
    s = se._entry_spread_bps(0.0029450, 0.0029451)
    assert 0.3 < s < 0.4          # ~0.34 bps (1 tick)


def test_entry_spread_bps_not_ready_returns_zero():
    assert se._entry_spread_bps(0.0, 100.0) == 0.0      # no bid
    assert se._entry_spread_bps(100.0, 0.0) == 0.0      # no ask
    assert se._entry_spread_bps(100.05, 100.0) == 0.0   # crossed (ask<bid)


# ─────────────── capped reversal-close limit price ───────────────
def test_capped_close_long_sells_below_bid():
    # close LONG = SELL → cross DOWN to bid − cap. tick 1e-7, cap 10 → −1e-6.
    p = se._capped_close_limit_scaled("long", 0.0029450, 0.0029451, 10, 1e-7)
    assert abs(p - 0.0029440) < 1e-12


def test_capped_close_short_buys_above_ask():
    # close SHORT = BUY → cross UP to ask + cap.
    p = se._capped_close_limit_scaled("short", 0.0029450, 0.0029451, 10, 1e-7)
    assert abs(p - 0.0029461) < 1e-12


def test_capped_close_cap_zero_is_at_touch():
    assert se._capped_close_limit_scaled("long", 100.0, 100.1, 0, 0.01) == 100.0    # bid
    assert se._capped_close_limit_scaled("short", 100.0, 100.1, 0, 0.01) == 100.1   # ask


def test_capped_close_book_not_ready_returns_zero():
    assert se._capped_close_limit_scaled("long", 0.0, 100.1, 10, 0.01) == 0.0   # no bid
    assert se._capped_close_limit_scaled("long", 100.0, 100.1, 10, 0.0) == 0.0  # no tick

# ─────────────── gap-relative binance_reversal trigger ───────────────
def test_gap_relative_long_full_retrace():
    assert abs(se._gap_relative_trigger_scaled("long", 100.0, 10, 0.01, 1.0) - 100.0) < 1e-9


def test_gap_relative_long_partial():
    # frac=0.7, gap 10t, tick 0.01 -> entry + 10*0.01*0.3 = 100.03
    assert abs(se._gap_relative_trigger_scaled("long", 100.0, 10, 0.01, 0.7) - 100.03) < 1e-9


def test_gap_relative_short_partial():
    assert abs(se._gap_relative_trigger_scaled("short", 100.0, 10, 0.01, 0.7) - 99.97) < 1e-9


def test_gap_relative_inactive_returns_zero():
    assert se._gap_relative_trigger_scaled("long", 100.0, 0, 0.01, 0.7) == 0.0
    assert se._gap_relative_trigger_scaled("long", 100.0, 10, 0.01, 0.0) == 0.0
    assert se._gap_relative_trigger_scaled("long", 0.0, 10, 0.01, 0.7) == 0.0


def test_needs_binance_book_on_when_gap_retrace_set():
    class _C:
        binance_reversal_ticks = 0
        gap_retrace_frac = 0.7
    assert se._needs_binance_book(_C()) is True
