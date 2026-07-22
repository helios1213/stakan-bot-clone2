-- =====================================================================
-- Migration: nizar_pengu_strategy
-- Date:      2026-05-09
-- Purpose:   Configure PENGUUSDT pair to mirror friend's reverse-engineered
--            strategy (522 trades, +$54.24, expectancy +$0.107/trade,
--            WR 47.6%, payoff 1.79).
-- =====================================================================
--
-- Strategy summary (reconstructed from raw fills + recorder snapshots):
--   ENTRY:
--     - Detector: static_gap with require_executable_edge=True (set via env)
--     - Trigger: gap_mid_ticks >= 1 AND exec_buy_ticks >= 0  (long)
--                gap_mid_ticks <= -1 AND exec_sell_ticks >= 0 (short)
--     - 90% of profitable entries match this AND-pattern
--   ORDER PLACEMENT:
--     - IOC LIMIT with ~2 ticks slippage tolerance (taker, 99% of his fills)
--   EXIT:
--     - Primary: gap closure (mexc_caught_up) — 87% of his exits
--     - Soft SL: -3 ticks (his MAE distribution: median -2t, p75 -3t)
--     - Max hold: 30s safety net
--     - NO trailing TP, NO quick scalp (he doesn't use these)
--   POSITION:
--     - Margin $25, leverage 50x (matches his ~$5K notional)
--
-- IMPORTANT: nizar_mode_enabled column is added by db.py migration on
-- bot startup. If applying this SQL BEFORE first restart with new db.py,
-- the INSERT will fail. Always restart bot first to apply schema migration,
-- THEN run this SQL.
--
-- Apply with:
--   sqlite3 /app/data/stakan-live.db < scripts/migrations/nizar_pengu_strategy.sql
--   (also apply to stakan.db if you want shadow alongside)
-- =====================================================================

-- Insert OR REPLACE PENGU config with Nizar parameters.
INSERT OR REPLACE INTO pair_configs (
    symbol,
    strategy_type,
    disabled_detectors,
    detector_strategy,

    -- Entry filters
    min_confidence,
    min_mexc_lag_pct,
    max_mexc_lag_pct,

    -- IOC entry
    ioc_offset_bps,
    ioc_max_attempts,
    ioc_attempt_interval_ms,

    -- Position sizing
    margin_usdt,
    margin_min_usdt,
    margin_max_usdt,
    leverage,
    leverage_min,
    leverage_max,

    -- Exit logic
    stop_loss_roi_pct,
    stop_loss_ticks,
    take_profit_roi_pct,
    trailing_activation_roi_pct,
    trailing_distance_roi_pct,
    max_hold_sec,
    min_hold_sec_for_exits,
    sl_grace_sec,

    -- Quick scalp (DISABLED for Nizar)
    quick_scalp_enabled,
    quick_scalp_window_sec,
    quick_scalp_roi_pct,

    -- MEXC catch-up exit (PRIMARY exit for Nizar)
    exit_on_mexc_caught_up,
    mexc_caught_up_threshold_pct,
    exit_on_reverse_signal,

    -- Cooldowns
    cooldown_after_loss_sec,
    cooldown_after_win_sec,

    -- Mode
    mode,

    -- GTC (not used in Nizar — IOC only)
    gtc_enabled,

    -- Nizar mode: bypass ROI/directional guards on gap closure exit
    nizar_mode_enabled
) VALUES (
    'PENGUUSDT',                    -- symbol
    'sniper',                        -- strategy_type (sniper engine, params nizar-tuned)
    'lead_lag,trade_flow,wall',     -- disabled_detectors: ONLY static_gap
    '',                              -- detector_strategy: empty

    -- Entry filters: very permissive — static_gap detector handles all gating
    0.30,                            -- min_confidence (low; static_gap conf=0.5+ at min_gap)
    0.005,                           -- min_mexc_lag_pct (0.005% ≈ 0.5 tick — passes when mid_gap >= 1t)
    0.30,                            -- max_mexc_lag_pct (allow up to 0.3% — his data shows up to 0.15%)

    -- IOC: slippage tolerance ~2 ticks. PENGU at $0.0107, 2 ticks ≈ 1.87 bps.
    -- Set ioc_offset_bps=2.0 (≈2.1 ticks at typical price). NOTE: this is
    -- BPS-BASED in current executor — at extreme price moves (PENGU 2x or 0.5x)
    -- the tick equivalent will change. For PENGU's typical $0.005-$0.020 range
    -- this stays within 1-4 ticks.
    2.0,                             -- ioc_offset_bps
    2,                               -- ioc_max_attempts
    50,                              -- ioc_attempt_interval_ms

    -- Position sizing: friend's median qty = 6000 contracts × 10 PENGU/contract
    -- × $0.0107 = $642 notional. Start CONSERVATIVE: $1250 notional ≈ 11K contracts.
    -- (Slightly above his median to compensate for our slower latency.)
    25.0,                            -- margin_usdt (legacy)
    23.0,                            -- margin_min_usdt
    27.0,                            -- margin_max_usdt
    50,                              -- leverage (legacy)
    50,                              -- leverage_min
    60,                              -- leverage_max

    -- Exit logic — Nizar parameters
    -50.0,                           -- stop_loss_roi_pct: effectively disabled (parachute only,
                                     --                    -50% ROI = liquidation territory).
                                     --                    SL via tick-based stop_loss_ticks.
    3,                               -- stop_loss_ticks: 3 ticks adverse = exit
                                     --                  (matches his MAE p75 = -3t, median -2t)
    999.0,                           -- take_profit_roi_pct: disabled (gap closure handles TP)
    999.0,                           -- trailing_activation_roi_pct: disabled (he has no trailing)
    999.0,                           -- trailing_distance_roi_pct: disabled
    30,                              -- max_hold_sec: safety net (his p99 duration ~22s)
    0.5,                             -- min_hold_sec_for_exits: 500ms entry stabilization
    0.5,                             -- sl_grace_sec: 500ms tolerance for MEXC catch-up noise

    -- Quick scalp: DISABLED — friend has no quick TP rule
    0,                               -- quick_scalp_enabled
    5,                               -- quick_scalp_window_sec (irrelevant)
    2.5,                             -- quick_scalp_roi_pct (irrelevant)

    -- MEXC catch-up: PRIMARY exit. Threshold must match actual gap-closure
    -- behavior. Math: 1 tick on PENGU = 0.000001 / 0.010670 × 100 ≈ 0.0094%.
    -- His exit pattern: median exit_gap=0t, p75=0t (87% exits at gap=0).
    -- Setting threshold to 0.005% ≈ 0.53 ticks captures gap≤0.5t (i.e. gap
    -- has crossed through zero). Includes the median exit moment.
    1,                               -- exit_on_mexc_caught_up
    0.005,                           -- mexc_caught_up_threshold_pct (≈0.5 tick on PENGU)
    0,                               -- exit_on_reverse_signal: not part of his strategy

    -- Cooldowns: short, friend trades 43/hour ≈ 1 every 84 seconds
    5,                               -- cooldown_after_loss_sec
    3,                               -- cooldown_after_win_sec

    -- Mode: SHADOW first! Verify behavior matches expectation before live.
    'shadow',

    -- GTC: friend uses IOC (taker), not GTC (maker)
    0,

    -- Nizar mode: ENABLED. Bypasses ROI ≥ -0.5% and directional guards on
    -- gap closure exit. Friend exits at gap=0 regardless of PnL or MEXC
    -- direction (87% of his exits are at gap=0; even his losses exit there).
    -- Without this flag, bot would hold past gap closure waiting for guards
    -- to pass, missing the clean exit and riding drawdowns.
    1
);

-- ──────────────────────────────────────────────────────────────────
-- ADAPTIVE EXIT (v3 patch, 2026-05-09)
-- Replace fixed gap_closure exit with momentum-based exit:
--   * "stalled":  no new favorable peak for adaptive_stall_ms
--   * "reversal": price pulled back from peak by adaptive_reversal_ticks
-- Live-tunable via env: NIZAR_PENGUUSDT_ADAPTIVE_STALL_MS,
--                        NIZAR_PENGUUSDT_ADAPTIVE_REVERSAL_TICKS,
--                        NIZAR_PENGUUSDT_ADAPTIVE_MIN_HOLD_MS
-- (or NIZAR_ADAPTIVE_* for all adaptive-enabled pairs).
-- ──────────────────────────────────────────────────────────────────
UPDATE pair_configs SET
    adaptive_exit_enabled    = 1,    -- enable momentum-based exit
    adaptive_stall_ms        = 2000, -- exit if no new peak for 2s
    adaptive_reversal_ticks  = 1.5,  -- exit if price pulls back 1.5 ticks from peak
    adaptive_min_hold_ms     = 1000  -- give 1s to settle after entry
WHERE symbol = 'PENGUUSDT';

-- Verify
SELECT
    symbol,
    strategy_type,
    disabled_detectors,
    stop_loss_ticks,
    quick_scalp_enabled,
    exit_on_mexc_caught_up,
    mexc_caught_up_threshold_pct,
    nizar_mode_enabled,
    adaptive_exit_enabled,
    adaptive_stall_ms,
    adaptive_reversal_ticks,
    adaptive_min_hold_ms,
    max_hold_sec,
    min_hold_sec_for_exits,
    mode
FROM pair_configs
WHERE symbol = 'PENGUUSDT';
