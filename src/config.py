"""
Configuration loader.

Two sources:
  1. Environment variables (.env) — secrets and infra paths.
  2. config/config.yaml — strategy parameters (hot-reloadable via Telegram).

Environment is loaded once at startup. YAML is reloaded on demand
when the user changes parameters via Telegram.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ------------------------------------------------------------------
# Environment (secrets, paths)
# ------------------------------------------------------------------
class EnvSettings(BaseSettings):
    """Loaded from .env file and OS environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    master_key: str = Field(..., min_length=32, description="Fernet key for credential encryption")
    telegram_bot_token: str = Field(..., min_length=20)
    telegram_owner_id: int = Field(..., gt=0)

    log_level: str = "INFO"
    log_file: str = "/app/logs/stakan.log"
    db_path: str = "/app/data/stakan.db"

    @field_validator("master_key")
    @classmethod
    def _check_fernet_key(cls, v: str) -> str:
        # Fernet keys are url-safe base64-encoded 32 bytes => 44 chars with `=` padding.
        if len(v) != 44 or not v.endswith("="):
            raise ValueError(
                "MASTER_KEY does not look like a valid Fernet key. "
                "Generate with: python scripts/gen_master_key.py"
            )
        return v


# ------------------------------------------------------------------
# YAML config — strategy parameters (hot-reloadable)
# ------------------------------------------------------------------
class AppConf(BaseModel):
    timezone: str = "Europe/Kyiv"
    heartbeat_interval_sec: int = 15


class BinanceConf(BaseModel):
    ws_base: str
    rest_base: str
    depth_levels: int = 20
    depth_update_speed_ms: int = 100
    reconnect_delay_sec: int = 3
    max_reconnect_delay_sec: int = 60
    # Optional diagnostic bookTicker stream. When True, subscribes to
    # <symbol>@bookTicker in parallel and logs how much earlier real-time
    # bookTicker sees top-of-book changes than the throttled @depth@100ms
    # stream. Production paths untouched.
    book_ticker_enabled: bool = False
    # Periodic summary log interval (sec). 0 disables periodic logs.
    book_ticker_log_interval_sec: int = 60
    # When True, bookTicker updates the OrderBook's cached top-of-book
    # (best_bid/ask) directly, so the detector reacts to top-of-book
    # changes earlier than waiting for @depth@100ms. Full depth diffs
    # still apply normally for deep-book data. Requires book_ticker_enabled=True.
    book_ticker_feed_enabled: bool = False


class MexcConf(BaseModel):
    ws_base: str
    rest_base: str
    reconnect_delay_sec: int = 3
    max_reconnect_delay_sec: int = 60
    funding_intervals_utc: list[str] = ["00:00", "08:00", "16:00"]
    funding_cutoff_sec: int = 60


class UniverseConf(BaseModel):
    """Trading universe — explicit whitelist of pairs the bot trades.


    There is no discovery, no scoring, no periodic refresh. To change the
    universe: edit config.yaml, rebuild image, restart container.
    """
    whitelist_priority: list[str] = []         # the bot will trade exactly these
    blacklist_symbols: list[str] = []          # safety net — overrides whitelist if symbol appears in both
    reference_only_symbols: list[str] = []     # included in universe (WS subscribed) but detectors don't emit signals on them

    # Allow tolerant parsing of old configs that still ship the legacy
    # scanner block (filters/scoring_weights/refresh_interval_min/etc.).
    # The fields above are the only ones consumed; everything else is ignored.
    model_config = {"extra": "ignore"}


class ShadowConf(BaseModel):
    enabled: bool = True
    entry_latency_ms: int = 168          # PENGU live p50 — enable flag + representative
    entry_latency_min_ms: int = 150      # shadow IOC sleep window low  (PENGU submit p10)
    entry_latency_max_ms: int = 205      # shadow IOC sleep window high (PENGU submit p90)
    fill_latency_ms: int = 50
    # How far BEHIND the real MEXC book our WS reconstruction runs.
    # Shadow anchors its IOC limit AND judges the fill against the SAME
    # reconstruction, so this lag cancels itself out and shadow fills on
    # liquidity the exchange had already taken — measured 2026-08-21 as
    # 1.55-1.63x more fills per attempt than live on the same pairs.
    # Waiting this much longer before walking the ladder removes the
    # cancellation. 0 = off; set it only from measured [BOOKLAG] data,
    # otherwise it is just another unvalidated fudge factor.
    mexc_feed_lag_ms: int = 0
    # Refuse to fill against a book that has not ticked for this long. A synced
    # book is not a fresh one. 0 = off.
    max_book_age_ms: int = 0
    # Яка ЧАСТКА показаного обсягу рівня реально дістається нам у симуляції.
    # Симулятор забирає рівень цілком, ніби ми єдиний покупець — звідси 5.7%
    # часткових філів у shadow проти 46% у live на PEPE.
    # 1.0 = вимкнено. Вмикати ЛИШЕ з даних shadow_twin (T2.2): там видно, чи
    # розходження в частці заповнення, а не вгадувати. На відміну від
    # mexc_feed_lag_ms це плавний регулятор — він міняє РОЗМІР філу, а не факт.
    queue_frac: float = 1.0


class RiskConf(BaseModel):
    max_positions_per_symbol: int = 1


class TelegramConf(BaseModel):
    send_realtime_open: bool = True
    send_realtime_close: bool = True


class LoggingConf(BaseModel):
    pass


class YamlConfig(BaseModel):
    app: AppConf
    binance: BinanceConf
    mexc: MexcConf
    universe: UniverseConf
    shadow: ShadowConf
    risk: RiskConf
    telegram: TelegramConf
    logging: LoggingConf

    # Allow extra fields in yaml (e.g. legacy lead_lag, walls sections that
    # may still be present in config.yaml — they're ignored
    # only static_gap detector is in use.)
    model_config = {"extra": "ignore"}


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------
_CONFIG_PATH = Path("config/config.yaml")


def load_yaml(path: Path = _CONFIG_PATH) -> YamlConfig:
    """Load and validate YAML config."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)
    return YamlConfig.model_validate(raw)


def load_env() -> EnvSettings:
    """Load and validate environment settings."""
    return EnvSettings()  # type: ignore[call-arg]
