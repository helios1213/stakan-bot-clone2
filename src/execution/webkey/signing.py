"""
MEXC Web API signing — Layer 1 (dolos) + Layer 2 (web).

Pure functions, no I/O, no async. Reverse-engineered from
remote-intelligent-ai-bot.v2.0.7.js + fp.umd.js. Verified against real cURL
captured cURL traces (matches submitOrder live response with orderId).

Layer 1 (dolos):  AES-256-GCM + RSA-PKCS1 v1.5
                  Encrypts order body into {p0, k0, chash, mtoken, ts, mhash}.

Layer 2 (web):    MD5-based signature
                  Produces x-mxc-sign + x-mxc-nonce headers covering the
                  Layer-1 body.

Used by client.MexcWebClient — typically you don't call these directly.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes


# ---------------------------------------------------------------------------
# RSA public key — pinned, never changes per session
# ---------------------------------------------------------------------------

MEXC_RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqqpMCeNv7qfsKe09xwE5
o05ZCq/qJvTok6WbqYZOXA16UQqR+sHH0XXfnWxLSEvCviP9qjZjruHWdpMmC4i/
yQJe7MJ66YoNloeNtmMgtqEIjOvSxRktmAxywul/eJolrhDnRPXYll4fA5+24t1g
6L5fgo/p66yLtZRg4fC1s3rAF1WPe6dSJQx7jQ/xhy8Z0WojmzIeaoBa0m8qswx0
DMIdzXfswH+gwMYCQGR3F/NAlxyvlWPMBlpFEuHZWkp9TXlTtbLf+YL8vYjV5HNq
IdNjVzrIvg/Bis49ktfsWuQxT/RIyCsTEuHmZyZR6NJAMPZUE5DBnVWdLShb6Kuy
qwIDAQAB
-----END PUBLIC KEY-----"""

_PUBLIC_KEY = RSA.import_key(MEXC_RSA_PUBLIC_KEY_PEM)
_RSA_CIPHER = PKCS1_v1_5.new(_PUBLIC_KEY)


# ---------------------------------------------------------------------------
# Layer 1 — Dolos signing primitives
# ---------------------------------------------------------------------------

def aes_gcm_encrypt(plaintext: str, key_utf8: str) -> str:
    """AES-256-GCM encryption, output = base64(IV || ciphertext || authTag).

    Key must be a 32-character ASCII/hex string (used as 32 UTF-8 bytes).
    Mirrors fp.umd.js Te(t, e).
    """
    key_bytes = key_utf8.encode("utf-8")
    if len(key_bytes) != 32:
        raise ValueError(f"Key must be 32 UTF-8 bytes, got {len(key_bytes)}")

    iv = get_random_bytes(12)
    cipher = AES.new(key_bytes, AES.MODE_GCM, nonce=iv)
    ciphertext, auth_tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))

    return base64.b64encode(iv + ciphertext + auth_tag).decode("ascii")


def aes_gcm_decrypt(ciphertext_b64: str, key_utf8: str) -> str:
    """Reverse of `aes_gcm_encrypt` — used in tests and offline analysis."""
    key_bytes = key_utf8.encode("utf-8")
    combined = base64.b64decode(ciphertext_b64)
    iv = combined[:12]
    auth_tag = combined[-16:]
    ciphertext = combined[12:-16]
    cipher = AES.new(key_bytes, AES.MODE_GCM, nonce=iv)
    plaintext = cipher.decrypt_and_verify(ciphertext, auth_tag)
    return plaintext.decode("utf-8")


def rsa_encrypt(text: str) -> str:
    """RSA-PKCS1 v1.5 encryption with pinned MEXC public key. Returns base64."""
    encrypted = _RSA_CIPHER.encrypt(text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def sign_dolos(input_payload: dict[str, Any], dolos_config: dict[str, Any]) -> dict[str, Any]:
    """Build dolos signature block: {p0, k0, chash, mtoken, ts, mhash}.

    Args:
        input_payload: order params merged with {mtoken, mhash}.
        dolos_config: cached server config — see WebkeyCredential.dolos_config.
            Must contain: chash, mtoken, mhash, parameters, data_upload.

    Returns:
        Six-key dict to be merged into the request body.
    """
    # 1. Random AES-256 key as 32-char hex (16 raw bytes → hex)
    aes_key_hex = secrets.token_bytes(16).hex()

    # 2. RSA-encrypt the AES key (UTF-8 bytes of the hex string itself)
    k0 = rsa_encrypt(aes_key_hex)

    # 3. Millisecond timestamp
    ts = int(time.time() * 1000)

    # 4. Filter input fields per server config
    if dolos_config.get("data_upload", 1) == 1:
        params = dolos_config["parameters"]
        filtered = {k: input_payload.get(k) for k in params if k in input_payload}
    else:
        filtered = {"mtoken": input_payload.get("mtoken")}

    # ts is always part of the encrypted payload
    filtered["ts"] = ts

    # 5. AES-GCM encrypt JSON(filtered) with the AES key
    p0 = aes_gcm_encrypt(json.dumps(filtered, separators=(",", ":")), aes_key_hex)

    return {
        "p0": p0,
        "k0": k0,
        "chash": dolos_config["chash"],
        "mtoken": dolos_config["mtoken"],
        "ts": ts,
        "mhash": dolos_config["mhash"],
    }


# ---------------------------------------------------------------------------
# Layer 2 — Web signing (x-mxc-sign / x-mxc-nonce)
# ---------------------------------------------------------------------------

def sign_web(
    body_dict: dict[str, Any],
    webkey: str,
    server_time_diff_ms: int = 0,
) -> dict[str, str]:
    """Compute x-mxc-sign / x-mxc-nonce for a fully-built request body.

    Algorithm (from remote-intelligent-ai-bot.v2.0.7.js):
        nonce = now_ms + server_time_diff
        P     = md5(webkey + nonce)[7:]   # last 25 hex chars of MD5
        sign  = md5(nonce + json_body + P)

    The body must be the EXACT same JSON the request will send (separators
    `(,:)`, no spaces). Any reordering breaks the server-side check.
    """
    nonce = int(time.time() * 1000) + server_time_diff_ms

    # 25-hex-char tail of md5(webkey || nonce)
    p_tail = hashlib.md5(f"{webkey}{nonce}".encode()).hexdigest()[7:]

    body_json = json.dumps(body_dict, separators=(",", ":"))
    sign = hashlib.md5(f"{nonce}{body_json}{p_tail}".encode()).hexdigest()

    return {
        "x-mxc-sign": sign,
        "x-mxc-nonce": str(nonce),
    }


# ---------------------------------------------------------------------------
# Sanity check — call from main() at startup
# ---------------------------------------------------------------------------

# NOTE: the captured-cURL Layer-2 replay fixture + verifier now live in
# tests/test_webkey_signing.py — they are test artifacts, not production code
# (and the captured fixture carried a real webkey, which must never sit in the
# import path of the live trading process).


def verify_aes_round_trip() -> bool:
    """Self-test of AES-256-GCM encrypt/decrypt symmetry."""
    plaintext = '{"test":"value","n":123}'
    key = secrets.token_bytes(16).hex()
    encrypted = aes_gcm_encrypt(plaintext, key)
    decrypted = aes_gcm_decrypt(encrypted, key)
    return decrypted == plaintext


if __name__ == "__main__":
    print("AES-GCM round-trip:", "OK" if verify_aes_round_trip() else "FAIL")
