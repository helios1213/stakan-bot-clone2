# -*- coding: utf-8 -*-
"""Стокові перпи змаплені правильно — з тіком і contractSize, не з дефолтом.

Пастка, яку цей тест стереже: рідний символ MEXC для двох із трьох має суфікс
STOCK (SKHYNIXSTOCK_USDT, SPCXSTOCK_USDT). Дефолтний to_mexc зробив би
SKHYNIX_USDT, книга б не знайшлась, а get_tick_size/CONTRACT_SIZES впали б на
хибний дефолт (тік у 1000× завеликий, contractSize=1.0) — і shadow-числа були б
сміттям. Усе це — тихі помилки, тому їх ловить тест, а не очі.
"""
import pytest

from src.exchanges.mexc_rest import to_binance, to_mexc
from src.execution.live_executor import (CONTRACT_SIZES, PRICE_SCALES,
                                         TICK_SIZES, get_tick_size)

# Binance-символ → (MEXC-символ, тік, contractSize, priceScale)
STOCKS = {
    "SKHYNIXUSDT": ("SKHYNIXSTOCK_USDT", 1e-2, 0.001, 2),
    "SPCXUSDT": ("SPCXSTOCK_USDT", 1e-2, 0.01, 2),
    "SOXLUSDT": ("SOXL_USDT", 1e-2, 0.01, 2),
}


@pytest.mark.parametrize("binance,expected", STOCKS.items())
def test_binance_symbol_maps_to_the_real_mexc_symbol(binance, expected):
    mexc = expected[0]
    assert to_mexc(binance) == mexc, (
        f"{binance} має мапитись у {mexc}, а не у дефолтний "
        f"{binance.replace('USDT', '_USDT')}")


@pytest.mark.parametrize("binance,expected", STOCKS.items())
def test_the_mapping_round_trips(binance, expected):
    assert to_binance(expected[0]) == binance


@pytest.mark.parametrize("binance,expected", STOCKS.items())
def test_tick_size_is_real_not_the_1e5_fallback(binance, expected):
    mexc, tick = expected[0], expected[1]
    assert TICK_SIZES.get(mexc) == tick
    assert get_tick_size(mexc) == tick
    assert get_tick_size(mexc) != 1e-5, "впав на дефолт → тік у 1000× хибний"


@pytest.mark.parametrize("binance,expected", STOCKS.items())
def test_contract_size_is_known_not_the_1_0_fallback(binance, expected):
    mexc, cs = expected[0], expected[2]
    assert CONTRACT_SIZES.get(mexc) == cs, (
        "невідомий contractSize → shadow-нотіонал і PnL спотворені, "
        "а live-ордер відмовляється відкриватись")


@pytest.mark.parametrize("binance,expected", STOCKS.items())
def test_price_scale_is_set_for_live_rounding(binance, expected):
    assert PRICE_SCALES.get(expected[0]) == expected[3]


def test_soxl_needs_no_alias_but_still_resolves():
    """SOXL мапиться правильно дефолтним правилом — перевіряємо, що ми не
    зламали цей випадок, додаючи аліаси для інших двох."""
    from src.exchanges.mexc_rest import BINANCE_TO_MEXC_ALIASES
    assert "SOXLUSDT" not in BINANCE_TO_MEXC_ALIASES
    assert to_mexc("SOXLUSDT") == "SOXL_USDT"


def test_the_tuning_gates_translate_to_a_sane_bps_gap():
    """Ворота в тіках мають відповідати ~1.5-2.5 bps гепу — інакше на дрібному
    тіку SKHYNIX поріг сповз би у безглуздя. Це sanity, не точний тюнінг."""
    cases = [("SKHYNIXSTOCK_USDT", 1017.0, 20), ("SPCXSTOCK_USDT", 135.0, 2.75),
             ("SOXL_USDT", 134.0, 2.75)]
    for mexc, price, min_gap_ticks in cases:
        tick_bps = 1e4 * TICK_SIZES[mexc] / price
        gap_bps = min_gap_ticks * tick_bps
        assert 1.0 <= gap_bps <= 3.0, f"{mexc}: геп {gap_bps:.2f}bps поза [1,3]"
