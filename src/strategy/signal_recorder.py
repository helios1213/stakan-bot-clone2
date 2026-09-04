"""Log-only entry-feature recorder for offline signal-quality discovery.

Hypothesis under test: PEPE's problem is the ENTRY, not execution. To learn
the winning entry signature we record EVERY candidate (the full gap space,
INCLUDING the ones the live gate rejects) with a rich feature vector AND the
FORWARD MEXC-mid path (MFE / MAE / fixed-horizon returns). The label is the
forward return — computed for ALL candidates without ever placing an order, so
fill-rate selection bias is removed and "impossible" (rejected) signals are
labelled too.

Env SIGNAL_RECORDER=1 to enable. Zero orders, zero execution. Writes to the
signal_features table; analysed offline.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Forward-return snapshot horizons (ms) + the window after which a candidate is
# finalised and flushed.
_HORIZONS = [500, 1000, 2000, 3000, 5000, 10000]
_MAX_HORIZON_MS = 10000
_FILL_LATENCY_MS = 150   # our IOC submit floor (~143-160ms); touch must survive this to fill

_COLS = (
    "ts,symbol,direction,gap_ticks,long_gap_ticks,short_gap_ticks,mid_gap_bps,"
    "passes_gate,binance_impulse_bps,mexc_impulse_bps,spread_bps,book_imbalance,"
    "gap_age_ms,hour,entry_mid,entry_exec,touch_survival_ms,fillable,"
    "mfe_bps,mae_bps,time_to_mfe_ms,"
    "ret_500ms,ret_1s,ret_2s,ret_3s,ret_5s,ret_10s,"
    # rejected_by: яке саме гейт-правило відкинуло цей сигнал (NULL = пройшов,
    # або рядок старіший за цю колонку). passes_gate сам по собі знає лише про
    # min_ticks, бо пишеться до решти воріт — див. mark_rejected().
    # exec_ticks: перевага НЕТТО обох спредів, детектор рахував і не зберігав.
    "rejected_by,exec_ticks,binance_signal_age_ms"
)
_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS signal_features (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, symbol TEXT, direction TEXT,
  gap_ticks REAL, long_gap_ticks REAL, short_gap_ticks REAL, mid_gap_bps REAL,
  passes_gate INTEGER, binance_impulse_bps REAL, mexc_impulse_bps REAL, spread_bps REAL,
  book_imbalance REAL, gap_age_ms REAL, hour INTEGER, entry_mid REAL, entry_exec REAL,
  touch_survival_ms REAL, fillable INTEGER,
  mfe_bps REAL, mae_bps REAL, time_to_mfe_ms REAL,
  ret_500ms REAL, ret_1s REAL, ret_2s REAL, ret_3s REAL, ret_5s REAL, ret_10s REAL,
  rejected_by TEXT, exec_ticks REAL, binance_signal_age_ms REAL
)
"""
_INSERT_SQL = f"INSERT INTO signal_features ({_COLS}) VALUES ({','.join('?' * 30)})"


class SignalRecorder:
    """Records candidate entry features + forward MEXC-mid returns. Log-only.

    record()/on_tick() are SYNC (memory only, hot-path safe). flush() is async
    (batched DB write). Gated by `enabled` — fully inert when off.
    """

    def __init__(self, db, enabled: bool, record_min_ticks: float = 1.0,
                 dedup_ms: int = 800, writebuf_cap: int = 20_000) -> None:
        self.db = db
        self.enabled = bool(enabled)
        self.record_min_ticks = record_min_ticks
        self.dedup_ms = dedup_ms
        self._cap = writebuf_cap
        # pending[symbol][id] = forward-accumulator dict
        self._pending: dict[str, dict[int, dict]] = {}
        self._next_id = 0
        self._last_record_ms: dict[tuple, int] = {}   # (symbol,dir) -> ms
        # symbol -> (cid, ts_ms) останнього записаного кандидата. Потрібне, щоб
        # ворота, які спрацьовують ПІСЛЯ record(), могли позначити саме той
        # рядок. Звірка по ts_ms точна: якщо record() пропустив через dedup,
        # ts не збігається і ми не зіпсуємо старіший рядок.
        self._last_rec: dict[str, tuple[int, int]] = {}
        self._writebuf: list[tuple] = []
        self.recorded = 0
        self.finalized = 0

    async def ensure_table(self) -> None:
        if self.enabled:
            await self.db.execute(_CREATE_SQL)
            # Index on ts so the retention prune is an index range-scan, not a
            # full table scan (the table is the biggest in the DB). (2026-08-13.)
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_signal_features_ts "
                "ON signal_features(ts)")
            # ADD COLUMN у sqlite — операція над схемою, таблицю не переписує,
            # тому це дешево навіть на ~26 млн рядків.
            for _col, _typ in (("rejected_by", "TEXT"), ("exec_ticks", "REAL"),
                               ("binance_signal_age_ms", "REAL")):
                try:
                    await self.db.execute(
                        f"ALTER TABLE signal_features ADD COLUMN {_col} {_typ}")
                    logger.info("[SIGNAL_RECORDER] додано колонку %s", _col)
                except Exception:
                    pass          # вже існує
            logger.info("[SIGNAL_RECORDER] enabled (record_min=%.1ft, horizons=%s)",
                        self.record_min_ticks, _HORIZONS)

    def on_tick(self, symbol: str, now_ms: int, m_mid: float,
                m_ask: float, m_bid: float) -> None:
        """Update forward accumulators + at-touch fillability for this symbol."""
        if not self.enabled:
            return
        bucket = self._pending.get(symbol)
        if not bucket or m_mid <= 0:
            return
        done = []
        for cid, p in bucket.items():
            sign = 1.0 if p["direction"] == "long" else -1.0
            fav = sign * (m_mid - p["entry_mid"]) / p["entry_mid"] * 1e4  # in-dir bps
            age = now_ms - p["ts"]
            if fav > p["mfe"]:
                p["mfe"] = fav; p["t_mfe"] = age
            if fav < p["mae"]:
                p["mae"] = fav
            # FILLABILITY: time until the at-touch limit breaches (= our IOC
            # would no longer fill). long breach = ask rose above entry_exec
            # (price up = thesis won BUT we miss at offset=0); short = bid fell.
            if p["touch_survival"] is None:
                ex = p["entry_exec"]
                breached = (m_ask > ex) if p["direction"] == "long" else (m_bid < ex)
                if breached:
                    p["touch_survival"] = age
            rets = p["rets"]
            for h in _HORIZONS:
                if rets[h] is None and age >= h:
                    rets[h] = fav
            if age >= _MAX_HORIZON_MS:
                done.append(cid)
        for cid in done:
            self._finalize(bucket.pop(cid))
        if not bucket:
            self._pending.pop(symbol, None)

    def record(self, symbol: str, direction: str, now_ms: int,
               entry_mid: float, feat: dict) -> None:
        """Register a candidate (full space) + start forward tracking."""
        if not self.enabled or entry_mid <= 0:
            return
        key = (symbol, direction)
        if now_ms - self._last_record_ms.get(key, 0) < self.dedup_ms:
            return
        self._last_record_ms[key] = now_ms
        cid = self._next_id
        self._next_id += 1
        self._last_rec[symbol] = (cid, now_ms)
        self._pending.setdefault(symbol, {})[cid] = {
            "symbol": symbol, "direction": direction, "ts": now_ms,
            "entry_mid": entry_mid, "entry_exec": feat["entry_exec"],
            "mfe": 0.0, "mae": 0.0, "t_mfe": 0.0, "touch_survival": None,
            "rets": {h: None for h in _HORIZONS}, "feat": feat,
        }
        self.recorded += 1

    def mark_rejected(self, symbol: str, ts_ms: int, reason: str,
                      clears_gate: bool = True) -> None:
        """Позначити щойно записаного кандидата як відкинутого воротами.

        Викликається з детектора на кожному gate-return. Рядок ще не в БД
        (пишеться в _finalize через 10с), тож це просто правка dict.

        clears_gate=False для кулдауну: сигнал ворота ПРОЙШОВ, його прибрав
        анти-дублікатний захист — змішувати ці дві причини не можна.
        """
        if not self.enabled:
            return
        last = self._last_rec.get(symbol)
        if last is None or last[1] != ts_ms:
            return                        # dedup пропустив запис — нічого правити
        p = self._pending.get(symbol, {}).get(last[0])
        if p is None:
            return
        f = p["feat"]
        if f.get("rejected_by") is None:  # перша причина — найточніша
            f["rejected_by"] = reason
            if clears_gate:
                f["passes_gate"] = 0

    def _finalize(self, p: dict) -> None:
        f = p["feat"]; r = p["rets"]
        ts = p["touch_survival"]
        if ts is None:                       # never breached over the window = fully fillable
            ts = float(_MAX_HORIZON_MS)
        fillable = 1 if ts >= _FILL_LATENCY_MS else 0
        self._writebuf.append((
            p["ts"], p["symbol"], p["direction"],
            f["gap_ticks"], f["long_gap"], f["short_gap"], f["mid_gap_bps"],
            f["passes_gate"], f["bin_impulse"], f["mexc_impulse"], f["spread_bps"],
            f["imbalance"], f["gap_age_ms"], f["hour"], p["entry_mid"], f["entry_exec"],
            round(ts, 1), fillable,
            round(p["mfe"], 3), round(p["mae"], 3), p["t_mfe"],
            r[500], r[1000], r[2000], r[3000], r[5000], r[10000],
            f.get("rejected_by"), f.get("exec_ticks"),
            f.get("binance_signal_age_ms"),
        ))
        self.finalized += 1
        if len(self._writebuf) > self._cap:    # safety: never grow unbounded
            self._writebuf = self._writebuf[-self._cap:]

    async def flush(self) -> None:
        if not self._writebuf:
            return
        rows, self._writebuf = self._writebuf, []
        try:
            await self.db.executemany(_INSERT_SQL, rows)
        except Exception as e:
            logger.warning("[SIGNAL_RECORDER] flush failed (%d rows): %s", len(rows), e)
