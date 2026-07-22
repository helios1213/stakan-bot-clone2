-- =====================================================================
-- Migration: scanner_removal
-- Date:      2026-05-16
-- Purpose:   May 2026 scanner-removal refactor.
--
--   1. The pair scanner has been deleted from the codebase. Pairs are now
--      seeded into pair_states directly as SHADOW from the universe
--      whitelist in config.yaml — there is no more `discovered` lifecycle.
--
--   2. Any existing rows in `state='discovered'` are migrated to `shadow`
--      so the bot can immediately start shadow-trading them. If you DON'T
--      want a stale discovered pair to start trading, manually pause it
--      via Telegram /menu after the migration.
--
-- This migration is idempotent — re-running it is safe.
-- =====================================================================

BEGIN TRANSACTION;

-- Show what we're about to migrate (visible in deploy logs)
SELECT 'BEFORE migration: ' || COUNT(*) || ' rows in discovered state'
  FROM pair_states WHERE state = 'discovered';

-- Migrate: discovered → shadow.
-- For each migrated row:
--   - state          → 'shadow'
--   - state_since    → now (so the bot doesn't think it's been in shadow forever)
--   - shadow_started_at → now (only if NULL; preserve real history if present)
--   - updated_at     → now
--   - last_state_change_reason → audit trail
UPDATE pair_states
   SET state = 'shadow',
       state_since = strftime('%s','now'),
       shadow_started_at = COALESCE(shadow_started_at, CAST(strftime('%s','now') AS INTEGER)),
       updated_at = CAST(strftime('%s','now') AS INTEGER),
       last_state_change_reason = COALESCE(last_state_change_reason || '; ', '')
                                  || 'auto-migrated from discovered (scanner-removal refactor 2026-05-16)'
 WHERE state = 'discovered';

-- Show what was migrated
SELECT 'AFTER migration: ' || COUNT(*) || ' rows still in discovered (should be 0)'
  FROM pair_states WHERE state = 'discovered';

SELECT 'Current shadow count: ' || COUNT(*) FROM pair_states WHERE state = 'shadow';
SELECT 'Current live count:   ' || COUNT(*) FROM pair_states WHERE state = 'live';

COMMIT;
