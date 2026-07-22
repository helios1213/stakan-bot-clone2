-- =====================================================================
-- Option Y migration — per-pair-per-detector strategy tuning
-- =====================================================================
-- Apply on a running bot (no restart required for pair_configs;
-- ShadowEngine reloads pair_configs every 60s by default).
--
-- Decisions are based on shadow_trades data analysis (~7200 trades):
--
-- Whitelist (5 pairs with proven edge):
--   ZECUSDT   — both detectors profitable: trade_flow +$66, lead_lag +$37
--   TAOUSDT   — both: lead_lag +$25, trade_flow +$20
--   BCHUSDT   — both: trade_flow +$12, lead_lag +$8
--   ASTERUSDT — lead_lag-only: +$12 (trade_flow loses -$4)
--   LINKUSDT  — trade_flow-only: +$54 (lead_lag breaks even)
--
-- Blacklist (5 pairs killing PnL):
--   DOGEUSDT  — trade_flow -$193, lead_lag -$23 (catastrophic)
--   SOLUSDT   — trade_flow -$134
--   ENAUSDT   — both negative (kept open for future Nizar-detector)
--   XRPUSDT   — net -$29
--   ADAUSDT   — net -$46
--
-- Parameter tuning rationale:
--   stop_loss_roi_pct: -1.5 → -2.0  (SL bleeding $232 on ZEC alone)
--   quick_scalp_window_sec: 5 → 10  (capture more flash moves)
--   quick_scalp_roi_pct: 2.5 → 1.0  (accept smaller fast wins)
--   mexc_caught_up_threshold_pct: 0.005 → 0.025
--     (caught_up exit closing in zero before move develops)
-- =====================================================================

BEGIN TRANSACTION;

-- ========== WHITELIST (5 pairs) ==========

-- ZECUSDT: edge on both detectors
UPDATE pair_configs SET
  stop_loss_roi_pct = -2.0,
  quick_scalp_window_sec = 10,
  quick_scalp_roi_pct = 1.0,
  mexc_caught_up_threshold_pct = 0.025,
  disabled_detectors = ''
WHERE symbol = 'ZECUSDT';

-- TAOUSDT: edge on both detectors
UPDATE pair_configs SET
  stop_loss_roi_pct = -2.0,
  quick_scalp_window_sec = 10,
  quick_scalp_roi_pct = 1.0,
  mexc_caught_up_threshold_pct = 0.025,
  disabled_detectors = ''
WHERE symbol = 'TAOUSDT';

-- BCHUSDT: edge on both detectors
UPDATE pair_configs SET
  stop_loss_roi_pct = -2.0,
  quick_scalp_window_sec = 10,
  quick_scalp_roi_pct = 1.0,
  mexc_caught_up_threshold_pct = 0.025,
  disabled_detectors = ''
WHERE symbol = 'BCHUSDT';

-- ASTERUSDT: only lead_lag profits, disable trade_flow + wall
UPDATE pair_configs SET
  stop_loss_roi_pct = -2.0,
  quick_scalp_window_sec = 10,
  quick_scalp_roi_pct = 1.0,
  mexc_caught_up_threshold_pct = 0.025,
  disabled_detectors = 'trade_flow,wall'
WHERE symbol = 'ASTERUSDT';

-- LINKUSDT: only trade_flow profits, disable lead_lag + wall
UPDATE pair_configs SET
  stop_loss_roi_pct = -2.0,
  quick_scalp_window_sec = 10,
  quick_scalp_roi_pct = 1.0,
  mexc_caught_up_threshold_pct = 0.025,
  disabled_detectors = 'lead_lag,wall'
WHERE symbol = 'LINKUSDT';

-- ========== BLACKLIST (disable native detectors on losers) ==========
-- ENAUSDT: native detectors disabled, but nizar_scalp routes to nizar_micro
-- preset (sub-experiment for the Nizar-style trading approach).
-- DOGE/SOL/XRP/ADA: all detectors disabled (universal losers).

UPDATE pair_configs SET
  disabled_detectors = 'lead_lag,trade_flow,wall',
  detector_strategy = '{"nizar_scalp": "nizar_micro"}'
WHERE symbol = 'ENAUSDT';

UPDATE pair_configs SET
  disabled_detectors = 'lead_lag,trade_flow,wall'
WHERE symbol IN ('DOGEUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT');

-- ========== Sanity check ==========
-- (these SELECTs print to console but don't fail the migration)
SELECT '-- WHITELIST after update --' AS msg;
SELECT symbol, stop_loss_roi_pct, quick_scalp_window_sec, quick_scalp_roi_pct,
       mexc_caught_up_threshold_pct, disabled_detectors
  FROM pair_configs
 WHERE symbol IN ('ZECUSDT','TAOUSDT','BCHUSDT','ASTERUSDT','LINKUSDT');

SELECT '-- BLACKLIST after update --' AS msg;
SELECT symbol, disabled_detectors
  FROM pair_configs
 WHERE symbol IN ('DOGEUSDT','SOLUSDT','ENAUSDT','XRPUSDT','ADAUSDT');

COMMIT;
