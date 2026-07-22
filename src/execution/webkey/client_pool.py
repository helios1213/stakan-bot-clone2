"""
Persistent MexcWebClient pool — eliminates TLS handshake overhead.

Background:
    Each MexcWebClient owns a curl_cffi.AsyncSession with its own connection
    pool, cookie jar, and Akamai warmup state. Creating a new client per
    request means:
      - New TLS handshake (~57ms)
      - Cookie warmup may fire if cache empty (~150-300ms)
      - No connection reuse → every REST call is full TLS round-trip

    With a pool, each slot's client is created once at startup and lives
    for the bot's lifetime. Subsequent calls reuse the same TLS connection
    via curl_cffi's HTTP/2 keep-alive. Cookie warmup happens once per
    600 seconds (DEFAULT_COOKIE_MAX_AGE_SEC) — for the rest of the time
    `client.warmup()` is a fast no-op.

Usage:
    pool = WebkeyClientPool(webkey_store)
    await pool.start()  # eager init: create client+warmup for each enabled slot

    # Reuse the same client across requests:
    client = await pool.get(slot_id=1)
    report = await client.health_check()  # fast — no new TLS handshake

    # On shutdown:
    await pool.close_all()

Threading:
    All operations are async. A per-slot lock is held during creation
    to prevent duplicate client construction on concurrent first access.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .client import MexcClientError, MexcWebClient
from .credentials import WebkeyStore

logger = logging.getLogger(__name__)


class WebkeyClientPool:
    """
    Manages persistent MexcWebClient instances per slot.

    The pool holds at most one client per slot_id. Clients are created
    lazily on first access via `get(slot_id)`, or eagerly via `start()`.
    """

    def __init__(self, store: WebkeyStore) -> None:
        self._store = store
        self._clients: dict[int, MexcWebClient] = {}
        self._creation_locks: dict[int, asyncio.Lock] = {}
        self._closed = False

    async def start(self, eager_warmup: bool = True) -> dict[int, str]:
        """
        Create clients for all enabled+ready slots and (optionally) warm them up.
        Returns a per-slot status report: {slot_id: "ok" | "error: ..."}

        Eager warmup means we pay the warmup cost once at boot, not on first
        user request. Recommended for production.
        """
        report: dict[int, str] = {}
        slots = await self._store.list_enabled_complete()

        for slot in slots:
            try:
                client = await self.get(slot.slot_id)
                if eager_warmup:
                    await client.warmup()
                report[slot.slot_id] = "ok"
            except Exception as e:
                # Don't fail startup if one slot is broken — log and continue
                logger.warning(
                    "WebkeyClientPool: slot %d initialisation failed: %s",
                    slot.slot_id, e,
                )
                report[slot.slot_id] = f"error: {e}"

        ok_count = sum(1 for v in report.values() if v == "ok")
        logger.info(
            "WebkeyClientPool started: %d/%d slots warmed up",
            ok_count, len(report),
        )
        return report

    async def get(self, slot_id: int) -> MexcWebClient:
        """
        Get or create the persistent client for `slot_id`.

        First access creates the client and ensures its session is open.
        Subsequent accesses return the cached instance (which keeps its
        TLS connection and cookie jar alive between calls).
        """
        if self._closed:
            raise MexcClientError("WebkeyClientPool is closed")

        # Fast path: already in cache
        if slot_id in self._clients:
            return self._clients[slot_id]

        # Slow path: lock per slot to prevent duplicate construction
        lock = self._creation_locks.setdefault(slot_id, asyncio.Lock())
        async with lock:
            # Double-check after acquiring lock
            if slot_id in self._clients:
                return self._clients[slot_id]

            slot = await self._store.get(slot_id)
            if slot is None:
                raise MexcClientError(f"Slot {slot_id} not found")
            if slot.webkey is None or slot.visitor_id is None:
                raise MexcClientError(
                    f"Slot {slot_id} not ready (webkey or visitor_id missing)"
                )

            client = MexcWebClient.from_slot(slot)
            # Open the underlying AsyncSession so subsequent calls reuse
            # the same connection. Without this, the first real call
            # will do TLS handshake + warmup on top of normal latency.
            await client._ensure_session()
            self._clients[slot_id] = client
            logger.debug("WebkeyClientPool: created persistent client for slot %d", slot_id)
            return client

    async def invalidate(self, slot_id: int) -> None:
        """
        Drop a slot's cached client (e.g., after webkey rotation or auth error).
        Next `get()` will reconstruct from current store state.
        """
        client = self._clients.pop(slot_id, None)
        if client is not None:
            try:
                await client.close()
            except Exception as e:
                logger.debug("invalidate: close raised %s — ignored", e)

    async def close_all(self) -> None:
        """Close all persistent clients. Called on shutdown."""
        self._closed = True
        for slot_id, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as e:
                logger.debug("close_all: slot %d close raised %s — ignored", slot_id, e)
        self._clients.clear()
        logger.info("WebkeyClientPool closed")

    def stats(self) -> dict[str, Any]:
        return {
            "active_clients": len(self._clients),
            "slot_ids": sorted(self._clients.keys()),
            "closed": self._closed,
        }
