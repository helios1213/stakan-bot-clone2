"""Tests for the fee gate. No network.

The properties here are the ones that decide whether real money gets exposed:

  1. FAIL-CLOSED. An unreadable rate is never treated as 0%. This is the whole
     point — a rate-limited None once made 6 of 7 pairs look "unknown", and
     reading that as "fee-free" would open positions on an assumption.
  2. A non-zero maker is rejected even when taker is 0.
  3. Symbols go through the bot's alias table, so 1000PEPEUSDT and MUUSDT
     resolve instead of being silently dropped as unknown.
  4. Retries actually retry, and a late success counts.
  5. The cache serves repeats (this runs against a live account).
"""
from __future__ import annotations

import pytest

from src.execution.fee_gate import FeeGate, PairFee, to_contract_symbol


class FakeClient:
    """Returns canned tiered_fee_rate payloads; records the endpoints asked."""

    def __init__(self, payloads=None, fail_times=0):
        self.calls = []
        self.payloads = payloads or {}
        self.fail_times = fail_times

    async def _request(self, method, endpoint, **kw):
        self.calls.append(endpoint)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("rate limited")
        sym = endpoint.split("symbol=")[-1]
        if sym not in self.payloads:
            return {"code": 0, "data": {}}          # no makerFee -> unknown
        maker, taker = self.payloads[sym]
        return {"code": 0, "data": {"makerFee": maker, "takerFee": taker,
                                    "feeRateMode": "NORMAL"}}


def gate(payloads=None, **kw) -> tuple[FeeGate, FakeClient]:
    c = FakeClient(payloads or {})
    return FeeGate(c, pacing_sec=0, **kw), c


def test_symbol_mapping_uses_bot_alias_table():
    """The six pairs a naive underscore rule would lose."""
    assert to_contract_symbol("1000PEPEUSDT") == "PEPE_USDT"
    assert to_contract_symbol("MUUSDT") == "MUSTOCK_USDT"
    assert to_contract_symbol("SKHYNIXUSDT") == "SKHYNIXSTOCK_USDT"
    assert to_contract_symbol("SNDKUSDT") == "SNDKSTOCK_USDT"
    assert to_contract_symbol("SPCXUSDT") == "SPCXSTOCK_USDT"
    assert to_contract_symbol("HYPEUSDT") == "HYPE_USDT"


@pytest.mark.asyncio
async def test_zero_maker_accepted():
    g, c = gate({"HYPE_USDT": (0, 0)})
    assert await g.is_zero_maker("HYPEUSDT") is True
    fee = await g.fee("HYPEUSDT")
    assert isinstance(fee, PairFee) and fee.zero_maker and fee.zero_both


@pytest.mark.asyncio
async def test_nonzero_maker_rejected():
    g, _ = gate({"HEMI_USDT": (0.0001, 0.0004)})
    assert await g.is_zero_maker("HEMI_USDT") is False


@pytest.mark.asyncio
async def test_zero_maker_but_paid_taker_still_counts_as_zero_maker():
    """BTC_USDT on this account: makerFee=0, takerFee=0.0002.
    The strategy is maker-only, so zero_maker is the gate — but zero_both must
    report the taker cost honestly."""
    g, _ = gate({"BTC_USDT": (0, 0.0002)})
    fee = await g.fee("BTCUSDT")
    assert fee.zero_maker is True
    assert fee.zero_both is False
    assert await g.is_zero_maker("BTCUSDT") is True


@pytest.mark.asyncio
async def test_unknown_fee_fails_closed():
    """The property that keeps money safe: no data -> do not trade."""
    g, _ = gate({})                       # endpoint answers without makerFee
    assert await g.fee("HYPEUSDT") is None
    assert await g.is_zero_maker("HYPEUSDT") is False


@pytest.mark.asyncio
async def test_transport_error_fails_closed():
    g, _ = gate({"HYPE_USDT": (0, 0)}, retries=1)
    g._client.fail_times = 99
    assert await g.is_zero_maker("HYPEUSDT") is False


@pytest.mark.asyncio
async def test_retry_recovers_a_transient_miss():
    """A rate-limited first attempt must not condemn a 0% pair."""
    g, c = gate({"HYPE_USDT": (0, 0)}, retries=3)
    c.fail_times = 2                      # first two attempts blow up
    assert await g.is_zero_maker("HYPEUSDT") is True
    assert len(c.calls) == 3


@pytest.mark.asyncio
async def test_cache_avoids_refetch_and_force_bypasses():
    g, c = gate({"HYPE_USDT": (0, 0)})
    await g.is_zero_maker("HYPEUSDT")
    await g.is_zero_maker("HYPEUSDT")
    assert len(c.calls) == 1
    await g.fee("HYPEUSDT", force=True)
    assert len(c.calls) == 2
    g.refresh()
    await g.fee("HYPEUSDT")
    assert len(c.calls) == 3


@pytest.mark.asyncio
async def test_universe_filter_keeps_only_zero_maker():
    g, _ = gate({
        "HYPE_USDT": (0, 0),
        "PEPE_USDT": (0, 0),
        "HEMI_USDT": (0.0001, 0.0004),
        # MUSTOCK_USDT deliberately absent -> unknown -> excluded
    })
    keep = await g.zero_fee_universe(
        ["HYPEUSDT", "1000PEPEUSDT", "HEMI_USDT", "MUUSDT"])
    assert keep == ["HYPEUSDT", "1000PEPEUSDT"]


@pytest.mark.asyncio
async def test_universe_filter_survives_one_bad_symbol():
    """One exploding lookup must not abort the whole filter."""
    class Exploder(FakeClient):
        async def _request(self, method, endpoint, **kw):
            if "BOOM" in endpoint:
                raise RuntimeError("boom")
            return await super()._request(method, endpoint, **kw)

    c = Exploder({"HYPE_USDT": (0, 0)})
    g = FeeGate(c, pacing_sec=0, retries=1)
    keep = await g.zero_fee_universe(["HYPEUSDT", "BOOM_USDT"])
    assert keep == ["HYPEUSDT"]
