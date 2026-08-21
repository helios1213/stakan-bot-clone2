"""The live order path must stay independent of every shadow-realism knob.

Every honesty fix we add to shadow is a chance to accidentally tax the live
path — and the live path is where the money is. The audit added a feed-lag
sleep, a stale-book guard and extra bookkeeping in the same functions live
orders travel through; nothing today proves the next one will be guarded too.

So this file does not test behaviour, it tests SHAPE: any `asyncio.sleep` on
the signal→submit path must sit inside a `not _is_live_pair` branch. A sleep
that escapes that guard delays real orders, and at ~160ms round-trip a few
tens of milliseconds is a measurable share of the fill probability.

Measured 2026-08-21 after the day's changes — the live path was unaffected:
  http POST /order/create p50 158ms (before) → 156.6ms (after)
  signal_to_submit p50 5.4ms → 6.9ms, inside the day's own 5.4–14.1 spread
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

SRC = Path("src/strategy/shadow_engine.py").read_text()
TREE = ast.parse(SRC)


def _func(name: str) -> ast.AST:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — did it get renamed?")


def _guards_live(test: ast.AST) -> bool:
    """True if this `if` test keeps live pairs OUT of the branch."""
    src = ast.unparse(test)
    return "not _is_live_pair" in src or "_is_live_pair is False" in src


def _sleeps_outside_shadow_guard(func: ast.AST) -> list[str]:
    """Every asyncio.sleep in `func` that is not under a `not _is_live_pair` if."""
    offenders: list[str] = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.guarded = 0

        def visit_If(self, node: ast.If):
            if _guards_live(node.test):
                self.guarded += 1
                for n in node.body:
                    self.visit(n)
                self.guarded -= 1
                for n in node.orelse:
                    self.visit(n)
            else:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call):
            name = ast.unparse(node.func)
            if name.endswith("asyncio.sleep") and not self.guarded:
                offenders.append(f"line {node.lineno}: {ast.unparse(node)[:70]}")
            self.generic_visit(node)

    V().visit(func)
    return offenders


def test_no_unguarded_sleep_before_the_attempt_loop():
    """The one that matters: everything here runs before the order is built,
    so a sleep is paid by every real order. This is where the feed-lag wait
    went, and where the next realism knob will want to go too."""
    fn = _func("_try_enter")
    loop = next(n for n in ast.walk(fn)
                if isinstance(n, ast.For) and "attempt" in ast.unparse(n.target))
    head = ast.Module(body=[n for n in fn.body
                            if getattr(n, "lineno", 0) < loop.lineno],
                      type_ignores=[])
    offenders = _sleeps_outside_shadow_guard(head)
    assert not offenders, (
        "asyncio.sleep on the signal→submit path outside a `not _is_live_pair` "
        "guard — live orders would pay it:\n  " + "\n  ".join(offenders)
    )


def test_live_stub_is_always_filled_so_the_retry_sleep_is_unreachable():
    """Inside the attempt loop there IS a retry sleep, and it is safe only
    because the live branch hands back a `filled` stub and returns on the first
    pass. Weaken that stub and live starts paying retry latency — silently."""
    i = SRC.index("if _is_live_pair:")
    # NB: the branch contains an inner if/else (long vs short), so the first
    # `else:` after `i` is NOT the one closing the live branch. Bound the slice
    # by the shadow simulator call instead — that is unambiguously the `else`.
    stub = SRC[i:SRC.index("simulate_ioc_entry", i)]
    assert 'status="filled"' in stub, (
        "live stub no longer reports `filled` — live would now fall through to "
        "the retry sleep inside the attempt loop"
    )
    tail = SRC[SRC.index("await self._open_position", i):][:600]
    assert "return" in tail, "live no longer returns after the first attempt"


def test_the_shadow_realism_knobs_are_all_guarded():
    """Each knob must appear only in shadow-only territory."""
    fn = _func("_try_enter")
    body = ast.unparse(fn)
    for knob in ("_mexc_feed_lag_ms", "_latency_enabled", "should_reject_order",
                 "simulate_server_error"):
        assert knob in body, f"{knob} vanished — update this test with the new name"
    # the feed-lag wait specifically
    i = body.index("_mexc_feed_lag_ms")
    window = body[max(0, i - 400):i + 200]
    assert "not _is_live_pair" in window, (
        "the feed-lag wait lost its live guard — every live order would sleep"
    )


def test_stale_book_guard_is_not_applied_to_live():
    """max_book_age_ms may only reach the shadow simulator, never the live stub."""
    j = SRC.index("max_book_age_ms=self._max_book_age_ms")
    assert SRC.rfind("else:", 0, j) > SRC.rfind("if _is_live_pair:", 0, j)


def test_live_branch_still_skips_the_ladder_walk():
    """The walk cost 5-15ms per signal — that is why live builds a stub instead.
    If someone deletes the stub, live pays it again."""
    i = SRC.index("if _is_live_pair:")
    seg = SRC[i:i + 2000]
    assert "IOCAttemptResult(" in seg, "live fast-path stub is gone"
    assert "simulate_ioc_entry" not in seg.split("else:")[0], (
        "live branch now walks the ladder — that is the 5-15ms we removed"
    )


def test_booklag_sampling_cannot_raise_into_the_order_path():
    """It runs ON the live fill path by design — so it must be unable to throw."""
    i = SRC.index("[BOOKLAG]")
    seg = SRC[max(0, i - 900):i + 900]
    assert "try:" in seg and "except Exception:" in seg


def test_phantom_gate_is_a_cheap_lookup():
    """Guarding the open must not cost a round-trip — a set membership only."""
    ex = Path("src/execution/live_executor.py").read_text()
    i = ex.index("if symbol in self._phantom_unknown:")
    head = ex[max(0, i - 250):i]
    assert "await" not in head.split("\n")[-1], "no awaits immediately before the gate"


@pytest.mark.parametrize("name", ["_try_enter", "_open_position"])
def test_entry_functions_still_exist(name):
    """Guard against this whole file silently passing after a rename."""
    assert _func(name) is not None
