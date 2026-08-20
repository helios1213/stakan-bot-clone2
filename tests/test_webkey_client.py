"""Tests for v5 webkey-only MexcWebClient (focus on auto-derived dolos)."""
from __future__ import annotations

import hashlib

import pytest

from src.execution.webkey.client import (
    MexcClientError,
    MexcWebClient,
    _DolosRuntime,
    _TROCHILUS_UID_PLACEHOLDER,
)
from src.execution.webkey.credentials import (
    BOOTSTRAP_CHASH,
    WebkeySlot,
)


SAMPLE_WEBKEY = "WEB" + "a" * 64
SAMPLE_VISITOR = "abcDEF1234567890wxYZ"


class TestDolosRuntime:
    def test_from_visitor_computes_mhash(self):
        d = _DolosRuntime.from_visitor(SAMPLE_VISITOR)
        expected_mhash = hashlib.md5(SAMPLE_VISITOR.encode()).hexdigest()
        assert d.mhash == expected_mhash
        assert len(d.mhash) == 32

    def test_uses_bootstrap_chash(self):
        d = _DolosRuntime.from_visitor(SAMPLE_VISITOR)
        assert d.chash == BOOTSTRAP_CHASH

    def test_visitor_preserved(self):
        d = _DolosRuntime.from_visitor(SAMPLE_VISITOR)
        assert d.visitor_id == SAMPLE_VISITOR

    def test_signing_dict_format(self):
        d = _DolosRuntime.from_visitor(SAMPLE_VISITOR)
        sd = d.as_signing_dict
        assert sd["chash"] == BOOTSTRAP_CHASH
        assert sd["mtoken"] == SAMPLE_VISITOR
        assert sd["mhash"] == d.mhash
        assert sd["data_upload"] == 1
        assert isinstance(sd["parameters"], list)


class TestFromSlot:
    def test_from_complete_slot(self):
        slot = WebkeySlot(
            slot_id=1, label=None, enabled=False,
            webkey=SAMPLE_WEBKEY,
            visitor_id=SAMPLE_VISITOR,
        )
        client = MexcWebClient.from_slot(slot)
        assert client.webkey == SAMPLE_WEBKEY
        assert client.dolos.visitor_id == SAMPLE_VISITOR
        assert client.dolos.chash == BOOTSTRAP_CHASH
        assert client.slot_id == 1
        # proxy support fully removed
        assert not hasattr(client, "proxy")

    # NOTE on the two tests below: they resolve the class through the module at
    # CALL time instead of using the names imported at the top of this file.
    #
    # `test_order_host.py::test_env_override_reverts_host` calls
    # `importlib.reload()` on this very module to check the MEXC_API_HOST
    # override. It cleans up its env and reloads back — but a reload cannot
    # restore class IDENTITY: afterwards `sys.modules[...].MexcClientError` is a
    # NEW object, while the top-level import here still points at the original.
    # `pytest.raises` compares by identity, so it stopped recognising a
    # perfectly correct exception, and only in a full-suite run (alphabetically
    # test_order_host goes first). Reading the attribute off the module makes
    # these immune to that.

    def test_from_slot_no_webkey_fails(self):
        from src.execution.webkey import client as client_mod

        slot = WebkeySlot(
            slot_id=1, label=None, enabled=False,
            webkey=None,
            visitor_id=None,
        )
        with pytest.raises(client_mod.MexcClientError, match="no webkey"):
            client_mod.MexcWebClient.from_slot(slot)

    def test_from_slot_no_visitor_fails(self):
        from src.execution.webkey import client as client_mod

        slot = WebkeySlot(
            slot_id=1, label=None, enabled=False,
            webkey=SAMPLE_WEBKEY,
            visitor_id=None,
        )
        with pytest.raises(client_mod.MexcClientError, match="no visitor"):
            client_mod.MexcWebClient.from_slot(slot)


class TestHeaders:
    def test_trochilus_uid_is_placeholder(self):
        client = MexcWebClient(SAMPLE_WEBKEY, SAMPLE_VISITOR)
        h = client._common_headers()
        assert h["trochilus-uid"] == _TROCHILUS_UID_PLACEHOLDER
        assert _TROCHILUS_UID_PLACEHOLDER == "0"

    def test_authorization_is_webkey(self):
        client = MexcWebClient(SAMPLE_WEBKEY, SAMPLE_VISITOR)
        h = client._common_headers()
        assert h["authorization"] == SAMPLE_WEBKEY

    def test_mtoken_is_visitor(self):
        client = MexcWebClient(SAMPLE_WEBKEY, SAMPLE_VISITOR)
        h = client._common_headers()
        assert h["mtoken"] == SAMPLE_VISITOR

    def test_layer2_sign_merged(self):
        client = MexcWebClient(SAMPLE_WEBKEY, SAMPLE_VISITOR)
        h = client._common_headers({"x-mxc-sign": "abc", "x-mxc-nonce": "123"})
        assert h["x-mxc-sign"] == "abc"
        assert h["x-mxc-nonce"] == "123"
