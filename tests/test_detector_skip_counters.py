"""Skip counters must tell a BUG apart from a normal condition.

`signals_skip_no_ob` used to lump four unrelated causes into one number:
a book that is not synced yet (normal at startup), a zero price, a crossed
MEXC book, and a crossed BINANCE book. Only the last two are bugs — the
crossed-Binance one is exactly what the bookTicker feed produced ~3000×/hr
on 2026-08-13 — and none of them appeared in [GATE_SKIPS]. The crossed-book
log line is rate-limited to one per 60s, so without these counters the real
rate is unknowable: 1 log line can mean 1 event or 3000.
"""
from __future__ import annotations

import inspect

from src.strategy import static_gap_detector as sgd


class _Counters:
    """Just the counter surface of the detector, without building a real one."""

    def __init__(self):
        self.signals_skip_unsynced = 0
        self.signals_skip_zero_price = 0
        self.signals_skip_crossed_mexc = 0
        self.signals_skip_crossed_binance = 0

    signals_skip_no_ob = sgd.StaticGapDetector.signals_skip_no_ob


def test_no_ob_still_reports_the_old_total():
    """External consumers (panel, stats) must not see a number that shrank."""
    c = _Counters()
    assert c.signals_skip_no_ob == 0
    c.signals_skip_unsynced = 3
    c.signals_skip_zero_price = 5
    c.signals_skip_crossed_mexc = 7
    c.signals_skip_crossed_binance = 11
    assert c.signals_skip_no_ob == 26


def test_the_four_causes_are_separately_counted():
    src = inspect.getsource(sgd.StaticGapDetector)
    for name in ("signals_skip_unsynced", "signals_skip_zero_price",
                 "signals_skip_crossed_mexc", "signals_skip_crossed_binance"):
        assert f"self.{name} += 1" in src, f"{name} is never incremented"
    assert "self.signals_skip_no_ob += 1" not in src, \
        "the summed counter must not be written to directly"


def test_a_crossed_book_is_attributed_to_the_right_side():
    """MEXC-crossed and Binance-crossed are different failures; keep them apart."""
    src = inspect.getsource(sgd.StaticGapDetector)
    mexc = src.index("if m_bid_p >= m_ask_p:")
    binance = src.index("if b_bid_p >= b_ask_p:")
    assert "self.signals_skip_crossed_mexc += 1" in src[mexc:mexc + 200]
    assert "self.signals_skip_crossed_binance += 1" in src[binance:binance + 200]


def test_gate_skips_reports_the_crossed_counters():
    """A bug you cannot count is a bug you cannot claim to have fixed."""
    src = inspect.getsource(sgd.StaticGapDetector._log_gate_skips)
    for key in ("crossed_binance", "crossed_mexc", "unsynced", "zero_price"):
        assert f'"{key}"' in src, f"[GATE_SKIPS] does not report {key}"


def test_stats_exposes_the_breakdown():
    src = inspect.getsource(sgd.StaticGapDetector.stats)
    for key in ("signals_skip_no_ob", "signals_skip_crossed_binance",
                "signals_skip_crossed_mexc"):
        assert key in src
