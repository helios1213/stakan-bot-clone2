"""bookTicker top-of-book application — the 2026-08-13 crossed-book bug.

Feeding the detector from Binance's bookTicker stream was switched off
(`book_ticker_feed_enabled: false`) because it produced ~3000 false crossed
books/hr. The cause was NOT the feed: bookTicker names the whole top, but the
old code inserted the new top WITHOUT removing the levels it supersedes, and
then took max(_bids)/min(_asks) over that unpruned ladder. A price that ticked
DOWN left the old, higher bid in place — and a stale bid above a fresh ask is a
crossed book, which makes the detector skip the signal.

These tests pin the pruning, the staleness guard, and the invariant itself.
"""
from __future__ import annotations

from src.exchanges.orderbook import OrderBook


def mk() -> OrderBook:
    ob = OrderBook(symbol="HYPEUSDT", exchange="binance")
    ob.apply_snapshot(
        bids=[(100.4, 5), (100.3, 5), (100.2, 5)],
        asks=[(100.5, 5), (100.6, 5), (100.7, 5)],
        update_id=1,
    )
    return ob


def test_price_ticking_down_does_not_cross_the_book():
    """THE regression. Old code left the 100.4 bid and crossed against a 100.1 ask."""
    ob = mk()
    moved = ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=2)
    assert moved is True
    assert ob.best_bid().price == 100.0
    assert ob.best_ask().price == 100.1
    assert not ob.is_crossed(), "a stale higher bid must not survive the update"
    assert 100.4 not in ob._bids, "levels better than the new top are gone"
    assert 100.3 not in ob._bids


def test_price_ticking_up_prunes_stale_asks():
    ob = mk()
    ob.apply_top_of_book(100.8, 3, 100.9, 3, update_id=2)
    assert ob.best_bid().price == 100.8
    assert ob.best_ask().price == 100.9
    assert not ob.is_crossed()
    assert 100.5 not in ob._asks and 100.6 not in ob._asks


def test_depth_below_the_top_is_preserved():
    """bookTicker owns the top only — the deep ladder still belongs to depth."""
    ob = mk()
    ob.apply_top_of_book(100.35, 3, 100.55, 3, update_id=2)
    assert 100.3 in ob._bids and 100.2 in ob._bids, "deeper bids survive"
    assert 100.6 in ob._asks and 100.7 in ob._asks, "deeper asks survive"
    assert ob._bids[100.35] == 3


def test_a_stale_frame_is_ignored():
    """Two streams, no shared ordering: a late frame must not prune live levels."""
    ob = mk()
    ob.apply_top_of_book(100.8, 3, 100.9, 3, update_id=10)
    assert ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=9) is False
    assert ob.best_bid().price == 100.8, "the newer top stands"
    assert ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=10) is False, "same id"


def test_a_crossed_quote_is_refused_outright():
    ob = mk()
    assert ob.apply_top_of_book(100.6, 3, 100.5, 3, update_id=2) is False
    assert ob.best_bid().price == 100.4, "the book is untouched"
    assert not ob.is_crossed()


def test_zero_or_missing_size_is_refused():
    ob = mk()
    for args in ((100.0, 0, 100.1, 3), (100.0, 3, 100.1, 0),
                 (0, 3, 100.1, 3), (100.0, 3, 0, 3)):
        assert ob.apply_top_of_book(*args, update_id=2) is False
    assert ob.best_bid().price == 100.4


def test_restating_the_same_top_reports_no_move():
    """Most frames only repeat a top we have — they must not wake listeners."""
    ob = mk()
    assert ob.apply_top_of_book(100.4, 9, 100.5, 9, update_id=2) is False
    assert ob._bids[100.4] == 9, "but the size is still refreshed"


def test_sync_state_belongs_to_depth():
    ob = mk()
    before = (ob.last_update_id, ob.is_synced)
    ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=2, event_ts_ms=1787000000000)
    assert (ob.last_update_id, ob.is_synced) == before
    assert ob.top_lead_ts_ms == 1787000000000


def test_a_thousand_alternating_ticks_never_cross():
    """The failure was rate-dependent: it showed up ~3000x/hr on fast moves."""
    import random

    ob = mk()
    rng = random.Random(7)          # deterministic walk, but it really wanders
    px = 100.0
    lo = hi = px
    for i in range(1000):
        px = round(px + rng.choice((-0.1, -0.1, 0.1, 0.1, 0.2, -0.2)), 4)
        lo, hi = min(lo, px), max(hi, px)
        ob.apply_top_of_book(px, 2, round(px + 0.1, 4), 2, update_id=10 + i)
        assert not ob.is_crossed(), f"crossed after {i} ticks at {px}"
        assert ob.best_bid().price == px
    assert hi - lo > 1.0, "the walk must actually drift, or this proves nothing"
    # Pruning is what bounds the ladder: without it every visited price stays.
    assert len(ob._bids) < 100 and len(ob._asks) < 100, \
        f"ladder grew to {len(ob._bids)}/{len(ob._asks)} — pruning is not working"


# ---- the OTHER direction: a late depth diff undoing bookTicker -------------

def test_a_late_depth_diff_cannot_resurrect_a_pruned_level():
    """The residual that still crossed the book in production on 2026-08-20.

    depth@100ms lags bookTicker by 10-50ms. Pruning on the bookTicker side is
    not enough: the diff that arrives afterwards still carries the old world
    and puts the stale bid straight back above the fresh ask.
    """
    ob = mk()
    ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=500)
    # a diff from BEFORE that quote — it still believes 100.4 is bid
    ob.apply_diff(bids=[(100.4, 5)], asks=[], first_update_id=480, final_update_id=490)
    assert not ob.is_crossed(), "an older diff must not override a newer top"
    assert ob.best_bid().price == 100.0
    assert 100.4 not in ob._bids


def test_a_newer_depth_diff_still_wins():
    """The clamp must not freeze the book: depth is authoritative once ahead."""
    ob = mk()
    ob.apply_top_of_book(100.0, 3, 100.1, 3, update_id=500)
    ob.apply_diff(bids=[(100.05, 5)], asks=[], first_update_id=501, final_update_id=510)
    assert ob.best_bid().price == 100.05, "a newer diff moves the book"
    assert not ob.is_crossed()


def test_the_clamp_is_inert_without_bookticker():
    """Feed off: depth owns everything and nothing is pruned behind its back."""
    ob = mk()
    ob.apply_diff(bids=[(100.45, 5)], asks=[], first_update_id=2, final_update_id=3)
    assert ob.best_bid().price == 100.45
    assert not ob.is_crossed()
