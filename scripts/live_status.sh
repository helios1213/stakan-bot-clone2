#!/bin/bash
# Show live trading status. Usage: bash live_status.sh

DB=/root/stakan-bot/data/stakan.db
LIVE_DB=/root/stakan-bot/data/stakan-live.db

echo "=== LIVE TRADING STATUS ==="
echo ""
echo "Pair modes (mode='live' = trading real money):"
sqlite3 -header -column "$DB" "
SELECT symbol, mode, margin_min_usdt AS m_min, margin_max_usdt AS m_max,
       leverage_min AS l_min, leverage_max AS l_max,
       stop_loss_roi_pct AS sl, max_hold_sec AS hold
  FROM pair_configs
 WHERE symbol IN (SELECT symbol FROM live_pair_whitelist)
 ORDER BY mode DESC, symbol;"

echo ""
echo "Slot assignments:"
sqlite3 -header -column "$DB" "
SELECT slot_id, assigned_pair AS pair, live_enabled AS live,
       last_balance_usdt AS balance,
       last_latency_ms AS lat
  FROM webkey_slots
 WHERE enabled=1
 ORDER BY slot_id;"

echo ""
echo "Today's live trades:"
sqlite3 -header -column "$LIVE_DB" "
SELECT datetime(opened_at,'unixepoch','localtime') AS opened,
       symbol AS pair, direction AS dir, leverage AS lev,
       ROUND(net_pnl_usdt,3) AS pnl,
       exit_reason AS reason,
       duration_sec AS dur_s
  FROM live_trades
 WHERE opened_at >= strftime('%s', date('now', 'start of day'))
 ORDER BY opened_at DESC LIMIT 20;" 2>/dev/null

echo ""
echo "Today's live PnL:"
sqlite3 "$LIVE_DB" "
SELECT 'Total: ' || COUNT(*) || ' trades | PnL $' || ROUND(COALESCE(SUM(net_pnl_usdt),0),3)
  FROM live_trades
 WHERE opened_at >= strftime('%s', date('now', 'start of day')) AND closed_at IS NOT NULL;
" 2>/dev/null

echo ""
echo "Open positions:"
sqlite3 -header -column "$LIVE_DB" "
SELECT id, symbol, direction, leverage, ROUND(margin_usdt,2) AS margin,
       datetime(opened_at,'unixepoch','localtime') AS opened
  FROM live_trades WHERE closed_at IS NULL;" 2>/dev/null
