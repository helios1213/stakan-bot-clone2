# stakan-bot — Deployment Guide

Binance→MEXC lead-lag arbitrage bot, controlled via Telegram, run with Docker
Compose. This guide covers deploying on a **fresh server**.

---

## Prerequisites

- A Linux server with **Docker** + **Docker Compose v2** installed.
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your
  numeric Telegram user id.
- (For live trading) a MEXC account with a captured **webkey** — added later
  via the bot, not during install.

---

## 1. Get the code

**Recommended — copy the whole directory** (captures everything on disk,
including any files not yet committed to git):

```bash
rsync -av --exclude data --exclude logs --exclude '.env' \
      stakan-bot/ NEWHOST:/root/stakan-bot/
```

`Dockerfile`, `requirements.txt`, and the strategy config (`config/global.yaml`,
`config/pairs/*.yaml`) all come along. `data/`, `logs/`, and `.env` are excluded
(created per-deployment).

> If you prefer `git clone` instead: first confirm the repo is complete —
> `git status` should show **no untracked `.py` files** under `src/` and no
> untracked `config/pairs/*.yaml`. Commit them first, or the clone will be
> missing modules and the bot will fail at runtime.

## 2. Configure secrets

Generate an encryption key, then create `.env`:

```bash
python scripts/gen_master_key.py     # prints the MASTER_KEY= line
```

```ini
# .env — secrets + global trading knobs (bot reads this at startup)
MASTER_KEY=<from gen_master_key.py — needed to decrypt webkeys, never change it>
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_OWNER_ID=<your numeric Telegram id>

DB_PATH=/app/data/stakan.db
LIVE_DB_PATH=/app/data/stakan-live.db
LOG_LEVEL=INFO
LOG_FILE=/app/logs/stakan.log

# Start in shadow (0); flip to 1 for real orders.
LIVE_TRADING_ENABLED=0
LIVE_MAX_MARGIN=500
LIVE_MAX_DRAWDOWN=20

IOC_MAX_ATTEMPTS=1
IOC_RETRY_DELAY_MS=0
IOC_PASSIVE_TICK_OFFSET=0
```

Webkey credentials are NOT set here — add them later via Telegram (🔑 Webkey →
slot → setup); they are stored encrypted in the DB, keyed by `MASTER_KEY`.

## 3. Build & start

```bash
docker compose build stakan-bot
docker compose up -d stakan-bot
docker compose logs -f stakan-bot        # watch startup
```

On first start the bot:
- creates `data/stakan.db` (shadow) and `data/stakan-live.db` (live) with the
  full schema and seeds the live-pair whitelist,
- connects to Binance/MEXC WebSockets and the Telegram API,
- runs in **shadow mode** (simulated trades only).

A healthy start logs `Application started`, `Telegram bot ready`, and periodic
`SHADOW: rec=... fill=...` funnel lines.

## 4. Enable live trading (optional, when ready)

1. In Telegram, open the bot → **🔑 Webkey** → pick a slot → **setup**, and
   paste your MEXC webkey. Credentials are encrypted (via `MASTER_KEY`) and
   stored in the DB — never in `.env`.
2. Assign a pair to the slot and **Enable live**.
3. Set `LIVE_TRADING_ENABLED=1` in `.env`, then redeploy (step 5).

## 5. Redeploying after a change

Source is **baked into the image** (not volume-mounted), so any code or `.env`
change needs a rebuild:

```bash
docker compose build stakan-bot && docker compose up -d stakan-bot
```

A restart re-syncs open positions from MEXC (authoritative) and interrupts
trading for ~1–2 min.

---

## Volumes & persistence

| Path | Mount | Notes |
|---|---|---|
| `./data` | `/app/data` (rw) | SQLite DBs — **back these up** |
| `./logs` | `/app/logs` (rw) | rotating logs |
| `./config` | `/app/config` (ro) | strategy YAML, hot-reloaded within 30s |

## Running the tests

```bash
docker compose run --rm --no-deps \
  -v "$PWD/src:/app/src" -v "$PWD/tests:/app/tests" -v "$PWD/pytest.ini:/app/pytest.ini" \
  stakan-bot python -m pytest -q
```
