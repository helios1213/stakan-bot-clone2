# -*- coding: utf-8 -*-
"""KILL ALL має зупиняти САМЕ той бот, який вибрали.

Боти торгують різні пари на різних акаунтах. Один спільний «стоп» означав, що
оператор або кладе зайве, або думає, що поклав усе, а поклав половину — і те,
і те коштує грошей у момент, коли він і так гасить пожежу.
"""
import pytest

data = pytest.importorskip("src.webpanel.data")


@pytest.fixture
def spy(monkeypatch):
    """Підмінити обидві дії (локальний UPDATE і SSH), лишивши сам вибір."""
    calls = {"local": 0, "remote": []}

    def _local():
        calls["local"] += 1
        return 3

    def _rpc(server, payload, timeout=8):
        calls["remote"].append((server, payload.get("op")))
        return {"ok": True, "demoted": 1}

    monkeypatch.setattr(data, "set_all_pairs_shadow", _local)
    monkeypatch.setattr(data, "_remote_rpc", _rpc)
    return calls


def test_clone_only_leaves_the_primary_trading(spy):
    """Головне, заради чого це робилось."""
    out = data.set_all_pairs_shadow_all(["clone1"])
    assert spy["local"] == 0, "праймер зупинили, хоча просили лише клон"
    assert spy["remote"] == [("clone1", "kill_all")]
    assert set(out) == {"clone1"}


def test_primary_only_does_not_touch_the_clone(spy):
    out = data.set_all_pairs_shadow_all(["primary"])
    assert spy["local"] == 1
    assert spy["remote"] == [], "полізли на клон, хоча просили лише праймер"
    assert out["primary"] == {"ok": True, "demoted": 3}


def test_no_argument_still_means_every_bot(spy):
    """Стара поведінка не має змінитись мовчки для інших викликів."""
    out = data.set_all_pairs_shadow_all()
    assert spy["local"] == 1
    assert spy["remote"] == [("clone1", "kill_all")]
    assert set(out) == {"primary", "clone1"}


def test_an_unknown_name_is_dropped_not_defaulted(spy):
    """Одрук не має розвʼязатись у «якийсь інший бот»."""
    out = data.set_all_pairs_shadow_all(["clone7"])
    assert spy["local"] == 0 and spy["remote"] == []
    assert out == {}


def test_an_empty_list_stops_nothing(spy):
    assert data.set_all_pairs_shadow_all([]) == {}
    assert spy["local"] == 0 and spy["remote"] == []


def test_a_dead_clone_never_masks_a_requested_primary_stop(monkeypatch):
    """Праймер комітиться ДО SSH — недоступний клон не має його затримати."""
    monkeypatch.setattr(data, "set_all_pairs_shadow", lambda: 2)

    def _boom(server, payload, timeout=8):
        raise OSError("ssh down")

    monkeypatch.setattr(data, "_remote_rpc", _boom)
    out = data.set_all_pairs_shadow_all(["primary", "clone1"])
    assert out["primary"] == {"ok": True, "demoted": 2}
    assert out["clone1"]["ok"] is False


# ── шар API: вибір мусить бути перевірений до того, як щось зупиниться ──

def test_api_refuses_an_empty_selection(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    app_mod = pytest.importorskip("src.webpanel.app")
    monkeypatch.setattr(app_mod.data, "set_all_pairs_shadow_all",
                        lambda *a, **k: pytest.fail("зупинка при порожньому виборі"))
    with pytest.raises(fastapi.HTTPException) as e:
        app_mod.api_kill_all({"servers": []})
    assert e.value.status_code == 400


def test_api_refuses_an_unknown_server(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    app_mod = pytest.importorskip("src.webpanel.app")
    monkeypatch.setattr(app_mod.data, "set_all_pairs_shadow_all",
                        lambda *a, **k: pytest.fail("зупинка з невідомим ботом"))
    with pytest.raises(fastapi.HTTPException) as e:
        app_mod.api_kill_all({"servers": ["clone1", "typo"]})
    assert e.value.status_code == 400


def test_api_passes_the_selection_through(monkeypatch):
    app_mod = pytest.importorskip("src.webpanel.app")
    seen = {}

    def _fake(servers=None):
        seen["servers"] = servers
        return {"clone1": {"ok": True, "demoted": 4}}

    monkeypatch.setattr(app_mod.data, "set_all_pairs_shadow_all", _fake)
    res = app_mod.api_kill_all({"servers": ["clone1"]})
    assert seen["servers"] == ["clone1"]
    assert res["ok"] is True and res["demoted"] == 4


def test_api_without_the_field_keeps_meaning_every_bot(monkeypatch):
    app_mod = pytest.importorskip("src.webpanel.app")
    seen = {}

    def _fake(servers=None):
        seen["servers"] = servers
        return {"primary": {"ok": True, "demoted": 1}}

    monkeypatch.setattr(app_mod.data, "set_all_pairs_shadow_all", _fake)
    app_mod.api_kill_all({})
    assert seen["servers"] is None
