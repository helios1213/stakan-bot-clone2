"""Audit the bot's own trading universe against real MEXC fee rates.

Two independent sources, deliberately compared:
  * PUBLIC   contract.mexc.com/api/v1/contract/detail  -> the CONTRACT's base
    maker/taker rate. Same for everyone.
  * PRIVATE  /account/tiered_fee_rate?symbol=X (web-signed, needs the webkey)
    -> what THIS account actually pays, i.e. base rate after promo/tier.

The private one is the answer soft-start needs. The public one is the sanity
check: where they disagree, the promo is doing something, and we want to see it
rather than assume it.

READ-ONLY: GETs only.

    docker compose exec stakan-bot python -m src._fee_audit --slot 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import urllib.request

# Профіль пристрою, а не хардкод: цей інструмент ходить із ВЕБКЕЄМ, тобто
# автентифіковано під тим самим акаунтом, що й бот. `Chrome/151` не існує як
# TLS-ціль curl_cffi, тож UA суперечив би відбитку.
from src.execution.webkey import device_profile as _dp

UA = _dp.for_slot(None).user_agent


def public_contract_detail() -> dict[str, dict]:
    req = urllib.request.Request(
        "https://contract.mexc.com/api/v1/contract/detail",
        headers={"accept": "*/*", "user-agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8", "replace")).get("data") or []
    return {c["symbol"]: c for c in data if c.get("symbol")}


def bot_universe(db_path: str) -> list[str]:
    """The pairs the bot actually trades, as MEXC contract symbols."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    syms: list[str] = []
    try:
        for (s,) in con.execute("SELECT DISTINCT symbol FROM pair_states ORDER BY symbol"):
            syms.append(s)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return syms


def to_contract(sym: str) -> str:
    """Bot symbol -> MEXC contract symbol.

    Uses the bot's OWN alias table (`to_mexc`), which is the only thing that
    knows 1000PEPEUSDT -> PEPE_USDT and MUUSDT -> MUSTOCK_USDT. A hand-rolled
    "insert an underscore" rule silently misses those and makes their fee look
    unknown — which is exactly how a 0% pair would get wrongly excluded.
    """
    from src.exchanges.mexc_rest import to_mexc
    return to_mexc(sym)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--db", default="/app/data/stakan.db")
    args = ap.parse_args()

    from src.execution.webkey.client_pool import WebkeyClientPool
    from src.execution.webkey.credentials import WebkeyStore
    from src.storage.db import Database

    db = Database(args.db)
    await db.connect()
    store = WebkeyStore(db, os.environ["MASTER_KEY"])
    pool = WebkeyClientPool(store)
    cl = await pool.get(args.slot)

    pub = public_contract_detail()
    universe = bot_universe(args.db)
    print(f"universe: {len(universe)} pairs | public contract detail: {len(pub)} contracts\n")

    print(f"{'pair':<18} {'pub_maker':>10} {'pub_taker':>10} | "
          f"{'acct_maker':>11} {'acct_taker':>11}  verdict")
    print("-" * 82)

    async def acct_fee(cs: str, tries: int = 3):
        """Account fee for one contract, with retries.

        A transient miss must NOT be read as "non-zero" — for soft-start that
        distinction is the whole point, so an unresolved pair stays unresolved
        and gets excluded, never guessed.
        """
        for i in range(tries):
            try:
                r = await cl._request("GET", f"/account/tiered_fee_rate?symbol={cs}",
                                      needs_web_sign=True)
                d = (r or {}).get("data") or {}
                if d.get("makerFee") is not None:
                    return d.get("makerFee"), d.get("takerFee")
            except Exception:
                pass
            await asyncio.sleep(0.4 * (i + 1))
        return None, None

    results = {}
    zero, nonzero, unknown = [], [], []
    for sym in universe:
        cs = to_contract(sym)
        p = pub.get(cs, {})
        pm, pt = p.get("makerFeeRate"), p.get("takerFeeRate")
        am, at = await acct_fee(cs)
        results[cs] = (pm, pt, am, at)
        await asyncio.sleep(0.25)          # be polite; this is a live account

        if isinstance(am, (int, float)):
            if float(am) == 0:
                verdict = "0% maker OK"
                zero.append(cs)
            else:
                verdict = "*** MAKER FEE ***"
                nonzero.append((cs, am, at))
        else:
            verdict = "unknown"
            unknown.append(cs)

        print(f"{cs:<18} {str(pm):>10} {str(pt):>10} | {str(am):>11} {str(at):>11}  {verdict}")

    print()
    print(f"0% maker for this account : {len(zero)}")
    print(f"NON-ZERO maker            : {len(nonzero)}")
    for s, m, t in nonzero:
        print(f"    {s:<18} maker={m} taker={t}")
    if unknown:
        print(f"unresolved                : {unknown}")

    # Where the account rate differs from the contract's base rate, the promo is
    # doing something. Reuses the results already collected — no second round of
    # requests (the first version re-queried here and hit rate limits).
    print("\ndisagreements public vs account (promo at work):")
    diffs = 0
    for cs, (pm, pt, am, at) in sorted(results.items()):
        if am is None or pm is None:
            continue
        if float(am) != float(pm):
            print(f"    {cs:<18} public_maker={pm}  ->  account_maker={am}")
            diffs += 1
    if not diffs:
        print("    (none — account rate matches the contract base rate everywhere)")

    print("\n>>> soft-start whitelist (account maker == 0):")
    print("    " + " ".join(zero) if zero else "    (empty)")

    await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
