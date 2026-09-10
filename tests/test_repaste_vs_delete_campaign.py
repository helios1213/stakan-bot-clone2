"""Переклеювання вебкея ПРОДОВЖУЄ кампанію прогріву, видалення — забуває.

Рішення оператора 2026-09-10. Мотив виміряний: primary слот 1, кампанія
1.84/3 доби, ключ помер о 19:32 UTC. За старою семантикою вставка нового
ключа рахувалась як «інший акаунт» -> нова кампанія -> `preclear` зливає
спот (там було MX 6.25 + SUI 56.53 ≈ 57 USDT за собівартістю). А акаунт той
самий, просто сесія протухла.

**Відрізнити перелогін від іншого акаунта за самим ключем неможливо** —
`account_key` це sha256-префікс РЯДКА ключа, і перелогін міняє його так само,
як зміна акаунта. Тож намір задає не код, а ДІЯ оператора:
    вставка поверх (майстер / панель `add`)      -> кампанія триває
    видалення (`/webkey_remove`, кнопка, `remove`) -> кампанія забувається

Тут тести ПРОВОДКИ: що кожен із чотирьох шляхів реально доносить прапорець
до `clear_slot_restrictions`, а не лише що функція вміє його приймати. Саме
цей клас діри ловився в проєкті дванадцять разів.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


# --------------------------------------------------------------------------
# 1. Сама кампанія
# --------------------------------------------------------------------------

def test_a_key_change_alone_never_resets_a_running_campaign():
    """Пінить ПОВЕДІНКУ, а не текст: у `start_if_new` свіжість кампанії
    визначають лише `started_at` і `expired()`. Мутант, що повертає
    `key_changed` у цю умову, має падати тут."""
    from src.execution.soft_start_campaign import SoftStartCampaign
    src = textwrap.dedent(inspect.getsource(SoftStartCampaign.start_if_new))
    tree = ast.parse(src)
    fresh = [n for n in ast.walk(tree)
             if isinstance(n, ast.Assign)
             and any(getattr(t, "id", "") == "fresh" for t in n.targets)]
    assert fresh, "у start_if_new більше немає рішення `fresh` — перевір тест"
    names = {n.id for n in ast.walk(fresh[0].value) if isinstance(n, ast.Name)}
    assert "key_changed" not in names, (
        "зміна ключа знову вирішує, чи починати кампанію — це саме той дефект, "
        "через який перелогін зливав спот живої кампанії")


# --------------------------------------------------------------------------
# 2. Пул: прапорець доходить до забуття кампанії
# --------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.cleared = []

    async def set_last_error(self, slot_id, err):
        self.cleared.append(slot_id)


def _pool():
    from src.execution.live_pool import LiveExecutorPool
    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p._executors = {}
    p._safety_controllers = {}
    p.webkey_store = _Store()
    p.live_db = None
    return p


@pytest.mark.asyncio
async def test_wipe_campaign_forgets_the_campaign_file(tmp_path, monkeypatch):
    import src.execution.soft_start_campaign as ssc
    seen = []
    monkeypatch.setattr(ssc, "forget_campaign",
                        lambda sid, *a, **k: (seen.append(sid), True)[1])
    out = await _pool().clear_slot_restrictions(1, reason="тест",
                                                wipe_campaign=True)
    assert seen == [1], "кампанію не забуто, хоча просили"
    assert any("кампан" in s for s in out), (
        "оператору не сказано, що кампанію забуто: %r" % (out,))


@pytest.mark.asyncio
async def test_without_the_flag_the_campaign_is_untouched(monkeypatch):
    import src.execution.soft_start_campaign as ssc
    seen = []
    monkeypatch.setattr(ssc, "forget_campaign",
                        lambda sid, *a, **k: (seen.append(sid), True)[1])
    out = await _pool().clear_slot_restrictions(1, reason="тест")
    assert seen == [], "переклеювання забуло кампанію — це і був баг"
    assert not any("кампан" in s for s in out)


# --------------------------------------------------------------------------
# 3. Telegram: три шляхи
# --------------------------------------------------------------------------

def _call_kwargs(src: str, fn: str) -> list[dict]:
    """Усі виклики `fn(...)` у тексті, розкладені на kwargs."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name == fn:
                out.append({k.arg: k.value for k in node.keywords})
    return out


def test_telegram_delete_paths_wipe_and_the_paste_path_does_not():
    """Обидві гілки видалення (`/webkey_remove` і кнопка меню) мусять просити
    забуття, а майстер вставки — ні."""
    wk = (SRC / "telegram_bot" / "cmd_webkey.py").read_text()
    bot = (SRC / "telegram_bot" / "bot.py").read_text()

    def wipes(calls):
        return [c for c in calls
                if isinstance(c.get("wipe_campaign"), ast.Constant)
                and c["wipe_campaign"].value is True]

    wk_calls = _call_kwargs(wk, "_clear_restrictions")
    # У cmd_webkey рівно один виклик із видалення і один зі вставки.
    assert len(wk_calls) >= 2, "шляхи зняття обмежень зникли з cmd_webkey"
    assert len(wipes(wk_calls)) == 1, (
        "у cmd_webkey має бути РІВНО один шлях, що забуває кампанію "
        "(видалення); знайдено %d" % len(wipes(wk_calls)))

    bot_calls = _call_kwargs(bot, "_clear_restrictions")
    assert len(wipes(bot_calls)) == 1, (
        "кнопка видалення в меню не забуває кампанію — слот дістав би чистий "
        "халт, але чужий облік прогріву")


# --------------------------------------------------------------------------
# 4. Панель: окремий маркер, а не суфікс
# --------------------------------------------------------------------------

def test_panel_remove_wipes_and_add_does_not():
    src = (SRC / "webpanel" / "data.py").read_text()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)}
    for name, want in (("remove_account_routed", True),
                       ("add_account_routed", False)):
        calls = [c for c in ast.walk(fns[name])
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", "") == "_queue_clear_after"]
        assert calls, f"{name} більше не ставить запит на чистку"
        got = any(
            isinstance(k.value, ast.Constant) and k.value.value is True
            for c in calls for k in c.keywords if k.arg == "wipe_campaign")
        assert got is want, (
            f"{name}: wipe_campaign={got}, а має бути {want} — "
            "видалення починає нову кампанію, вставка продовжує стару")


def test_the_wipe_marker_is_a_SEPARATE_key_not_a_suffix():
    """Значення `clear_restrictions_req` бот читає як таймстемп (`int(value)`),
    а нечитабельне значення трактується як ПРОТЕРМІНОВАНЕ. Тож суфікс у ньому
    мовчки вимкнув би зняття обмежень із панелі взагалі."""
    from src.execution.live_pool import LiveExecutorPool
    a = LiveExecutorPool._clear_request_key(2)
    b = LiveExecutorPool._campaign_wipe_key(2)
    assert a != b and b.endswith("slot2")
    assert not b.startswith(a), "маркер кампанії вкладено в основний"

    data = (SRC / "webpanel" / "data.py").read_text()
    assert "campaign_wipe_req:slot" in data, "панель не пише парний маркер"
    for ln in data.splitlines():
        if "clear_restrictions_req:slot" in ln and "f\"" in ln:
            assert ln.rstrip().endswith('slot{int(slot_id)}", str(now), now),') \
                or "slot{int(slot_id)}\"" in ln, (
                    "у значення/ключ основного маркера щось дописано: %s" % ln)


@pytest.mark.asyncio
async def test_the_bot_consumes_the_wipe_marker_and_removes_it():
    """Тест ПРОВОДКИ: маркер у `live_state` -> `wipe_campaign=True` у пулі,
    і обидва маркери зникають (інакше кожен цикл rebuild забував би кампанію
    заново)."""
    from src.execution.live_pool import LiveExecutorPool
    import time

    now = str(int(time.time()))
    rows = {"clear_restrictions_req:slot1": now,
            "campaign_wipe_req:slot1": now}

    class _DB:
        def __init__(self):
            self.deleted = []

        async def fetchall(self, sql, *a):
            return [(k, v) for k, v in rows.items()
                    if k.startswith("clear_restrictions_req:slot")]

        async def fetchone(self, sql, params):
            k = params[0]
            return (rows[k],) if k in rows else None

        async def execute(self, sql, params):
            self.deleted.append(params[0])
            rows.pop(params[0], None)

    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p.live_db = _DB()
    got = {}

    async def _clear(sid, *, reason, wipe_campaign=False):
        got.update(sid=sid, wipe=wipe_campaign)
        return []

    p.clear_slot_restrictions = _clear
    await p.sync_clear_requests()

    assert got == {"sid": 1, "wipe": True}, (
        "маркер кампанії не доїхав до пулу: %r" % (got,))
    assert set(p.live_db.deleted) == {"clear_restrictions_req:slot1",
                                      "campaign_wipe_req:slot1"}, (
        "маркер лишився — кампанія забувалась би на кожному циклі rebuild")


@pytest.mark.asyncio
async def test_a_plain_clear_request_does_not_wipe():
    """Контроль: без парного маркера — звичайне зняття обмежень."""
    from src.execution.live_pool import LiveExecutorPool
    import time

    rows = {"clear_restrictions_req:slot2": str(int(time.time()))}

    class _DB:
        async def fetchall(self, sql, *a):
            return list(rows.items())

        async def fetchone(self, sql, params):
            k = params[0]
            return (rows[k],) if k in rows else None

        async def execute(self, sql, params):
            rows.pop(params[0], None)

    p = LiveExecutorPool.__new__(LiveExecutorPool)
    p.live_db = _DB()
    got = {}

    async def _clear(sid, *, reason, wipe_campaign=False):
        got.update(sid=sid, wipe=wipe_campaign)
        return []

    p.clear_slot_restrictions = _clear
    await p.sync_clear_requests()
    assert got == {"sid": 2, "wipe": False}


def test_the_rpc_op_carries_the_flag_to_the_clone():
    """Скрипт живе на КОРОБЦІ клона (`/usr/local/bin`), а в репо лише тут —
    без цього поля видалення ключа на клоні мовчки продовжувало б кампанію."""
    rpc = (SRC.parent / "scripts" / "stakan-account-rpc.py")
    if not rpc.exists():
        pytest.skip("scripts/stakan-account-rpc.py є лише в репо основи")
    tree = ast.parse(rpc.read_text())
    calls = [c for c in ast.walk(tree)
             if isinstance(c, ast.Call)
             and getattr(c.func, "attr", "") == "request_clear_restrictions"]
    assert calls, "RPC більше не ставить запит на зняття обмежень"
    # Пінимо САМ ВИКЛИК, а не наявність слова у файлі: перша версія цього
    # тесту шукала "wipe_campaign" у тексті, а те слово є ще й у коментарі
    # поруч — мутант, що викидає аргумент, проходив зеленим.
    for c in calls:
        names = {n.attr for n in ast.walk(c) if isinstance(n, ast.Attribute)} | \
                {getattr(k, "value", None) for k in ast.walk(c)
                 if isinstance(k, ast.Constant)}
        assert len(c.args) >= 2 or any(k.arg == "wipe_campaign"
                                       for k in c.keywords), (
            "RPC-оп ковтає wipe_campaign — видалення ключа на КЛОНІ мовчки "
            "продовжувало б стару кампанію")
        assert "wipe_campaign" in names, (
            "другий аргумент береться не з поля wipe_campaign запиту")
