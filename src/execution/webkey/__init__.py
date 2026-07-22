"""
Webkey-based MEXC execution — webkey-only multi-slot edition (v5).
"""
from .client import (
    MexcAuthError,
    MexcClientError,
    MexcServerError,
    MexcWebClient,
)
from .client_pool import WebkeyClientPool
from .credentials import (
    BOOTSTRAP_CHASH,
    MAX_SLOTS,
    InvalidSlotError,
    WebkeyDecryptError,
    WebkeyError,
    WebkeyNotFound,
    WebkeySlot,
    WebkeyStore,
    generate_visitor_id,
    validate_webkey,
)
from .signing import (
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    rsa_encrypt,
    sign_dolos,
    sign_web,
)

__all__ = [
    "BOOTSTRAP_CHASH",
    "MAX_SLOTS",
    "InvalidSlotError",
    "MexcAuthError",
    "MexcClientError",
    "MexcServerError",
    "MexcWebClient",
    "WebkeyClientPool",
    "WebkeyDecryptError",
    "WebkeyError",
    "WebkeyNotFound",
    "WebkeySlot",
    "WebkeyStore",
    "aes_gcm_decrypt",
    "aes_gcm_encrypt",
    "generate_visitor_id",
    "rsa_encrypt",
    "sign_dolos",
    "sign_web",
    "validate_webkey",
]
