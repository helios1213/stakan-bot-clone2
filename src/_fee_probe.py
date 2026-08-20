"""Find where MEXC exposes the ACCOUNT's per-pair fee rate over the web path.

Goal: soft-start must open futures positions ONLY on pairs that are actually 0%
for THIS account. Today nothing in the bot knows that up front — `fee_guard`
only finds out after a fill has already been charged, and
`realism.NON_ZERO_FEE_PAIRS` is an empty simulation stub, not a fact from the
exchange.

This probe is READ-ONLY: it issues GETs only. It never places, cancels, or
modifies anything. Safe to run against a live slot.

    docker compose exec stakan-bot python -m src._fee_probe --slot 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

# Private candidates (relative to https://contract.mexc.com/api/v1/private).
# Ordered cheapest-guess first. A hit is any 200 whose body mentions a fee rate.
PRIVATE_CANDIDATES = [
    "/account/tiered_fee_rate",
    "/account/fee_rate",
    "/account/user_fee_rate",
    "/account/fee",
    "/account/current_fee_rate",
    "/account/vip_level",
    "/account/risk_limit",
    "/account/assets",           # known-good — proves auth works at all
]

# Same, but taking a symbol — some fee endpoints are per-contract.
PRIVATE_WITH_SYMBOL = [
    "/account/tiered_fee_rate?symbol={sym}",
    "/account/fee_rate?symbol={sym}",
    "/contract/fee_rate?symbol={sym}",
]

# Public futures metadata — carries the CONTRACT's base maker/taker rates.
# Not account-specific, but it establishes the baseline the promo deviates from.
PUBLIC_CANDIDATES = [
    "https://contract.mexc.com/api/v1/contract/detail",
    "https://contract.mexc.com/api/v1/contract/detail?symbol={sym}",
]

FEE_HINTS = ("feerate", "makerfee", "takerfee", "maker_fee", "taker_fee",
             "commission", "feetier", "viplevel", "fee_rate")


def looks_feeish(payload) -> list[str]:
    """Return the fee-ish keys found anywhere in the payload."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(h in str(k).lower() for h in FEE_HINTS):
                    found.append(f"{k}={v}")
                walk(v)
        elif isinstance(o, list):
            for v in o[:50]:
                walk(v)

    walk(payload)
    return found


def public_get(url: str):
    req = urllib.request.Request(url, headers={
        "accept": "*/*",
        "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/151.0.0.0 Safari/537.36"),
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--db", default="/app/data/stakan.db")
    ap.add_argument("--symbol", default="ZEC_USDT")
    args = ap.parse_args()

    from src.execution.webkey.client_pool import WebkeyClientPool
    from src.execution.webkey.credentials import WebkeyStore
    from src.storage.db import Database

    db = Database(args.db)
    await db.connect()
    store = WebkeyStore(db, os.environ["MASTER_KEY"])
    pool = WebkeyClientPool(store)
    cl = await pool.get(args.slot)
    print(f"slot {args.slot}: client ready\n")

    print("=" * 70)
    print("PRIVATE (web-signed GET)")
    print("=" * 70)
    hits = []
    for ep in PRIVATE_CANDIDATES + [e.format(sym=args.symbol) for e in PRIVATE_WITH_SYMBOL]:
        try:
            r = await cl._request("GET", ep, needs_web_sign=True)
        except Exception as e:
            print(f"  [exc ] {ep:<42} {type(e).__name__}: {str(e)[:60]}")
            continue
        code = r.get("code") if isinstance(r, dict) else "?"
        fees = looks_feeish(r)
        blob = json.dumps(r, ensure_ascii=False)
        if fees:
            print(f"  [FEE ] {ep:<42} code={code}")
            for f in fees[:8]:
                print(f"           {f}")
            hits.append(ep)
        else:
            print(f"  [{str(code):>4}] {ep:<42} {blob[:90]}")

    print()
    print("=" * 70)
    print("PUBLIC contract metadata")
    print("=" * 70)
    for url in PUBLIC_CANDIDATES:
        u = url.format(sym=args.symbol)
        try:
            r = public_get(u)
        except Exception as e:
            print(f"  [exc ] {u}  {type(e).__name__}")
            continue
        data = r.get("data") if isinstance(r, dict) else None
        if isinstance(data, list):
            print(f"  [ ok ] {u}  -> {len(data)} contracts")
            zero, nonzero = [], []
            for c in data:
                mk = c.get("makerFeeRate")
                sym = c.get("symbol")
                if mk is None:
                    continue
                (zero if float(mk) == 0 else nonzero).append((sym, mk, c.get("takerFeeRate")))
            print(f"         makerFeeRate == 0 : {len(zero)} pairs")
            print(f"         makerFeeRate != 0 : {len(nonzero)} pairs")
            for s, mk, tk in zero[:5]:
                print(f"           ZERO   {s:<16} maker={mk} taker={tk}")
            for s, mk, tk in nonzero[:5]:
                print(f"           NONZERO{s:<16} maker={mk} taker={tk}")
        elif isinstance(data, dict):
            print(f"  [ ok ] {u}")
            print(f"         symbol={data.get('symbol')} maker={data.get('makerFeeRate')} "
                  f"taker={data.get('takerFeeRate')}")
        else:
            print(f"  [ ?? ] {u}  {json.dumps(r)[:120]}")

    await db.close()
    print(f"\nfee-bearing private endpoints: {hits or 'NONE'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
