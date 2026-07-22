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

    def test_from_slot_no_webkey_fails(self):
        slot = WebkeySlot(
            slot_id=1, label=None, enabled=False,
            webkey=None,
            visitor_id=None,
        )
        with pytest.raises(MexcClientError, match="no webkey"):
            MexcWebClient.from_slot(slot)

    def test_from_slot_no_visitor_fails(self):
        slot = WebkeySlot(
            slot_id=1, label=None, enabled=False,
            webkey=SAMPLE_WEBKEY,
            visitor_id=None,
        )
        with pytest.raises(MexcClientError, match="no visitor"):
            MexcWebClient.from_slot(slot)


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
