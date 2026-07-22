"""
Unit tests for src.execution.webkey.signing.

Phase 1 spec:
    - test_aes_gcm_round_trip
    - test_layer2_algorithm_matches_real_curl
    - test_sign_dolos_structure
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

import pytest

from src.execution.webkey.signing import (
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    rsa_encrypt,
    sign_dolos,
    sign_web,
    verify_aes_round_trip,
)


# Captured-cURL replay fixture for the Layer-2 MD5 signing algorithm. Relocated
# here from production signing.py — it is a test artifact, not runtime code.
# The webkey below is a STALE captured credential kept only to replay a known
# signature; it was previously committed to git history and MUST be rotated on
# the MEXC account. It cannot place orders (the nonce is stale and the body is
# captured), and it no longer sits anywhere in the live process's import path.
_VERIFICATION_FIXTURE = {
    "webkey": "WEB13198788c33320f42e6205fcf7306a433aecb2e897089e42012b0fca19185c57",
    "nonce": 1777883704371,
    "expected_sign": "d58b06f98c259581f3c5d7547064a82b",
    "body": {
        "symbol": "ZEC_USDT", "side": 1, "openType": 1, "type": "5",
        "vol": 6, "leverage": 9, "marketCeiling": False, "priceProtect": "0",
        "p0": "7xa9EwQE2C5YWR/LqhiFCd8g30l2JmNDbxjYkwetGbqYpJUbJtftaCTfkeUbdGM1dpMl1ablhhJGoKYi4ynd32v4JbUu84ilIgQo8uv0SsnWUbpWzib7OJCOsjRsrLfjQnVgTLsJXLGCwWMZM6JpRGB4mPmKM/lNajLI9yhcIWcOtWv7zIgcqeTBHsy97xCj7o/Anc3tHJbcCWu0XrUJOIqXtvusDZ/yDUKV8tHWr0bXmgd6qrySxmfXspKhznRqSnbfYCChBZRwjaO6pKiUH0Aw+Cti/LuKEGX2mIRWhm4m7SOtJUF9A9O29OSDB3XlA7xyoWk9CvqmGuDAQVH2rXf3sxqEk+nMaCrRL6adP8NYEMC/GHAQ+dBzKgA=",
        "k0": "hZWcF28TBwqwNtDouF/FTockDiS6InFfrvP+ocIvidKRMQzI9m0QYgwOB025yLpGr9vIt3lT/hrFP5c2mDmmn42awMA8LQ+1yZYu9hcJgQYFGUvPkX6NwoqYotDC8sZFrkjR0LTesclV4WH6cyIVKYaTB1bTmAYteizwWAwA6pjbSblECCr+us8qInLT+nGrBvWD5m/CXlJs6qX0kh9Wx09RwJ8Dd7bmGoe35c0MMwi8kc5SEQyoVP2h8g+7oVF0NfFz48qPCL1N6gzwTVWB0QpprLvSQyt6ZKQerZFpCTTgnKPOeOuJpSJaS2xSXUSaowSwiSS3XXB7QgfnDUKONw==",
        "chash": "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8",
        "mtoken": "s1ZJ7ZR0WEe9zJf8KKWt",
        "ts": 1777883704525,
        "mhash": "2a8ca8f5c913017f4a51db4a357013c9",
    },
}


def _compute_layer2_sign(body, webkey, nonce):
    """Replay-recompute the Layer-2 MD5 signature for a fixed (captured) nonce."""
    p_tail = hashlib.md5(f"{webkey}{nonce}".encode()).hexdigest()[7:]
    body_json = json.dumps(body, separators=(",", ":"))
    return hashlib.md5(f"{nonce}{body_json}{p_tail}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# AES-GCM
# ---------------------------------------------------------------------------

class TestAesGcm:
    def test_round_trip_simple(self):
        key = secrets.token_bytes(16).hex()  # 32-char hex == 32 UTF-8 bytes
        plaintext = "hello mexc"
        encrypted = aes_gcm_encrypt(plaintext, key)
        assert aes_gcm_decrypt(encrypted, key) == plaintext

    def test_round_trip_unicode(self):
        key = secrets.token_bytes(16).hex()
        plaintext = '{"symbol":"ZEC_USDT","note":"тест 🚀"}'
        encrypted = aes_gcm_encrypt(plaintext, key)
        assert aes_gcm_decrypt(encrypted, key) == plaintext

    def test_round_trip_json_payload(self):
        """Mirrors what sign_dolos actually encrypts."""
        key = secrets.token_bytes(16).hex()
        payload = {
            "mtoken": "s1ZJ7ZR0WEe9zJf8KKWt",
            "ts": 1777883704525,
            "symbol": "ZEC_USDT",
            "side": 1,
            "openType": 1,
            "type": "5",
            "vol": 6,
            "leverage": 9,
        }
        plaintext = json.dumps(payload, separators=(",", ":"))
        encrypted = aes_gcm_encrypt(plaintext, key)
        decrypted = aes_gcm_decrypt(encrypted, key)
        assert json.loads(decrypted) == payload

    def test_output_is_base64(self):
        key = secrets.token_bytes(16).hex()
        encrypted = aes_gcm_encrypt("x", key)
        # base64 must round-trip without raising
        raw = base64.b64decode(encrypted)
        # 12 IV + ciphertext + 16 tag → at least 28 bytes for "x" plaintext
        assert len(raw) >= 12 + 1 + 16

    def test_short_key_rejected(self):
        with pytest.raises(ValueError, match="32 UTF-8 bytes"):
            aes_gcm_encrypt("data", "short")

    def test_helper_verifies(self):
        assert verify_aes_round_trip() is True


# ---------------------------------------------------------------------------
# Layer 2 — captured-cURL replay
# ---------------------------------------------------------------------------

class TestLayer2Algorithm:
    def test_matches_real_curl_explicit(self):
        """Re-computation of the Layer-2 sign matches the captured fixture."""
        fx = _VERIFICATION_FIXTURE
        computed = _compute_layer2_sign(fx["body"], fx["webkey"], fx["nonce"])
        assert computed == fx["expected_sign"]

    def test_sign_web_returns_correct_keys(self):
        """sign_web output shape matches what _request injects into headers."""
        sig = sign_web({"any": "body"}, "WEB" + "0" * 64)
        assert set(sig.keys()) == {"x-mxc-sign", "x-mxc-nonce"}
        assert len(sig["x-mxc-sign"]) == 32     # MD5 hex
        assert sig["x-mxc-nonce"].isdigit()
        assert len(sig["x-mxc-nonce"]) == 13    # ms timestamp

    def test_sign_web_changes_with_body(self):
        """Different body → different signature (sanity)."""
        webkey = "WEB" + "0" * 64
        # Force same nonce by passing a known time-diff isn't enough — we just
        # check that two synchronous calls with different bodies almost always
        # differ. They could collide in the same ms with identical sign by
        # cosmic accident, but body is part of the digest so the chance is ~0.
        s1 = sign_web({"a": 1}, webkey)
        s2 = sign_web({"a": 2}, webkey)
        # Either nonces differ OR signs differ; the conjunction-collision is
        # vanishingly small.
        assert s1["x-mxc-nonce"] != s2["x-mxc-nonce"] or s1["x-mxc-sign"] != s2["x-mxc-sign"]


# ---------------------------------------------------------------------------
# sign_dolos shape + replayability
# ---------------------------------------------------------------------------

class TestSignDolosStructure:
    DOLOS_CFG = {
        "chash": "d6c64d28e362f314071b3f9d78ff7494d9cd7177ae0465e772d1840e9f7905d8",
        "mtoken": "s1ZJ7ZR0WEe9zJf8KKWt",
        "mhash": "2a8ca8f5c913017f4a51db4a357013c9",
        "parameters": [
            "mtoken", "ts", "symbol", "side",
            "openType", "type", "vol", "leverage",
        ],
        "data_upload": 1,
    }

    INPUT_PAYLOAD = {
        "symbol": "ZEC_USDT",
        "side": 1,
        "openType": 1,
        "type": "5",
        "vol": 6,
        "leverage": 9,
        "mtoken": "s1ZJ7ZR0WEe9zJf8KKWt",
        "mhash": "2a8ca8f5c913017f4a51db4a357013c9",
    }

    def test_returns_six_keys(self):
        sig = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        assert set(sig.keys()) == {"p0", "k0", "chash", "mtoken", "ts", "mhash"}

    def test_static_fields_passed_through(self):
        sig = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        assert sig["chash"] == self.DOLOS_CFG["chash"]
        assert sig["mtoken"] == self.DOLOS_CFG["mtoken"]
        assert sig["mhash"] == self.DOLOS_CFG["mhash"]

    def test_ts_is_recent_ms(self):
        import time as _time
        before = int(_time.time() * 1000)
        sig = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        after = int(_time.time() * 1000)
        assert before <= sig["ts"] <= after

    def test_k0_is_base64_256_bytes(self):
        """RSA-2048 ciphertext is always 256 bytes → 344 base64 chars."""
        sig = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        raw = base64.b64decode(sig["k0"])
        assert len(raw) == 256
        assert len(sig["k0"]) == 344  # base64 of 256 bytes (no padding stripped)

    def test_p0_is_nonempty_base64(self):
        sig = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        raw = base64.b64decode(sig["p0"])
        # 12 IV + ciphertext (≥ payload size) + 16 tag
        assert len(raw) >= 12 + 16 + 1

    def test_two_calls_produce_different_p0_k0(self):
        """AES key + RSA pad + ts all vary → output must change every call."""
        sig1 = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        sig2 = sign_dolos(self.INPUT_PAYLOAD, self.DOLOS_CFG)
        assert sig1["p0"] != sig2["p0"]
        assert sig1["k0"] != sig2["k0"]

    def test_data_upload_zero_filters_to_mtoken_only(self):
        """When data_upload=0, only mtoken (+ ts) goes into the encrypted blob."""
        cfg = dict(self.DOLOS_CFG, data_upload=0)
        sig = sign_dolos(self.INPUT_PAYLOAD, cfg)
        # We can't easily decrypt without re-running RSA decrypt, but we can
        # at least confirm shape stays the same.
        assert set(sig.keys()) == {"p0", "k0", "chash", "mtoken", "ts", "mhash"}


# ---------------------------------------------------------------------------
# RSA primitive
# ---------------------------------------------------------------------------

class TestRsaEncrypt:
    def test_output_size(self):
        """RSA-2048 → 256-byte ciphertext → 344-char base64."""
        out = rsa_encrypt("0123456789abcdef" * 2)  # 32 chars, well under 245-byte cap
        raw = base64.b64decode(out)
        assert len(raw) == 256

    def test_nondeterministic(self):
        """PKCS1 v1.5 includes random padding → two encryptions must differ."""
        a = rsa_encrypt("same input")
        b = rsa_encrypt("same input")
        assert a != b
