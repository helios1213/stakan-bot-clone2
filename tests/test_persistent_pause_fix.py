"""Tests for persistent-pause fix: manual pauses never auto-resume."""

from src.state.pair_state import PairState
from src.state.transitions import evaluate_paused, TransitionCriteria
import time


def _make_paused_state(
    symbol: str = "TESTUSDT",
    pause_reason: str | None = None,
    paused_until: int | None = None,
) -> PairState:
    now = int(time.time())
    ps = PairState(
        symbol=symbol,
        state="paused",
        state_since=now - 3600,
        discovered_at=now - 86400,
    )
    ps.paused_until = paused_until
    ps.pause_reason = pause_reason
    return ps


def _dummy_metrics():
    from src.state.metrics_calculator import PairMetrics
    return PairMetrics(symbol="TESTUSDT")


class TestEvaluatePaused:
    """evaluate_paused must respect manual pauses."""

    def test_manual_pause_never_resumes_even_with_expired_until(self):
        """Manual pause with expired paused_until must NOT auto-resume."""
        ps = _make_paused_state(
            pause_reason="manual: shadow toggle off",
            paused_until=int(time.time()) - 86400,  # expired yesterday
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition, "Manual pause should never auto-resume"

    def test_manual_pause_persistent_no_resume(self):
        """Manual pause with paused_until=None must NOT auto-resume."""
        ps = _make_paused_state(
            pause_reason="manual: shadow toggle off",
            paused_until=None,
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition

    def test_manual_re_paused_never_resumes(self):
        """Re-paused pairs (manual: re-paused) must NOT auto-resume."""
        ps = _make_paused_state(
            pause_reason="manual: re-paused",
            paused_until=None,
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition

    def test_manual_testing_pause_never_resumes(self):
        """Testing-related manual pauses must NOT auto-resume."""
        ps = _make_paused_state(
            pause_reason="manual: testing PENGU only",
            paused_until=int(time.time()) - 3600,  # expired
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition

    def test_auto_pause_resumes_when_expired(self):
        """Auto pause (non-manual) with expired paused_until SHOULD resume."""
        ps = _make_paused_state(
            pause_reason="demotion: 24h_pnl=-5.00<0",
            paused_until=int(time.time()) - 3600,  # expired 1h ago
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert result.should_transition
        assert result.target_state == "shadow"

    def test_auto_pause_does_not_resume_before_expiry(self):
        """Auto pause with future paused_until should NOT resume yet."""
        ps = _make_paused_state(
            pause_reason="demotion: 24h_pnl=-5.00<0",
            paused_until=int(time.time()) + 3600,  # expires in 1h
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition

    def test_persistent_pause_no_reason_never_resumes(self):
        """Persistent pause with no reason and no expiry must NOT resume."""
        ps = _make_paused_state(
            pause_reason=None,
            paused_until=None,
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert not result.should_transition

    def test_null_reason_with_expired_until_resumes(self):
        """Pause with no reason but expired paused_until SHOULD resume.
        
        This covers legacy/edge cases where pause_reason wasn't set
        but a timed pause was created programmatically.
        """
        ps = _make_paused_state(
            pause_reason=None,
            paused_until=int(time.time()) - 3600,
        )
        result = evaluate_paused(ps, _dummy_metrics(), TransitionCriteria())
        assert result.should_transition
        assert result.target_state == "shadow"
