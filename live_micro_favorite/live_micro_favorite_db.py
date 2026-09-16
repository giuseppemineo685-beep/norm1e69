"""
Base de datos del ejecutor micro-LIVE FAVORITE_BASELINE:
live_micro_favorite/live_micro_favorite.db. Archivo completamente nuevo y
separado de data.db, paper_validation.db, y del live_micro.db del ejecutor
momentum -- ningun archivo comparte estado con otro.
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "live_micro_favorite.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_micro_favorite_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_micro_favorite_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS live_micro_favorite_run_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL,
    n_orders_attempted INTEGER NOT NULL DEFAULT 0,
    n_orders_filled_or_partial INTEGER NOT NULL DEFAULT 0,
    n_orders_failed INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    capital_deployed_usd REAL NOT NULL DEFAULT 0.0,
    realized_pnl_usd REAL NOT NULL DEFAULT 0.0,
    kill_switch_tripped INTEGER NOT NULL DEFAULT 0,
    kill_switch_reason TEXT,
    kill_switch_ts REAL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Paper y LIVE registrados en paralelo: paper_* son la decision congelada
-- POLYMARKET_FAVORITE_BASELINE (decide_favorite, misma funcion que el
-- paper validator), las demas columnas son el intento DRY_RUN/LIVE de ESTE
-- ejecutor a escala $2 -- ambas siempre en la MISMA fila, para comparacion directa.
CREATE TABLE IF NOT EXISTS live_micro_favorite_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    mode TEXT NOT NULL,                    -- DRY_RUN | LIVE
    condition_id TEXT NOT NULL,
    asset_symbol TEXT,
    decision_time_utc REAL,
    paper_side TEXT,                       -- lado que elige decide_favorite() (favorito)
    paper_expected_price REAL,             -- executable_price de decide_favorite a stake $10
    paper_fill_status TEXT,                -- FULL | PARTIAL | SKIPPED | NO_TRADE del calculo paper
    side TEXT,                             -- lado del intento DRY_RUN/LIVE (== paper_side si hay intento)
    token_id TEXT,
    depth_json TEXT,
    snapshot_id INTEGER,
    snapshot_received_at_utc_ms INTEGER,
    snapshot_age_s REAL,
    min_order_size_shares REAL,
    limit_price REAL,
    quantity_shares REAL,
    quantity_usd REAL,
    api_request_json TEXT,
    api_response_json TEXT,
    fill_status TEXT,                      -- FILLED | PARTIAL | REJECTED | ERROR | SKIPPED_STALE_SNAPSHOT | SKIPPED_SIZE_INFEASIBLE | SKIPPED_KILL_SWITCH | SIMULATED_DRY_RUN | NO_PAPER_SIGNAL
    shares_filled REAL,
    avg_fill_price REAL,
    fee_real_usd REAL,                     -- SOLO LIVE, si la API lo informa. NULL en DRY_RUN -- nunca inventado.
    fee_estimated_usd REAL,                -- DRY_RUN: estimacion con la formula verificada (0.07 * shares * price * (1-price),
                                            -- ver collector/export_paper_validation.py) -- rotulada ESTIMADA, no real.
    latency_ms REAL,
    slippage_pct REAL,
    tx_or_order_id TEXT,
    reject_reason TEXT,
    error TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    winner TEXT,
    payout_usd REAL,
    pnl_usd REAL,
    resolved_at_utc REAL
);

CREATE INDEX IF NOT EXISTS idx_live_micro_favorite_attempts_condition
    ON live_micro_favorite_attempts(condition_id);
"""

_conn = None


def connect():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def reset_connection():
    global _conn
    if _conn is not None:
        _conn.close()
    _conn = None


def init_db(mode="DRY_RUN"):
    conn = connect()
    conn.executescript(_SCHEMA)
    now = time.time()
    conn.execute("""INSERT OR IGNORE INTO live_micro_favorite_run_state
        (id, mode, started_at, updated_at) VALUES (1, ?, ?, ?)""", (mode, now, now))
    conn.commit()


def get_meta(key, default=None):
    conn = connect()
    row = conn.execute("SELECT value FROM live_micro_favorite_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta_if_absent(key, value):
    conn = connect()
    conn.execute("INSERT OR IGNORE INTO live_micro_favorite_meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    return get_meta(key)


def log_event(component, event_type, detail=None):
    conn = connect()
    conn.execute("INSERT INTO live_micro_favorite_events (ts, component, event_type, detail_json) VALUES (?,?,?,?)",
                 (time.time(), component, event_type, json.dumps(detail, default=str) if detail else None))
    conn.commit()


def load_run_state():
    conn = connect()
    row = conn.execute("SELECT * FROM live_micro_favorite_run_state WHERE id=1").fetchone()
    return dict(row) if row else None


def save_run_state(state_dict):
    conn = connect()
    conn.execute("""UPDATE live_micro_favorite_run_state SET
        n_orders_attempted=?, consecutive_failures=?, capital_deployed_usd=?,
        realized_pnl_usd=?, kill_switch_tripped=?, kill_switch_reason=?,
        kill_switch_ts=?, updated_at=?
        WHERE id=1""",
        (state_dict["n_orders_attempted"], state_dict["consecutive_failures"],
         state_dict["capital_deployed_usd"], state_dict["realized_pnl_usd"],
         1 if state_dict["kill_switch_tripped"] else 0, state_dict.get("kill_switch_reason"),
         time.time() if state_dict["kill_switch_tripped"] else None, time.time()))
    conn.commit()


def mark_resolved(attempt_id, winner, payout_usd, pnl_usd, resolved_at_utc):
    conn = connect()
    conn.execute("""UPDATE live_micro_favorite_attempts SET
        resolved=1, winner=?, payout_usd=?, pnl_usd=?, resolved_at_utc=?
        WHERE id=?""", (winner, payout_usd, pnl_usd, resolved_at_utc, attempt_id))
    conn.commit()


def unresolved_filled_attempts():
    conn = connect()
    return conn.execute("""SELECT * FROM live_micro_favorite_attempts
        WHERE resolved=0 AND fill_status IN ('FILLED','PARTIAL','SIMULATED_DRY_RUN')
          AND side IS NOT NULL AND shares_filled IS NOT NULL AND shares_filled > 0""").fetchall()


def insert_attempt(row):
    conn = connect()
    cols = ["created_at", "mode", "condition_id", "asset_symbol", "decision_time_utc",
            "paper_side", "paper_expected_price", "paper_fill_status", "side",
            "token_id", "depth_json", "snapshot_id", "snapshot_received_at_utc_ms",
            "snapshot_age_s", "min_order_size_shares", "limit_price", "quantity_shares",
            "quantity_usd", "api_request_json", "api_response_json", "fill_status",
            "shares_filled", "avg_fill_price", "fee_real_usd", "fee_estimated_usd", "latency_ms",
            "slippage_pct", "tx_or_order_id", "reject_reason", "error"]
    placeholders = ",".join("?" * len(cols))
    conn.execute(f"INSERT INTO live_micro_favorite_attempts ({','.join(cols)}) VALUES ({placeholders})",
                 tuple(row.get(c) for c in cols))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
