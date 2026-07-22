-- =====================================================================
-- Whitelist expansion — add PENGU, 1000PEPE, HYPE + LINK/ASTER tweaks
-- =====================================================================
-- Apply on a running bot (no restart required for pair_configs;
-- ShadowEngine reloads pair_configs every 60s).
--
-- Changes:
--
-- 1. LINKUSDT — add lead_lag + wall detectors (was: trade_flow only)
--    Why: original disabling was based on -$0 lead_lag PnL, but small sample.
--    Worth re-testing with min_hold patch active.
--
-- 2. ASTERUSDT — add trade_flow + wall (was: lead_lag only)
--    Why: original trade_flow was -$4, marginal. Re-test with new logic.
--
-- 3. PENGUUSDT, 1000PEPEUSDT, HYPEUSDT — new pairs, sniper strategy,
--    all native detectors enabled.
-- =====================================================================

BEGIN TRANSACTION;

-- ========== 1. LINKUSDT: enable lead_lag + wall ==========
-- Was: disabled_detectors = 'lead_lag,wall' → only trade_flow active
-- Now: all native detectors enabled
UPDATE pair_configs SET
  disabled_detectors = ''
WHERE symbol = 'LINKUSDT';

-- ========== 2. ASTERUSDT: enable trade_flow + wall ==========
-- Was: disabled_detectors = 'trade_flow,wall' → only lead_lag active
-- Now: all native detectors enabled
UPDATE pair_configs SET
  disabled_detectors = ''
WHERE symbol = 'ASTERUSDT';

-- ========== 3. New pairs: PENGUUSDT, 1000PEPEUSDT, HYPEUSDT ==========
-- These rows might not exist yet (PairStateManager creates them lazily
-- on first signal). Use INSERT OR IGNORE + UPDATE pattern.
--
-- Strategy: sniper (TP via trailing, QS for fast moves)
-- min_hold: 2.0s (consistent with whitelist)
-- exit_on_mexc_caught_up: 0 (catch-up IS the profit, not exit)
-- All native detectors enabled

INSERT OR IGNORE INTO pair_configs (symbol, strategy_type, min_confidence)
VALUES ('PENGUUSDT', 'sniper', 0.3),
       ('1000PEPEUSDT', 'sniper', 0.3),
       ('HYPEUSDT', 'sniper', 0.3);

UPDATE pair_configs SET
  strategy_type = 'sniper',
  stop_loss_roi_pct = -1.5,
  take_profit_roi_pct = 999.0,
  trailing_activation_roi_pct = 0.5,
  trailing_distance_roi_pct = 0.3,
  quick_scalp_enabled = 1,
  quick_scalp_window_sec = 3,
  quick_scalp_roi_pct = 1.5,
  max_hold_sec = 60,
  mexc_caught_up_threshold_pct = 0.025,
  exit_on_mexc_caught_up = 0,
  min_hold_sec_for_exits = 2.0,
  disabled_detectors = '',
  detector_strategy = ''
WHERE symbol IN ('PENGUUSDT', '1000PEPEUSDT', 'HYPEUSDT');

-- ========== Sanity check ==========
SELECT '-- Updated pair_configs --' AS msg;
SELECT symbol, strategy_type, disabled_detectors,
       stop_loss_roi_pct, quick_scalp_window_sec,
       min_hold_sec_for_exits, exit_on_mexc_caught_up
  FROM pair_configs
 WHERE symbol IN ('LINKUSDT','ASTERUSDT','PENGUUSDT','1000PEPEUSDT','HYPEUSDT')
 ORDER BY symbol;

COMMIT;
