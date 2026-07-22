"""Tests for source-of-truth consolidation (v6, May 2026).

In v6, pair_configs is THE ONLY source for live and shadow sizing.
Three previous sources have been removed:
  - webkey_slots.live_margin_*/live_leverage_*
  - live_pair_whitelist.margin_*/leverage_*
  - pair_configs.margin_usdt/leverage (legacy single-value fields)

This test suite validates:
  1. get_slot_config reads from pair_configs
  2. get_pair_sizing returns the right data structure
  3. get_slot_config returns None when the pair_configs row is missing
     (no hardcoded fallback — pair_configs is the sole source of truth)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live_pool import LiveExecutorPool


def _mk_slot(slot_id=1, assigned_pair="PENGUUSDT"):
    """Build a mock WebkeySlot object. No legacy override fields in v6."""
    slot = MagicMock()
    slot.slot_id = slot_id
    slot.assigned_pair = assigned_pair
    return slot


def _mk_webkey_store(slot_obj, pair_sizing=None):
    """Mock WebkeyStore."""
    store = MagicMock()
    store.get = AsyncMock(return_value=slot_obj)
    store.get_pair_sizing = AsyncMock(return_value=pair_sizing)
    # Per-(slot, pair) override lookup; None = inherit the pair YAML (default).
    store.get_slot_pair_sizing = AsyncMock(return_value=None)
    return store


def _mk_pool(webkey_store):
    return LiveExecutorPool(
        client_pool=MagicMock(),
        webkey_store=webkey_store,
        alerts=None,
    )


# ──────────────────────────────────────────────────────────────────────
# pair_configs is the source
# ──────────────────────────────────────────────────────────────────────

class TestPairConfigsAsOnlySource:
    @pytest.mark.asyncio
    async def test_uses_pair_configs_values(self):
        slot = _mk_slot()
        store = _mk_webkey_store(
            slot,
            pair_sizing={
                "margin_min_usdt": 2.0,
                "margin_max_usdt": 7.0,
                "leverage_min": 50,
                "leverage_max": 100,
            },
        )
        pool = _mk_pool(store)

        cfg = await pool.get_slot_config(1)
        assert cfg["margin_min_usdt"] == 2.0
        assert cfg["margin_max_usdt"] == 7.0
        assert cfg["leverage_min"] == 50
        assert cfg["leverage_max"] == 100
        # No per-(slot,pair) override → slot_* keys are None (inherit pair YAML).
        assert cfg["slot_leverage_min"] is None
        assert cfg["slot_margin_min_usdt"] is None

    @pytest.mark.asyncio
    async def test_per_slot_pair_override_flows_into_cfg(self):
        """A slot_pair_sizing row surfaces in cfg's slot_* keys so shadow_engine
        can size that slot differently for that pair (pair values unchanged)."""
        slot = _mk_slot()
        store = _mk_webkey_store(
            slot,
            pair_sizing={
                "margin_min_usdt": 2.0, "margin_max_usdt": 7.0,
                "leverage_min": 50, "leverage_max": 100,
            },
        )
        store.get_slot_pair_sizing = AsyncMock(return_value={
            "margin_min_usdt": 25.0, "margin_max_usdt": 30.0,
            "leverage_min": 45, "leverage_max": 50,
        })
        pool = _mk_pool(store)

        cfg = await pool.get_slot_config(1)
        # Pair (admission) values untouched…
        assert cfg["leverage_min"] == 50
        # …and the per-(slot,pair) override rides along for shadow_engine.
        assert cfg["slot_margin_min_usdt"] == 25.0
        assert cfg["slot_margin_max_usdt"] == 30.0
        assert cfg["slot_leverage_min"] == 45
        assert cfg["slot_leverage_max"] == 50

    @pytest.mark.asyncio
    async def test_returns_none_when_no_slot_assignment(self):
        slot = MagicMock()
        slot.assigned_pair = None
        store = _mk_webkey_store(slot)
        pool = _mk_pool(store)

        assert await pool.get_slot_config(1) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_slot_missing(self):
        store = _mk_webkey_store(None)
        pool = _mk_pool(store)
        assert await pool.get_slot_config(99) is None


# ──────────────────────────────────────────────────────────────────────
# Missing pair_configs row (edge case) — returns None, no fallback
# ──────────────────────────────────────────────────────────────────────

class TestMissingPairConfigsRow:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_pair_configs_row(self):
        """No pair_configs row → get_slot_config returns None (the trade is
        skipped). pair_configs is the sole source of sizing truth; there is
        no hardcoded fallback."""
        slot = _mk_slot()
        store = _mk_webkey_store(slot, pair_sizing=None)
        pool = _mk_pool(store)

        assert await pool.get_slot_config(1) is None

    @pytest.mark.asyncio
    async def test_missing_row_logs_warning(self, caplog):
        """A missing pair_configs row should log a warning so admin notices."""
        import logging

        slot = _mk_slot()
        store = _mk_webkey_store(slot, pair_sizing=None)
        pool = _mk_pool(store)

        with caplog.at_level(logging.WARNING, logger="src.execution.live_pool"):
            await pool.get_slot_config(1)

        warning_msgs = [r.message for r in caplog.records
                        if r.levelname == "WARNING"]
        assert any("no pair_configs row" in m.lower()
                   for m in warning_msgs), (
            f"Expected hardcoded-fallback warning. Got: {warning_msgs}"
        )


# ──────────────────────────────────────────────────────────────────────
# get_pair_sizing direct unit tests
# ──────────────────────────────────────────────────────────────────────

class TestGetPairSizing:
    @pytest.mark.asyncio
    async def test_returns_dict_when_row_exists(self):
        from src.execution.webkey.credentials import WebkeyStore

        mock_db = MagicMock()
        mock_row = {
            "margin_min_usdt": 2.0,
            "margin_max_usdt": 7.0,
            "leverage_min": 50,
            "leverage_max": 100,
        }
        mock_db.fetchone = AsyncMock(return_value=mock_row)

        store = WebkeyStore.__new__(WebkeyStore)
        store.db = mock_db

        result = await store.get_pair_sizing("PENGUUSDT")
        # be80f9d: get_pair_sizing is now a pure existence gate (2c dropped the
        # margin/leverage columns). A non-None row → a dict with None
        # placeholders; the VALUES are unused by all callers (sizing resolves
        # from YAML). Assert presence + None placeholders, not the old values.
        assert result is not None
        assert result["margin_min_usdt"] is None
        assert result["margin_max_usdt"] is None
        assert result["leverage_min"] is None
        assert result["leverage_max"] is None

        call_args = mock_db.fetchone.call_args
        sql = call_args[0][0]
        assert "pair_configs" in sql
        assert call_args[0][1] == ("PENGUUSDT",)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        from src.execution.webkey.credentials import WebkeyStore

        mock_db = MagicMock()
        mock_db.fetchone = AsyncMock(return_value=None)

        store = WebkeyStore.__new__(WebkeyStore)
        store.db = mock_db

        assert await store.get_pair_sizing("NOPAIR") is None


# ──────────────────────────────────────────────────────────────────────
# Legacy fields are gone from dataclass
# ──────────────────────────────────────────────────────────────────────

class TestLegacyFieldsRemoved:
    def test_webkey_slot_dataclass_has_no_legacy_sizing_fields(self):
        """v6 cleanup: WebkeySlot must not expose legacy override fields."""
        from src.execution.webkey.credentials import WebkeySlot

        slot = WebkeySlot(
            slot_id=1, label=None, enabled=True,
            webkey=None, visitor_id=None,
            assigned_pair="PENGUUSDT", live_enabled=True,
        )
        assert not hasattr(slot, "live_margin_min_usdt"), (
            "live_margin_min_usdt should be removed from WebkeySlot in v6"
        )
        assert not hasattr(slot, "live_margin_max_usdt")
        assert not hasattr(slot, "live_leverage_min")
        assert not hasattr(slot, "live_leverage_max")
        # proxy support fully removed
        assert not hasattr(slot, "proxy")
