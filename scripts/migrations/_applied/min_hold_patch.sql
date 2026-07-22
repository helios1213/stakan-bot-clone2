-- =====================================================================
-- Min-hold patch — fix "open→100ms→exit" pattern
-- =====================================================================
-- Apply on a running bot. Bot reloads pair_configs every 60s.
--
-- Rationale (from observation):
--   Current behavior: trades open → 100ms → close at ROI≈0 via mexc_caught_up.
--   The watcher tick fires before the catch-up move develops profit.
--
-- Goal: profit FROM the catch-up process (5-45s), not after it completes.
--
-- Two changes per pair:
--   1. min_hold_sec_for_exits — block TP/QS/CU/trailing exits during first N seconds
--      (SL and time_limit always work — catastrophic protection retained)
--   2. exit_on_mexc_caught_up = 0 — disable CU exit entirely; let TP/QS/time_limit handle it
--      (rationale: catch-up IS the profit event, not exit signal)
-- =====================================================================

BEGIN TRANSACTION;

-- ========== WHITELIST: enable min_hold + disable CU exit ==========

-- ZECUSDT: both detectors profitable, allow catch-up move to develop
UPDATE pair_configs SET
  min_hold_sec_for_exits = 2.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'ZECUSDT';

UPDATE pair_configs SET
  min_hold_sec_for_exits = 2.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'TAOUSDT';

UPDATE pair_configs SET
  min_hold_sec_for_exits = 2.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'BCHUSDT';

UPDATE pair_configs SET
  min_hold_sec_for_exits = 2.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'ASTERUSDT';

UPDATE pair_configs SET
  min_hold_sec_for_exits = 2.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'LINKUSDT';

-- ========== ENA: tighter min_hold for nizar_micro routing ==========
-- ENA uses scalper strategy (default for the pair) but nizar_scalp signals
-- get routed to nizar_micro preset which has its own min_hold=3s baked in.
-- This UPDATE provides a fallback at pair-config level.
UPDATE pair_configs SET
  min_hold_sec_for_exits = 3.0,
  exit_on_mexc_caught_up = 0
WHERE symbol = 'ENAUSDT';

-- ========== Sanity check ==========
SELECT '-- min_hold_sec_for_exits + exit_on_mexc_caught_up after update --' AS msg;
SELECT symbol, strategy_type, min_hold_sec_for_exits, exit_on_mexc_caught_up,
       stop_loss_roi_pct, quick_scalp_window_sec, mexc_caught_up_threshold_pct
  FROM pair_configs
 WHERE symbol IN ('ZECUSDT','TAOUSDT','BCHUSDT','ASTERUSDT','LINKUSDT','ENAUSDT')
 ORDER BY symbol;

COMMIT;
