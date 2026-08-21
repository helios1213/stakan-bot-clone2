"""Pushover alerts must reach EVERY configured recipient.

Found 2026-08-21 while investigating why a burst alert never arrived: the
detector had not fired (frequency gate too tight) AND `PUSHOVER_USER` held two
valid 30-char keys joined by a comma. Pushover's `user` parameter takes exactly
one key — the joined value is rejected with HTTP 400 "user key is invalid",
verified against /1/users/validate.json. So even once the gate was loosened, the
push would still have gone nowhere. Two independent faults, one symptom.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from src.strategy.shadow_engine import ShadowEngine

KEY_A = "u" * 30
KEY_B = "z" * 30


def _engine():
    return object.__new__(ShadowEngine)


def _run(eng, monkeypatch, user_value, poster):
    monkeypatch.setenv("PUSHOVER_TOKEN", "t" * 30)
    monkeypatch.setenv("PUSHOVER_USER", user_value)
    sent: list[str] = []

    class _Loop:
        @staticmethod
        def run_in_executor(_pool, fn, *args):
            fut: asyncio.Future = asyncio.Future()
            try:
                fut.set_result(poster(sent, *args))
            except Exception as e:               # noqa: BLE001 - mirrors real path
                fut.set_exception(e)
            return fut

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())
    asyncio.run(eng._send_pushover("title", "message"))
    return sent


def _ok(sent, user):
    sent.append(user)
    return 200


def test_every_recipient_gets_its_own_request(monkeypatch):
    sent = _run(_engine(), monkeypatch, f"{KEY_A},{KEY_B}", _ok)
    assert sent == [KEY_A, KEY_B], "a comma-joined value must be split, not sent whole"


def test_single_recipient_still_works(monkeypatch):
    assert _run(_engine(), monkeypatch, KEY_A, _ok) == [KEY_A]


def test_whitespace_around_keys_is_tolerated(monkeypatch):
    sent = _run(_engine(), monkeypatch, f"  {KEY_A} , {KEY_B}  ", _ok)
    assert sent == [KEY_A, KEY_B]


def test_empty_segments_are_dropped(monkeypatch):
    sent = _run(_engine(), monkeypatch, f"{KEY_A},,{KEY_B},", _ok)
    assert sent == [KEY_A, KEY_B]


def test_one_bad_key_does_not_silence_the_others(monkeypatch):
    """The whole point: a single rejected recipient must not eat the alert."""
    def _first_fails(sent, user):
        sent.append(user)
        if user == KEY_A:
            raise RuntimeError("HTTP 400 user key is invalid")
        return 200

    sent = _run(_engine(), monkeypatch, f"{KEY_A},{KEY_B}", _first_fails)
    assert sent == [KEY_A, KEY_B], "delivery must continue past a failing recipient"


def test_unset_credentials_are_a_silent_noop(monkeypatch):
    monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER", raising=False)
    asyncio.run(_engine()._send_pushover("t", "m"))   # must not raise


def test_never_sends_the_joined_value(monkeypatch):
    """Regression guard for the exact bug: 'key1,key2' as one `user`."""
    sent = _run(_engine(), monkeypatch, f"{KEY_A},{KEY_B}", _ok)
    assert not any("," in u for u in sent)


# ---- the gate that hid it, pinned so it cannot silently drift back -------

def test_burst_rate_gate_is_configured_below_the_untriggerable_default():
    """3.0 fired ZERO times in the 3 days to 2026-08-21 and missed a
    +$147.78 SOXL run that peaked at x2.70 of the pair's own median rate."""
    compose = Path("docker-compose.yml").read_text()
    m = re.search(r"BURST_RATE_MULT=([\d.]+)", compose)
    assert m, "BURST_RATE_MULT must stay pinned in compose, not left to the default"
    assert float(m.group(1)) <= 2.75
