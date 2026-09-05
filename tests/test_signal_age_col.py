# -*- coding: utf-8 -*-
"""binance_signal_age_ms проведено від OB до signal_features без розсинхрону арності.

Розбіжність _COLS / плейсхолдерів / tuple _finalize = ТИХА корупція (значення
поїдуть не в ті колонки). Тест ловить це до бою.
"""
import re
from src.strategy import signal_recorder as SR
from src.exchanges.orderbook import OrderBook


def test_insert_arity_matches_across_cols_placeholders_and_create():
    cols = [c.strip() for c in SR._COLS.replace("\n", "").split(",") if c.strip()]
    n_ph = SR._INSERT_SQL.count("?")
    create_cols = re.findall(r"([a-z_][a-z_0-9]*)\s+(?:INTEGER|REAL|TEXT)", SR._CREATE_SQL)
    create_cols = [c for c in create_cols if c != "id"]
    assert len(cols) == n_ph, f"_COLS={len(cols)} != плейсхолдерів={n_ph}"
    assert len(cols) == len(create_cols), f"_COLS={len(cols)} != CREATE={len(create_cols)}"
    assert "binance_signal_age_ms" in cols
    assert cols == create_cols, "порядок _COLS не збігається з CREATE"


def test_finalize_tuple_length_matches_placeholders():
    # _finalize будує tuple рівно з N значень — має дорівнювати кількості '?'.
    import inspect
    src = inspect.getsource(SR.SignalRecorder._finalize)
    assert "binance_signal_age_ms" in src
    # tuple починається з self._writebuf.append(( ... ))
    body = src.split("self._writebuf.append((", 1)[1].split("))", 1)[0]
    # рахуємо коми верхнього рівня + 1; груба, але ловить забутий елемент
    depth = 0; commas = 0
    for ch in body:
        if ch in "([{": depth += 1
        elif ch in ")]}": depth -= 1
        elif ch == "," and depth == 0: commas += 1
    n_vals = commas  # трейлінг-кома дає рівно N
    assert n_vals == SR._INSERT_SQL.count("?"), f"tuple={n_vals} != ?={SR._INSERT_SQL.count('?')}"


def test_orderbook_has_top_lead_ts_default_zero():
    ob = OrderBook(symbol="BTCUSDT", exchange="binance")
    assert getattr(ob, "top_lead_ts_ms", None) == 0
