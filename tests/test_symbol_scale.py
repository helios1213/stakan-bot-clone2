"""
Tests for cross-exchange symbol scale normalisation.

Some tokens trade on Binance as "1000X" (a contract == 1000 base units),
while MEXC uses native units. We need:
  1) Symbol alias: 1000PEPEUSDT <-> PEPE_USDT
  2) Price scale: multiply MEXC raw price by N to get Binance-equivalent
"""
from __future__ import annotations

from src.exchanges.mexc_rest import (
    BINANCE_TO_MEXC_ALIASES,
    SYMBOL_SCALE_TO_BINANCE,
    get_binance_scale,
    to_binance,
    to_mexc,
)


class TestAliases:
    def test_normal_pair_to_mexc(self):
        assert to_mexc("BTCUSDT") == "BTC_USDT"
        assert to_mexc("ETHUSDT") == "ETH_USDT"

    def test_normal_pair_to_binance(self):
        assert to_binance("BTC_USDT") == "BTCUSDT"
        assert to_binance("SOL_USDT") == "SOLUSDT"

    def test_aliased_pump(self):
        # PUMP is renamed to PUMPFUN on MEXC
        assert to_mexc("PUMPUSDT") == "PUMPFUN_USDT"
        assert to_binance("PUMPFUN_USDT") == "PUMPUSDT"

    def test_aliased_1000pepe(self):
        # Binance trades 1000PEPEUSDT (1 contract = 1000 PEPE),
        # MEXC trades PEPE_USDT (1 contract = N PEPE)
        assert to_mexc("1000PEPEUSDT") == "PEPE_USDT"
        assert to_binance("PEPE_USDT") == "1000PEPEUSDT"

    def test_aliased_1000shib(self):
        assert to_mexc("1000SHIBUSDT") == "SHIB_USDT"
        assert to_binance("SHIB_USDT") == "1000SHIBUSDT"

    def test_aliased_10000sats(self):
        assert to_mexc("10000SATSUSDT") == "SATS_USDT"
        assert to_binance("SATS_USDT") == "10000SATSUSDT"

    def test_aliases_are_bidirectional(self):
        # Every alias must round-trip
        for binance_sym, mexc_sym in BINANCE_TO_MEXC_ALIASES.items():
            assert to_mexc(binance_sym) == mexc_sym, f"forward broken: {binance_sym}"
            assert to_binance(mexc_sym) == binance_sym, f"reverse broken: {mexc_sym}"

    def test_already_mexc_format_passthrough(self):
        # If we accidentally pass an already-MEXC symbol, return as-is
        assert to_mexc("BTC_USDT") == "BTC_USDT"


class TestScaleLookup:
    def test_scale_for_known_alias(self):
        assert get_binance_scale("PEPE_USDT") == 1000.0
        assert get_binance_scale("SHIB_USDT") == 1000.0
        assert get_binance_scale("SATS_USDT") == 10000.0

    def test_scale_for_normal_pair(self):
        assert get_binance_scale("BTC_USDT") == 1.0
        assert get_binance_scale("SOL_USDT") == 1.0

    def test_scale_for_unknown(self):
        # Unknown symbols default to 1.0 (no scaling)
        assert get_binance_scale("FOOBAR_USDT") == 1.0

    def test_every_alias_has_scale_or_implicit_1x(self):
        # Every aliased pair should either have an explicit scale entry
        # or implicitly default to 1.0. Verify no surprises in scale table.
        for s in SYMBOL_SCALE_TO_BINANCE.values():
            assert s > 0, "scale must be positive"
            assert s in {1.0, 1000.0, 10000.0, 100000.0}, (
                "unusual scale; review SYMBOL_SCALE_TO_BINANCE"
            )
