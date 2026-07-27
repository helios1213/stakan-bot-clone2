"""Tests for the v5 webkey-only multi-slot credentials store."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from src.execution.webkey.credentials import (
    BOOTSTRAP_CHASH,
    MAX_SLOTS,
    InvalidSlotError,
    WebkeyDecryptError,
    WebkeyError,
    WebkeyStore,
    generate_visitor_id,
    validate_webkey,
)
from src.storage.db import Database, init_db


SAMPLE_WEBKEY = "WEB" + "a" * 64
SAMPLE_WEBKEY_2 = "WEB" + "b" * 64


@pytest_asyncio.fixture
async def store():
    """Fresh store on a temp DB, with all slots seeded."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    await init_db(db_path)
    db = Database(db_path)
    await db.connect()
    master = Fernet.generate_key().decode()
    s = WebkeyStore(db, master)
    await s.ensure_slots_seeded()
    yield s
    await db.close()
    Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_webkey(self):
        validate_webkey(SAMPLE_WEBKEY)

    def test_invalid_webkey_no_prefix(self):
        with pytest.raises(WebkeyError):
            validate_webkey("a" * 67)

    def test_invalid_webkey_too_short(self):
        with pytest.raises(WebkeyError):
            validate_webkey("WEB" + "a" * 60)

    def test_invalid_webkey_non_hex(self):
        with pytest.raises(WebkeyError):
            validate_webkey("WEB" + "z" * 64)

    # proxy validation/masking tests removed — proxy support was deleted
    # (validate_proxy_url / mask_proxy no longer exist).


class TestVisitorGenerator:
    def test_length(self):
        for _ in range(20):
            v = generate_visitor_id()
            assert len(v) == 20

    def test_alphanumeric(self):
        for _ in range(20):
            v = generate_visitor_id()
            assert all(c.isalnum() for c in v)

    def test_uniqueness(self):
        # Cryptographically random — 20 chars from 62 alphabet, collision
        # probability for 100 samples is astronomically low.
        ids = {generate_visitor_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# Store: seeding & basic operations
# ---------------------------------------------------------------------------

class TestSeeding:
    @pytest.mark.asyncio
    async def test_all_slots_present(self, store):
        slots = await store.list_all()
        assert len(slots) == MAX_SLOTS
        assert [s.slot_id for s in slots] == list(range(1, MAX_SLOTS + 1))

    @pytest.mark.asyncio
    async def test_all_slots_empty_initially(self, store):
        slots = await store.list_all()
        assert all(s.is_empty for s in slots)

    @pytest.mark.asyncio
    async def test_first_empty_slot(self, store):
        assert await store.first_empty_slot() == 1

    @pytest.mark.asyncio
    async def test_seed_idempotent(self, store):
        # Re-seed doesn't duplicate
        await store.ensure_slots_seeded()
        await store.ensure_slots_seeded()
        slots = await store.list_all()
        assert len(slots) == MAX_SLOTS


class TestSetWebkey:
    @pytest.mark.asyncio
    async def test_set_webkey_basic(self, store):
        visitor = await store.set_webkey(1, SAMPLE_WEBKEY)
        assert len(visitor) == 20
        slot = await store.get(1)
        assert slot.webkey == SAMPLE_WEBKEY
        assert slot.visitor_id == visitor

    @pytest.mark.asyncio
    async def test_set_webkey_preserves_visitor(self, store):
        v1 = await store.set_webkey(1, SAMPLE_WEBKEY)
        v2 = await store.set_webkey(1, SAMPLE_WEBKEY_2)
        # Visitor preserved across webkey rotation
        assert v1 == v2
        slot = await store.get(1)
        assert slot.webkey == SAMPLE_WEBKEY_2
        assert slot.visitor_id == v1

    @pytest.mark.asyncio
    async def test_set_webkey_invalid_slot(self, store):
        with pytest.raises(InvalidSlotError):
            await store.set_webkey(0, SAMPLE_WEBKEY)
        with pytest.raises(InvalidSlotError):
            await store.set_webkey(MAX_SLOTS + 1, SAMPLE_WEBKEY)

    @pytest.mark.asyncio
    async def test_set_webkey_invalid_format(self, store):
        with pytest.raises(WebkeyError):
            await store.set_webkey(1, "garbage")

    @pytest.mark.asyncio
    async def test_first_empty_after_set(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        assert await store.first_empty_slot() == 2


# TestProxy removed — set_proxy/clear_proxy and the WebkeySlot.proxy field were
# deleted (proxy support fully removed; the bot connects direct).


class TestEnable:
    @pytest.mark.asyncio
    async def test_enable_after_setup(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.set_enabled(1, True)
        slot = await store.get(1)
        assert slot.enabled is True

    @pytest.mark.asyncio
    async def test_enable_empty_fails(self, store):
        with pytest.raises(WebkeyError, match="no webkey"):
            await store.set_enabled(1, True)

    @pytest.mark.asyncio
    async def test_disable_works(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.set_enabled(1, True)
        await store.set_enabled(1, False)
        slot = await store.get(1)
        assert slot.enabled is False


class TestLabel:
    @pytest.mark.asyncio
    async def test_set_label(self, store):
        await store.set_label(1, "Main account")
        slot = await store.get(1)
        assert slot.label == "Main account"

    @pytest.mark.asyncio
    async def test_clear_label(self, store):
        await store.set_label(1, "name")
        await store.set_label(1, None)
        slot = await store.get(1)
        assert slot.label is None

    @pytest.mark.asyncio
    async def test_label_truncated_to_50(self, store):
        await store.set_label(1, "a" * 200)
        slot = await store.get(1)
        assert len(slot.label) == 50


class TestSlotIsolation:
    @pytest.mark.asyncio
    async def test_slots_independent(self, store):
        v1 = await store.set_webkey(1, SAMPLE_WEBKEY)
        v2 = await store.set_webkey(2, SAMPLE_WEBKEY_2)
        assert v1 != v2

        s1 = await store.get(1)
        s2 = await store.get(2)
        assert s1.webkey == SAMPLE_WEBKEY
        assert s2.webkey == SAMPLE_WEBKEY_2
        assert s1.visitor_id != s2.visitor_id



class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_clears_all_fields(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.set_enabled(1, True)
        await store.set_label(1, "test")

        assert await store.delete(1) is True
        slot = await store.get(1)
        assert slot.is_empty
        assert slot.webkey is None
        assert slot.visitor_id is None
        assert slot.enabled is False
        assert slot.label is None

    @pytest.mark.asyncio
    async def test_delete_empty_returns_false(self, store):
        assert await store.delete(1) is False

    @pytest.mark.asyncio
    async def test_delete_doesnt_drop_row(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.delete(1)
        slots = await store.list_all()
        assert len(slots) == MAX_SLOTS  # row still present, just empty


class TestEncryption:
    @pytest.mark.asyncio
    async def test_wrong_master_key_fails(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            await init_db(db_path)
            db = Database(db_path)
            await db.connect()

            master_a = Fernet.generate_key().decode()
            store_a = WebkeyStore(db, master_a)
            await store_a.ensure_slots_seeded()
            await store_a.set_webkey(1, SAMPLE_WEBKEY)

            await db.close()

            db2 = Database(db_path)
            await db2.connect()
            master_b = Fernet.generate_key().decode()
            store_b = WebkeyStore(db2, master_b)

            with pytest.raises(WebkeyDecryptError):
                await store_b.get(1)
            await db2.close()
        finally:
            Path(db_path).unlink(missing_ok=True)


class TestHealth:
    @pytest.mark.asyncio
    async def test_update_health_ok(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.update_health(1, latency_ms=250, balance_usdt="42.5")
        slot = await store.get(1)
        assert slot.last_latency_ms == 250
        assert slot.last_balance_usdt == "42.5"
        assert slot.last_error is None

    @pytest.mark.asyncio
    async def test_update_health_error(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.update_health(1, None, None, error="webkey expired")
        slot = await store.get(1)
        assert slot.last_error == "webkey expired"


class TestIsComplete:
    @pytest.mark.asyncio
    async def test_complete_after_webkey_only(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        slot = await store.get(1)
        # Complete = has webkey (visitor auto-set; proxy NOT required)
        assert slot.is_complete is True

    @pytest.mark.asyncio
    async def test_empty_not_complete(self, store):
        slot = await store.get(1)
        assert slot.is_complete is False
        assert slot.is_empty is True


class TestListEnabledComplete:
    @pytest.mark.asyncio
    async def test_only_enabled_and_complete(self, store):
        # Direct mode: list_enabled_complete = enabled AND has webkey.
        # Proxy is NOT required (legacy proxy-gating was removed).
        # Slot 1: webkey + enabled → included (no proxy needed)
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.set_enabled(1, True)

        # Slot 2: webkey but NOT enabled → excluded
        await store.set_webkey(2, SAMPLE_WEBKEY_2)

        result = await store.list_enabled_complete()
        assert len(result) == 1
        assert result[0].slot_id == 1


class TestBootstrapChashConstant:
    def test_chash_is_64_hex(self):
        assert len(BOOTSTRAP_CHASH) == 64
        assert all(c in "0123456789abcdef" for c in BOOTSTRAP_CHASH)


# ---------------------------------------------------------------------------
# Auto-shadow on unassign / delete — must respect the OTHER slot
# ---------------------------------------------------------------------------

class TestAutoShadowRespectsOtherSlots:
    """Two slots may trade the same pair, so losing one must not strand it.

    Regression 2026-07-27 18:57:44: slot 2 was moved from 1000PEPEUSDT to
    TAOUSDT while slot 1 was actively trading PEPE. The move demoted the whole
    pair to shadow and slot 1 went silent for 5.4 minutes while the detector
    kept emitting PEPE signals.

    The sibling only counts if it can actually execute. An assigned-but-idle
    slot (no webkey, or live trading switched off) would leave the pair LIVE
    with nothing to run it — the same silent outage, inverted.
    """

    @staticmethod
    def _spy(store):
        seen = []

        async def _fake(pair, reason):
            seen.append((pair, reason))
            return True

        store.demote_pair_to_shadow = _fake
        return seen

    @staticmethod
    async def _make_executing(store, slot_id, pair, webkey):
        """A slot that could really trade `pair`: key, live on, assigned."""
        await store.set_webkey(slot_id, webkey)
        await store.set_live_enabled(slot_id, True)
        await store.assign_pair(slot_id, pair)

    @pytest.mark.asyncio
    async def test_reassign_keeps_pair_live_when_another_slot_can_execute(self, store):
        await self._make_executing(store, 1, "1000PEPEUSDT", SAMPLE_WEBKEY)
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.assign_pair(2, "TAOUSDT")

        assert seen == [], "pair demoted while slot 1 still trades it"

    @pytest.mark.asyncio
    async def test_reassign_demotes_pair_when_it_was_the_last_slot(self, store):
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.assign_pair(2, "TAOUSDT")

        assert [p for p, _ in seen] == ["1000PEPEUSDT"]

    @pytest.mark.asyncio
    async def test_sibling_without_a_webkey_does_not_hold_the_pair_live(self, store):
        """Assigned but unusable — demote, or the pair sits live with no executor."""
        await store.set_live_enabled(1, True)
        await store.assign_pair(1, "1000PEPEUSDT")      # slot 1: no webkey
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.assign_pair(2, "TAOUSDT")

        assert [p for p, _ in seen] == ["1000PEPEUSDT"]

    @pytest.mark.asyncio
    async def test_sibling_with_live_disabled_does_not_hold_the_pair_live(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.assign_pair(1, "1000PEPEUSDT")      # slot 1: live_enabled=0
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.assign_pair(2, "TAOUSDT")

        assert [p for p, _ in seen] == ["1000PEPEUSDT"]

    @pytest.mark.asyncio
    async def test_delete_keeps_pair_live_when_another_slot_can_execute(self, store):
        await self._make_executing(store, 1, "1000PEPEUSDT", SAMPLE_WEBKEY)
        await store.set_webkey(2, SAMPLE_WEBKEY_2)
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.delete(2)

        assert seen == [], "pair demoted while slot 1 still trades it"

    @pytest.mark.asyncio
    async def test_delete_demotes_pair_when_it_was_the_last_slot(self, store):
        await store.set_webkey(2, SAMPLE_WEBKEY_2)
        await store.assign_pair(2, "1000PEPEUSDT")
        seen = self._spy(store)

        await store.delete(2)

        assert [p for p, _ in seen] == ["1000PEPEUSDT"]

    @pytest.mark.asyncio
    async def test_helper_ignores_the_slot_being_changed(self, store):
        await self._make_executing(store, 2, "1000PEPEUSDT", SAMPLE_WEBKEY_2)
        assert await store._pair_has_another_slot("1000PEPEUSDT", 2) is False
        await self._make_executing(store, 1, "1000PEPEUSDT", SAMPLE_WEBKEY)
        assert await store._pair_has_another_slot("1000PEPEUSDT", 2) is True


class TestDuplicateWebkeyIsFlagged:
    """The same account in two slots doubles exposure now that both slots act."""

    @pytest.mark.asyncio
    async def test_reused_key_is_reported(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        assert await store._warn_if_key_reused(2, SAMPLE_WEBKEY) == [1]

    @pytest.mark.asyncio
    async def test_distinct_key_is_not_reported(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        assert await store._warn_if_key_reused(2, SAMPLE_WEBKEY_2) == []

    @pytest.mark.asyncio
    async def test_the_slot_itself_is_never_its_own_duplicate(self, store):
        await store.set_webkey(1, SAMPLE_WEBKEY)
        assert await store._warn_if_key_reused(1, SAMPLE_WEBKEY) == []

    @pytest.mark.asyncio
    async def test_setting_a_reused_key_still_succeeds(self, store):
        """Warn, never block — the operator may be mid-swap."""
        await store.set_webkey(1, SAMPLE_WEBKEY)
        await store.set_webkey(2, SAMPLE_WEBKEY)
        assert await store._warn_if_key_reused(2, SAMPLE_WEBKEY) == [1]
