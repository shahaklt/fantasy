"""SQLite tick store for the live sentiment panel.

Quote snapshots are appended, never overwritten, so the panel can compute
microstructure features over any lookback and you keep a permanent record of
what the market looked like when a position was opened -- which is what makes
closing-line value measurable after the fact.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..config import DATA_DIR

DB_PATH = DATA_DIR / "market_ticks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    venue       TEXT    NOT NULL,
    market_id   TEXT    NOT NULL,
    bid         REAL,
    ask         REAL,
    mid         REAL,
    micro_price REAL,
    last        REAL,
    bid_depth   REAL,
    ask_depth   REAL,
    imbalance   REAL,
    ofi         REAL,
    volume      REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_market_ts ON ticks(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    venue        TEXT    NOT NULL,
    market_id    TEXT    NOT NULL,
    title        TEXT,
    model_prob   REAL,
    market_prob  REAL,
    blended_prob REAL,
    edge         REAL,
    kelly        REAL,
    payload      TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    mode          TEXT    NOT NULL,
    venue         TEXT    NOT NULL,
    market_id     TEXT    NOT NULL,
    side          TEXT,
    action        TEXT,
    quantity      REAL,
    price         REAL,
    status        TEXT,
    model_prob    REAL,
    closing_price REAL,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
"""


class TickStore:
    """Thread-safe, append-only store for quotes, signals and trades."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    def record_tick(self, venue: str, market_id: str, features: dict,
                    ts: float | None = None) -> None:
        row = (ts or time.time(), venue, market_id,
               features.get("best_bid"), features.get("best_ask"), features.get("mid"),
               features.get("micro_price"), features.get("last"),
               features.get("bid_depth"), features.get("ask_depth"),
               features.get("imbalance"), features.get("ofi"), features.get("volume"))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO ticks (ts,venue,market_id,bid,ask,mid,micro_price,last,"
                "bid_depth,ask_depth,imbalance,ofi,volume) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row)

    def record_ticks(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO ticks (ts,venue,market_id,bid,ask,mid,micro_price,last,"
                "bid_depth,ask_depth,imbalance,ofi,volume) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)

    def record_signal(self, venue: str, market_id: str, title: str, payload: dict) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO signals (ts,venue,market_id,title,model_prob,market_prob,"
                "blended_prob,edge,kelly,payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), venue, market_id, title, payload.get("model_prob"),
                 payload.get("market_prob"), payload.get("blended_prob"),
                 payload.get("edge"), payload.get("kelly"), json.dumps(payload)))

    def record_trade(self, mode: str, order, model_prob: float | None = None,
                     payload: dict | None = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts,mode,venue,market_id,side,action,quantity,price,"
                "status,model_prob,closing_price,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), mode, order.venue, order.market_id,
                 getattr(order.side, "value", order.side),
                 getattr(order.action, "value", order.action),
                 order.quantity, order.avg_fill_price or order.price, order.status,
                 model_prob, None, json.dumps(payload or {})))

    # ------------------------------------------------------------------- read
    def history(self, market_id: str, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ticks WHERE market_id = ? ORDER BY ts DESC LIMIT ?",
                (market_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_markets(self, minutes: int = 60) -> list[dict]:
        cutoff = time.time() - minutes * 60
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT market_id, venue, COUNT(*) n, MAX(ts) last_ts, "
                "       MIN(mid) min_mid, MAX(mid) max_mid "
                "FROM ticks WHERE ts > ? GROUP BY market_id, venue ORDER BY n DESC",
                (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def signals(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def trades(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def price_series(self, market_id: str, minutes: int = 240) -> list[dict]:
        cutoff = time.time() - minutes * 60
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, mid, micro_price, imbalance, ofi FROM ticks "
                "WHERE market_id = ? AND ts > ? ORDER BY ts", (market_id, cutoff)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, days: int = 30) -> int:
        cutoff = time.time() - days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
            return cur.rowcount
