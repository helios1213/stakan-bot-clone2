"""YAML-based per-pair config loader.

Single source of truth per pair:
  config/pairs/<SYMBOL>.yaml — one file = one pair = full detector + exit config
  config/global.yaml          — defaults used when a pair file is missing a key

The loader watches file mtime; YAML edits become active within
`reload_ttl_sec` (default 30s) without a process restart.

The `pair_configs.gap_*` DB tuning columns were dropped 2026-06-15; the
loader reads tuning ONLY from YAML. `pair_configs` now defines just which
symbols exist; there is no runtime DB tuning fallback.
"""
from __future__ import annotations

import logging
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Schema dataclasses. Defaults match the pre-Stage-18 hardcoded fallbacks
# from main.py / static_gap_detector.py so behaviour is unchanged when a
# pair file omits a field.
# ──────────────────────────────────────────────────────────────────────

@dataclass
class DetectorConfig:
    """Per-pair static_gap detector tuning.

    Fields here exactly mirror the StaticGapConf dataclass that the
    runtime constructs at startup, except per-pair. Names match the
    YAML keys to keep mental overhead low.
    """
    # enabled / scan_interval_sec are GLOBAL-ONLY: the runtime builds one
    # StaticGapConf from global.yaml at startup (main.py); PerPairDetectorOverride
    # does NOT carry them, so per-pair yaml values are ignored. Kept as fields for
    # the global path + /get_config display; not materialized into pair yamls.
    enabled: bool = True
    scan_interval_sec: float = 0.2
    min_ticks: int = 1
    cooldown_sec: float = 5.0
    long_only: bool = False
    short_only: bool = False
    max_spread_bps: float = 0.0   # >0 = reject signals when MEXC book spread exceeds this (bps); 0=off
    # >0 = reject signals whose BINANCE<->MEXC MID gap is below this many
    # ticks. Distinct from min_ticks, which reads the same-side quote gap
    # and so is also satisfied by a wide MEXC book:
    #     gap_ticks = mid_gap_ticks + (mexc_spread - binance_spread)/2
    # Ticks rather than bps because the mid sits on a half-tick grid, and
    # a bps threshold slides across those cohorts as the price moves.
    # 0 = off.
    min_mid_gap_ticks: float = 0.0
    # >0 = reject signals whose MID gap is ABOVE this many ticks.
    # Парний до min_mid_gap_ticks: без нього пару не можна зняти з
    # bps-смуги (min/max_mexc_lag_pct), бо верхня межа просто зникне,
    # а широкі геп-когорти на PEPE стабільно програють. 0 = off.
    max_mid_gap_ticks: float = 0.0
    # >0 = reject signals whose EXECUTABLE edge is below this many ticks.
    # exec = (binance_bid - mexc_ask)/tick for a long, mirrored for a short:
    # the dislocation net of both books' spreads, i.e. what is left after
    # crossing to the price we would really fill at. Distinct from both
    # min_ticks (same-side quote gap) and min_mid_gap_ticks (mid), neither
    # of which subtracts the cost of reaching the touch. 0 = off.
    min_exec_ticks: float = 0.0


@dataclass
class ExitStrategyConfig:
    """Per-pair exit strategy selector.

    Every pair uses the simple_trail exit model: exit on -N ticks adverse
    from entry, or when price retraces M ticks from running peak. Only
    stop_loss (catastrophic safety net) and time_limit (max_hold_sec)
    remain active alongside the trail rules.

    BPS fields (basis points, 1 bps = 0.01% of entry price):
      When a *_bps field is > 0, it OVERRIDES the corresponding *_ticks
      field. This makes thresholds universal across all pairs regardless
      of tick size. Set to 0 to use ticks (backward compatible).

      Example: stop_adverse_bps=10 on ZEC ($550) = $0.55 adverse
               = 55 ticks (tick=$0.01). Same 10bps on PEPE ($0.003624)
               = $0.0003624 adverse — same % risk, different tick count.
    """
    stop_adverse_ticks: int = 2        # exit when adverse from entry >= N ticks
    trail_distance_ticks: int = 1      # trail stop = peak - N ticks (long)
    min_hold_ms: int = 300             # block exits during first N ms (spread noise)

    # BPS overrides — when > 0, OVERRIDE the corresponding _ticks field.
    stop_adverse_bps: float = 0.0      # adverse stop in basis points (0 = use ticks)
    trail_distance_bps: float = 0.0    # trail distance in bps (0 = use ticks)
    breakeven_trigger_bps: float = 0.0 # breakeven trigger in bps (0 = use ticks)

    # Breakeven lock: once peak reaches trigger ticks of profit,
    # exit if current drops back to ≤0.5 ticks. Locks in micro-profit
    # covering fees. Set to 0 to disable. For lead-lag where gap=3-4t,
    # reasonable values: 1.5-2.0 ticks.
    breakeven_trigger_ticks: float = 0.0

    # Stall detection: if peak hasn't improved for stall_timeout_ms
    # AND trade is currently in profit (peak_favorable_ticks > 0), exit.
    # Catches trades that showed life but stopped progressing. Set to 0 to
    # disable. Reasonable: 1500ms for fast pairs (SUI/PENGU), 2000ms slower.
    stall_timeout_ms: int = 0

    # Dead-on-arrival cutoff: if peak NEVER exceeded entry by even
    # 0.001% (mfe ~ 0) after dead_on_arrival_timeout_ms — exit. Data shows
    # 841/864 such trades end as losers (-$0.18 avg, 2.7% win rate).
    # Set to 0 to disable. Reasonable: 1000ms.
    dead_on_arrival_timeout_ms: int = 0

    # Never-green deep-dip cut (the dead-from-entry killer). When > 0, cut a
    # position that is STILL never-green (peak_favorable_ticks <=
    # nevergreen_peak_ticks) AND already nevergreen_adverse_ticks adverse,
    # once elapsed >= nevergreen_cut_ms. Data: such trades win ~1.4% (vs 64%
    # for ever-green at same depth); dip-recover winners are shallow (median
    # -2t) and mostly green by ~1.5s, so they survive this. Targets the
    # dead-from-entry ~42% of trades that are the main bleed. friend State-1
    # rule recalibrated for taker. Set to 0 to disable (default, dormant).
    nevergreen_cut_ms: int = 0
    nevergreen_adverse_ticks: float = 4.0
    nevergreen_peak_ticks: float = 1.0

    # Binance reversal time window: only check Binance reversal during
    # first N ms after entry. After this window, MEXC is already moving
    # and Binance micro-dips are normal volatility, not "broken thesis".
    # Set to 0 to disable time limit (always check — old behavior).
    # Reasonable: 2000-3000ms.
    binance_reversal_max_ms: int = 0

    def effective_stop_adverse_ticks(self, entry_price: float, tick_scaled: float) -> float:
        """Resolve stop threshold in ticks, preferring bps if set."""
        if self.stop_adverse_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.stop_adverse_bps / 10000.0) / tick_scaled
        return float(self.stop_adverse_ticks)

    def effective_trail_distance_ticks(self, entry_price: float, tick_scaled: float) -> float:
        """Resolve trail distance in ticks, preferring bps if set."""
        if self.trail_distance_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.trail_distance_bps / 10000.0) / tick_scaled
        return float(self.trail_distance_ticks)

    def effective_breakeven_trigger_ticks(self, entry_price: float, tick_scaled: float) -> float:
        """Resolve breakeven trigger in ticks, preferring bps if set."""
        if self.breakeven_trigger_bps > 0 and entry_price > 0 and tick_scaled > 0:
            return (entry_price * self.breakeven_trigger_bps / 10000.0) / tick_scaled
        return self.breakeven_trigger_ticks


@dataclass
class ExecutionConfig:
    """Per-pair execution + sizing tuning (formerly pair_configs DB columns).

    Mirrors every field of strategy.shadow_engine.PairExecConfig EXCEPT
    ``mode`` — mode is runtime state (toggled live/shadow via Telegram /
    fee-guard), so it stays in the DB. Everything here is static tuning and
    now lives in YAML so there is ONE place to edit a pair's parameters.

    Defaults match the PairExecConfig dataclass defaults so behaviour is
    unchanged when a pair file omits a field.
    """
    ioc_offset_ticks: int = 0          # 0=at-touch, N>0=cross N ticks, N<0=inside spread
    ioc_max_attempts: int = 2
    ioc_attempt_interval_ms: int = 80

    margin_min_usdt: float = 23.0
    margin_max_usdt: float = 30.0
    leverage_min: int = 50
    leverage_max: int = 80

    stop_loss_ticks: int = 5
    sl_grace_sec: float = 0.0
    max_hold_sec: int = 600

    # Fallback for a pair that does not set them. Every live pair does set
    # them explicitly; keeping the fallback in step means a newly added pair
    # starts on the same pacing instead of a much slower legacy one.
    cooldown_after_loss_sec: int = 3
    cooldown_after_win_sec: int = 2

    binance_reversal_ticks: float = 2.0
    gap_retrace_frac: float = 0.0          # >0 = gap-relative binance_reversal (cut on retrace of entry gap); 0 = fixed-tick

    # Momentum entry filter (per-pair). Skips a signal unless the Binance leader
    # is trending in the signal direction (time-decay EMA). Off by default;
    # gated solely on this per-pair yaml flag (the global MOMENTUM_FILTER env
    # toggle was removed).
    momentum_filter: bool = False
    momentum_tau_sec: float = 2.0          # EMA time constant, seconds
    momentum_threshold_bps: float = 2.0    # how far px must lean past its EMA

    # Minimum Binance↔MEXC mid-gap at entry (signal.mexc_lag_pct, in %; 0.005 =
    # 0.5 bps). Blocks entries with a tiny mid-to-mid gap that the tick gate
    # (min_ticks) lets through — they fill straight into the adverse stop
    # (mid-gap < 0.5bps → ~9% win in the 1093-trade study). Re-introduces the
    # filter removed 2026-05-29. 0 = disabled (default; behaviour-preserving).
    min_mexc_lag_pct: float = 0.0
    # Upper bound on the same gap. 0 = no cap (prior behaviour).
    max_mexc_lag_pct: float = 0.0


@dataclass
class PairConfig:
    """Everything strategy-related for one trading pair.

    detector + exit_strategy + execution all live in YAML (single source of
    truth for tuning). Only runtime STATE (live/shadow/paused mode,
    fee-guard, slot assignment) remains in the pair_states / webkey_slots /
    pair_configs DB tables.
    """
    symbol: str
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    exit_strategy: ExitStrategyConfig = field(default_factory=ExitStrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


# ──────────────────────────────────────────────────────────────────────
# Parsing — turning raw dict (from YAML) into typed config.
# ──────────────────────────────────────────────────────────────────────

def _as_bool(v, default: bool) -> bool:
    """Robust YAML→bool. Guards the quoted-string trap: bool('false') is True,
    so a `momentum_filter: "false"` would wrongly ENABLE the flag. Unquoted
    YAML booleans already parse to bool; this only matters for strings/ints."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _warn_unknown_keys(raw: dict | None, dc, section: str, where: str) -> None:
    """Попередити про ключ, якого ця збірка не знає.

    Парсери читають поля через raw.get(...), тому ключ, якого немає в
    датакласі, просто не читається — без помилки й без сліду. Небезпечно це
    рівно в момент деплою: якщо конфіг поїхав раніше за код, стара збірка
    ІГНОРУЄ новий ключ і при цьому слухається старих, які ти вже занулив, —
    тобто працює зовсім без того гейту, який ти думаєш, що поставив.
    Спіймано на PEPE 2026-08-03: ~4 хвилини без гейту по гепу.

    Тільки WARNING: ямл із ключем із майбутньої версії мусить лишатись
    завантажуваним, інакше відкат образу покладе бота.
    """
    if not raw:
        return
    known = {f.name for f in dataclasses.fields(dc)}
    extra = sorted(set(raw) - known)
    if extra:
        logger.warning(
            "[CONFIG] %s: секція %s містить ключі, яких ця збірка НЕ ЧИТАЄ: %s. "
            "Вони не діють. Якщо це нові ключі — спершу деплой коду, потім конфіг.",
            where, section, ", ".join(extra),
        )


def _parse_detector(raw: dict | None, defaults: DetectorConfig) -> DetectorConfig:
    """Build a DetectorConfig from raw dict, falling back to defaults per field."""
    raw = raw or {}
    return DetectorConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        scan_interval_sec=float(raw.get("scan_interval_sec", defaults.scan_interval_sec)),
        min_ticks=int(raw.get("min_ticks", defaults.min_ticks)),
        cooldown_sec=float(raw.get("cooldown_sec", defaults.cooldown_sec)),
        long_only=bool(raw.get("long_only", defaults.long_only)),
        short_only=bool(raw.get("short_only", defaults.short_only)),
        max_spread_bps=float(raw.get("max_spread_bps", defaults.max_spread_bps)),
        min_mid_gap_ticks=float(raw.get("min_mid_gap_ticks", defaults.min_mid_gap_ticks)),
        max_mid_gap_ticks=float(raw.get("max_mid_gap_ticks", defaults.max_mid_gap_ticks)),
        min_exec_ticks=float(raw.get("min_exec_ticks", defaults.min_exec_ticks)),
    )


def _parse_exit_strategy(raw: dict | None, defaults: ExitStrategyConfig) -> ExitStrategyConfig:
    """Build an ExitStrategyConfig from raw dict (every pair uses simple_trail)."""
    raw = raw or {}
    return ExitStrategyConfig(
        stop_adverse_ticks=int(raw.get("stop_adverse_ticks", defaults.stop_adverse_ticks)),
        trail_distance_ticks=int(raw.get("trail_distance_ticks", defaults.trail_distance_ticks)),
        min_hold_ms=int(raw.get("min_hold_ms", defaults.min_hold_ms)),
        stop_adverse_bps=float(raw.get("stop_adverse_bps", defaults.stop_adverse_bps)),
        trail_distance_bps=float(raw.get("trail_distance_bps", defaults.trail_distance_bps)),
        breakeven_trigger_bps=float(raw.get("breakeven_trigger_bps", defaults.breakeven_trigger_bps)),
        breakeven_trigger_ticks=float(raw.get(
            "breakeven_trigger_ticks", defaults.breakeven_trigger_ticks)),
        stall_timeout_ms=int(raw.get("stall_timeout_ms", defaults.stall_timeout_ms)),
        dead_on_arrival_timeout_ms=int(raw.get(
            "dead_on_arrival_timeout_ms", defaults.dead_on_arrival_timeout_ms)),
        nevergreen_cut_ms=int(raw.get(
            "nevergreen_cut_ms", defaults.nevergreen_cut_ms)),
        nevergreen_adverse_ticks=float(raw.get(
            "nevergreen_adverse_ticks", defaults.nevergreen_adverse_ticks)),
        nevergreen_peak_ticks=float(raw.get(
            "nevergreen_peak_ticks", defaults.nevergreen_peak_ticks)),
        binance_reversal_max_ms=int(raw.get(
            "binance_reversal_max_ms", defaults.binance_reversal_max_ms)),
    )


def _validate_exit_strategy(raw_exit: dict | None, symbol: str) -> None:
    """Fail loudly if a yaml EXPLICITLY sets an exit bps threshold to <= 0.

    bps is the canonical exit unit. An explicit 0 (or negative) ``*_bps`` would
    silently fall back to the deprecated ``*_ticks`` default inside
    ``effective_*_ticks`` — a footgun while tuning (you think you set a bps stop,
    you actually run on the tick fallback). Only EXPLICIT values are checked:
    omitting a bps to inherit a positive global is fine and is NOT flagged.

    NOTE: breakeven_trigger_bps is intentionally NOT guarded — 0 there is a valid
    "breakeven off" (the exit rule has an explicit ``> 0`` gate; see
    ShadowEngine._check_simple_trail_exit). Only adverse + trail are guarded.
    """
    raw_exit = raw_exit or {}
    where = (f"config/pairs/{symbol}.yaml" if symbol != "global"
             else "config/global.yaml")
    for field in ("stop_adverse_bps", "trail_distance_bps"):
        if field not in raw_exit:
            continue  # omitted → inherits global (validated separately) → fine
        try:
            val = float(raw_exit[field])
        except (TypeError, ValueError):
            raise ValueError(
                f"{symbol}: {field} must be a positive number, "
                f"got {raw_exit[field]!r}. Fix {where}"
            )
        if val <= 0:
            raise ValueError(
                f"{symbol}: {field} must be >0 (bps is the canonical exit unit; "
                f"0 silently falls back to the tick-default). Got {val}. Fix {where}"
            )


def _parse_execution(raw: dict | None, defaults: ExecutionConfig) -> ExecutionConfig:
    """Build an ExecutionConfig from raw dict, falling back to defaults per field."""
    raw = raw or {}
    return ExecutionConfig(
        ioc_offset_ticks=int(raw.get("ioc_offset_ticks", defaults.ioc_offset_ticks)),
        ioc_max_attempts=int(raw.get("ioc_max_attempts", defaults.ioc_max_attempts)),
        ioc_attempt_interval_ms=int(raw.get(
            "ioc_attempt_interval_ms", defaults.ioc_attempt_interval_ms)),
        margin_min_usdt=float(raw.get("margin_min_usdt", defaults.margin_min_usdt)),
        margin_max_usdt=float(raw.get("margin_max_usdt", defaults.margin_max_usdt)),
        leverage_min=int(raw.get("leverage_min", defaults.leverage_min)),
        leverage_max=int(raw.get("leverage_max", defaults.leverage_max)),
        stop_loss_ticks=int(raw.get("stop_loss_ticks", defaults.stop_loss_ticks)),
        sl_grace_sec=float(raw.get("sl_grace_sec", defaults.sl_grace_sec)),
        max_hold_sec=int(raw.get("max_hold_sec", defaults.max_hold_sec)),
        cooldown_after_loss_sec=int(raw.get(
            "cooldown_after_loss_sec", defaults.cooldown_after_loss_sec)),
        cooldown_after_win_sec=int(raw.get(
            "cooldown_after_win_sec", defaults.cooldown_after_win_sec)),
        binance_reversal_ticks=float(raw.get(
            "binance_reversal_ticks", defaults.binance_reversal_ticks)),
        gap_retrace_frac=float(raw.get("gap_retrace_frac", defaults.gap_retrace_frac)),
        momentum_filter=_as_bool(raw.get("momentum_filter"), defaults.momentum_filter),
        momentum_tau_sec=float(raw.get("momentum_tau_sec", defaults.momentum_tau_sec)),
        momentum_threshold_bps=float(raw.get(
            "momentum_threshold_bps", defaults.momentum_threshold_bps)),
        min_mexc_lag_pct=float(raw.get("min_mexc_lag_pct", defaults.min_mexc_lag_pct)),
        max_mexc_lag_pct=float(raw.get("max_mexc_lag_pct", defaults.max_mexc_lag_pct)),
    )


# ──────────────────────────────────────────────────────────────────────
# Loader — file-watching cache, refreshed every reload_ttl_sec.
# ──────────────────────────────────────────────────────────────────────

class ConfigLoader:
    """Reads global.yaml + pairs/*.yaml. Refreshes on mtime change.

    Usage:
        loader = ConfigLoader("/app/config")
        loader.load()                       # one-shot at startup
        cfg = loader.get("PENGUUSDT")       # returns PairConfig (uses cache)
        # ... later, after user edits PENGUUSDT.yaml:
        loader.maybe_reload()               # called periodically
        cfg = loader.get("PENGUUSDT")       # returns fresh values
    """

    def __init__(self, config_dir: str | Path, reload_ttl_sec: float = 30.0):
        self.config_dir = Path(config_dir)
        self.pairs_dir = self.config_dir / "pairs"
        self.global_path = self.config_dir / "global.yaml"
        self.reload_ttl_sec = reload_ttl_sec

        # Cache state
        self._global_defaults: DetectorConfig = DetectorConfig()
        self._global_exit_strategy: ExitStrategyConfig = ExitStrategyConfig()
        self._global_execution: ExecutionConfig = ExecutionConfig()
        self._pairs: dict[str, PairConfig] = {}
        self._last_check_at: float = 0.0
        self._file_mtimes: dict[str, float] = {}  # path → mtime at last load

    # ── public API ─────────────────────────────────────────────────────

    def load(self) -> None:
        """Initial load. Reads global.yaml and all pairs/*.yaml."""
        self._reload_global()
        self._reload_all_pairs()
        self._last_check_at = time.monotonic()
        logger.info(
            "ConfigLoader: loaded global + %d pair configs from %s",
            len(self._pairs), self.config_dir,
        )

    def maybe_reload(self) -> bool:
        """If TTL elapsed, check file mtimes and reload changed files.

        Returns True if anything was reloaded.
        """
        if time.monotonic() - self._last_check_at < self.reload_ttl_sec:
            return False
        self._last_check_at = time.monotonic()
        return self._reload_changed_files()

    def get(self, symbol: str) -> PairConfig:
        """Return PairConfig for symbol. If no per-pair file exists,
        returns a config built entirely from global defaults."""
        if symbol in self._pairs:
            return self._pairs[symbol]
        return PairConfig(
            symbol=symbol,
            detector=self._global_defaults,
            exit_strategy=self._global_exit_strategy,
            execution=self._global_execution,
        )

    def list_pairs(self) -> list[str]:
        """All pair symbols that have a YAML file."""
        return sorted(self._pairs.keys())

    def global_defaults(self) -> DetectorConfig:
        return self._global_defaults

    # ── internal ───────────────────────────────────────────────────────

    def _reload_global(self) -> None:
        """Load defaults from global.yaml. Missing file → use hardcoded defaults."""
        if not self.global_path.exists():
            logger.warning(
                "Global config not found at %s, using built-in defaults",
                self.global_path,
            )
            self._global_defaults = DetectorConfig()
            self._global_exit_strategy = ExitStrategyConfig()
            self._global_execution = ExecutionConfig()
            return

        with open(self.global_path) as f:
            raw = yaml.safe_load(f) or {}
        self._global_defaults = _parse_detector(raw.get("detector"), DetectorConfig())
        self._global_exit_strategy = _parse_exit_strategy(
            raw.get("exit_strategy"), ExitStrategyConfig(),
        )
        self._global_execution = _parse_execution(
            raw.get("execution"), ExecutionConfig(),
        )
        _validate_exit_strategy(raw.get("exit_strategy"), "global")
        self._file_mtimes[str(self.global_path)] = self.global_path.stat().st_mtime

    def _reload_all_pairs(self) -> None:
        """Load all pairs/*.yaml. Resets the cache."""
        new_pairs: dict[str, PairConfig] = {}
        if not self.pairs_dir.exists():
            self._pairs = new_pairs
            return
        for path in sorted(self.pairs_dir.glob("*.yaml")):
            symbol = path.stem
            try:
                pc = self._load_pair_file(path, symbol)
                new_pairs[symbol] = pc
                self._file_mtimes[str(path)] = path.stat().st_mtime
            except Exception as e:
                logger.error("Failed to load pair config %s: %s", path, e)
        self._pairs = new_pairs

    def _load_pair_file(self, path: Path, symbol: str) -> PairConfig:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        _validate_exit_strategy(raw.get("exit_strategy"), symbol)
        _where = f"config/pairs/{symbol}.yaml"
        _warn_unknown_keys(raw.get("detector"), DetectorConfig, "detector", _where)
        _warn_unknown_keys(raw.get("exit_strategy"), ExitStrategyConfig,
                           "exit_strategy", _where)
        _warn_unknown_keys(raw.get("execution"), ExecutionConfig, "execution", _where)
        return PairConfig(
            symbol=symbol,
            detector=_parse_detector(raw.get("detector"), self._global_defaults),
            exit_strategy=_parse_exit_strategy(
                raw.get("exit_strategy"), self._global_exit_strategy,
            ),
            execution=_parse_execution(
                raw.get("execution"), self._global_execution,
            ),
        )

    def _reload_changed_files(self) -> bool:
        """Check mtime of every tracked file + new files. Reload only changes.

        Returns True if anything was reloaded.
        """
        reloaded_any = False

        # Global config
        if self.global_path.exists():
            mt = self.global_path.stat().st_mtime
            if mt != self._file_mtimes.get(str(self.global_path)):
                self._reload_global()
                # Globals changed → rebuild all pairs because they inherit defaults
                self._reload_all_pairs()
                logger.info("ConfigLoader: global config changed, rebuilt all pairs")
                return True

        # Per-pair files: detect changed AND new AND deleted
        if not self.pairs_dir.exists():
            return reloaded_any

        seen: set[str] = set()
        for path in self.pairs_dir.glob("*.yaml"):
            symbol = path.stem
            seen.add(symbol)
            mt = path.stat().st_mtime
            if mt != self._file_mtimes.get(str(path)):
                try:
                    self._pairs[symbol] = self._load_pair_file(path, symbol)
                    self._file_mtimes[str(path)] = mt
                    reloaded_any = True
                    logger.info("ConfigLoader: reloaded %s", symbol)
                except Exception as e:
                    logger.error("Failed to reload pair config %s: %s", path, e)

        # Deleted files (in cache but not on disk anymore)
        for symbol in list(self._pairs.keys()):
            if symbol not in seen:
                path_str = str(self.pairs_dir / f"{symbol}.yaml")
                self._pairs.pop(symbol, None)
                self._file_mtimes.pop(path_str, None)
                reloaded_any = True
                logger.info("ConfigLoader: removed deleted pair %s", symbol)

        return reloaded_any
