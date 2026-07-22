"""SpreadMonitor: scaled spread-in-ticks formula + hysteresis/debounce."""
import src.strategy.spread_monitor as sm


class _FakeOB:
    is_synced = True

    def __init__(self, bid, ask):
        self._b, self._a = bid, ask

    def best_bid_price(self):
        return self._b

    def best_ask_price(self):
        return self._a


class _FakeMgr:
    def __init__(self, ob):
        self._ob = ob

    def get(self, exchange, symbol):
        return self._ob

    def all_symbols(self, exchange):
        return ["X"]


def test_spread_ticks_uses_scaled_tick(monkeypatch):
    # tick 0.01, scale 1.0 -> tick_scaled 0.01; (100.03-100.00)/0.01 = 3 ticks
    monkeypatch.setattr(sm, "get_tick_size", lambda s: 0.01)
    monkeypatch.setattr(sm, "get_binance_scale", lambda s: 1.0)
    monkeypatch.setattr(sm, "to_mexc", lambda s: s)
    m = sm.SpreadMonitor(_FakeMgr(_FakeOB(100.00, 100.03)), None)
    st, bid, ask = m.spread_ticks("X")
    assert abs(st - 3.0) < 1e-9
    assert (bid, ask) == (100.00, 100.03)


def test_spread_ticks_with_scale_factor(monkeypatch):
    # scale 10 -> tick_scaled 0.1; (200.2-200.0)/0.1 = 2 ticks
    monkeypatch.setattr(sm, "get_tick_size", lambda s: 0.01)
    monkeypatch.setattr(sm, "get_binance_scale", lambda s: 10.0)
    monkeypatch.setattr(sm, "to_mexc", lambda s: s)
    m = sm.SpreadMonitor(_FakeMgr(_FakeOB(200.0, 200.2)), None)
    st, _, _ = m.spread_ticks("X")
    assert abs(st - 2.0) < 1e-9


def test_spread_ticks_none_when_book_not_ready(monkeypatch):
    monkeypatch.setattr(sm, "to_mexc", lambda s: s)
    ob = _FakeOB(0.0, 0.0)  # bid<=0
    assert sm.SpreadMonitor(_FakeMgr(ob), None).spread_ticks("X") is None
    ob2 = _FakeOB(1.0, 2.0); ob2.is_synced = False
    assert sm.SpreadMonitor(_FakeMgr(ob2), None).spread_ticks("X") is None


def test_hysteresis_debounce_transitions():
    m = sm.SpreadMonitor(None, None, wide_ticks=3.0, recover_ticks=2.0, debounce_sec=3.0)
    s = "ZECUSDT"
    assert m._evaluate(s, 1.0, 0.0) is None      # tight & healthy
    assert m._evaluate(s, 3.5, 0.0) is None       # wide observed -> pending
    assert m._evaluate(s, 3.5, 1.0) is None        # debounce not met
    assert m._evaluate(s, 3.5, 3.1) == "wide"      # debounce met -> FIRE wide
    assert m._evaluate(s, 3.5, 4.0) is None         # stays wide -> no repeat
    assert m._evaluate(s, 2.0, 5.0) is None          # recover observed -> pending
    assert m._evaluate(s, 2.0, 8.2) == "tight"       # debounce met -> FIRE recover
    assert m._evaluate(s, 1.0, 9.0) is None           # stays tight


def test_flapping_spike_does_not_fire():
    m = sm.SpreadMonitor(None, None, wide_ticks=3.0, recover_ticks=2.0, debounce_sec=3.0)
    assert m._evaluate("Y", 3.5, 0.0) is None   # pending wide
    assert m._evaluate("Y", 1.0, 1.0) is None    # spike ended -> pending cleared
    assert m._evaluate("Y", 3.5, 2.0) is None     # pending restarts
    assert m._evaluate("Y", 3.5, 4.0) is None      # only 2s since restart < 3s
    assert m._evaluate("Y", 3.5, 5.1) == "wide"    # now debounce met
