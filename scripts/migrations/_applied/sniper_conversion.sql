-- =====================================================================
-- Sniper conversion — drop scalper, all pairs use sniper or nizar_micro
-- =====================================================================
-- Apply on a running bot (no restart required for pair_configs;
-- ShadowEngine reloads pair_configs every 60s).
--
-- Rationale:
--   scalper preset is being retired. sniper subsumes its behavior:
--     - QS catches fast moves (= scalper's TP at 0.5%)
--     - Trailing TP captures larger moves scalper would miss
--     - SL -1.5% (vs scalper's -0.5%) gives more room for catch-up
--
-- Pairs being converted: ZEC, TAO, BCH, ASTER, ENA
--   (LINK already on sniper)
--
-- ENA special case:
--   ENA's strategy_type='scalper' was a fallback. In practice ENA only
--   receives nizar_scalp signals (lead_lag/trade_flow/wall disabled),
--   which route to nizar_micro preset via detector_strategy.
--   Setting ENA to sniper as the fallback default is safe — no native
--   detector signals will arrive there anyway.
-- =====================================================================

BEGIN TRANSACTION;

-- Convert all 'scalper' pairs to 'sniper'
UPDATE pair_configs SET
  strategy_type = 'sniper'
WHERE strategy_type = 'scalper';

-- Sanity check
SELECT '-- Strategy types after conversion --' AS msg;
SELECT symbol, strategy_type, disabled_detectors, detector_strategy,
       stop_loss_roi_pct, min_hold_sec_for_exits, exit_on_mexc_caught_up
  FROM pair_configs
 WHERE symbol IN ('ZECUSDT','TAOUSDT','BCHUSDT','ASTERUSDT','LINKUSDT','ENAUSDT')
 ORDER BY symbol;

-- Verify no remaining 'scalper' anywhere
SELECT '-- Remaining scalper rows (should be empty) --' AS msg;
SELECT COUNT(*) AS scalper_count FROM pair_configs WHERE strategy_type = 'scalper';

COMMIT;
