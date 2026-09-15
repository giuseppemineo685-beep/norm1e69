"""
Base de datos PROPIA y SEPARADA para la validacion forward en paper trading.
Nunca toca collector/data.db (esa se abre SOLO en modo read-only desde
run_paper_validation.py, ver ese modulo). Este archivo no importa nada de
polymarket_api, run_collector, scripts/live_trader ni ningun cliente HTTP --
es puro sqlite3 + stdlib.

DB_PATH = collector/paper_validation.db -- archivo nuevo, propio, nunca el
mismo que config.DB_PATH (verificado por un test de seguridad).
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "paper_validation.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_markets (
    condition_id TEXT PRIMARY KEY,
    market_title TEXT,
    asset_symbol TEXT NOT NULL,
    token_up_id TEXT,
    token_down_id TEXT,
    open_time_utc REAL NOT NULL,
    close_time_utc REAL NOT NULL,
    decision_time_utc REAL NOT NULL,      -- open_time_utc + 60
    discovered_at REAL NOT NULL
);

-- Una fila por (mercado, estrategia): la decision tomada a los 60s.
CREATE TABLE IF NOT EXISTS paper_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL REFERENCES paper_markets(condition_id),
    strategy TEXT NOT NULL,               -- MOMENTUM_PURE | MOMENTUM_PARTIAL_HEDGE | POLYMARKET_FAVORITE_BASELINE
    decision_timestamp_utc REAL NOT NULL,
    asset_symbol TEXT,
    underlying_open_price REAL,
    underlying_price_at_60s REAL,
    distance_pct REAL,
    signal_present INTEGER NOT NULL,      -- 0/1 (favorito siempre 1 si hay libro de ambos lados)
    side_chosen TEXT,                     -- 'Up'|'Down'|NULL
    token_id TEXT,
    snapshot_id INTEGER,                  -- fila exacta de orderbook_snapshots usada (en data.db)
    best_ask REAL,
    executable_price REAL,
    shares REAL,
    capital_usd REAL,
    fill_status TEXT NOT NULL,            -- FULL | PARTIAL | SKIPPED | NO_TRADE
    skip_or_partial_reason TEXT,
    created_at REAL NOT NULL,
    UNIQUE (condition_id, strategy)
);
CREATE INDEX IF NOT EXISTS idx_paper_decisions_market ON paper_decisions(condition_id);

-- Solo para MOMENTUM_PARTIAL_HEDGE: cada chequeo de condiciones de cobertura
-- (aunque no se cubra), para trazabilidad completa de por que si/no se cubrio.
CREATE TABLE IF NOT EXISTS paper_hedge_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES paper_decisions(id),
    condition_id TEXT NOT NULL,
    checked_at_utc REAL NOT NULL,
    opposite_token_id TEXT,
    opposite_snapshot_id INTEGER,
    opposite_best_ask REAL,
    opposite_executable_price REAL,
    combined_cost REAL,
    depth_sufficient INTEGER,             -- 0/1: profundidad suficiente para el 25% completo (FULL)
    seconds_to_close REAL,
    cond_price_le_010 INTEGER,
    cond_combined_le_090 INTEGER,
    cond_depth_ok INTEGER,
    cond_time_ok INTEGER,
    all_conditions_met INTEGER NOT NULL,
    hedge_executed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hedge_checks_decision ON paper_hedge_checks(decision_id);

-- La cobertura EJECUTADA (a lo sumo una por decision -- unico, no se vende nunca).
CREATE TABLE IF NOT EXISTS paper_hedge_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES paper_decisions(id) UNIQUE,
    condition_id TEXT NOT NULL,
    executed_at_utc REAL NOT NULL,
    target_shares REAL NOT NULL,          -- 25% de las shares de la 1ra pierna
    filled_shares REAL NOT NULL,
    usd_spent REAL NOT NULL,
    vwap REAL,
    fill_status TEXT NOT NULL,            -- FULL (parcial nunca se ejecuta -- condicion exige profundidad suficiente)
    snapshot_id INTEGER
);

CREATE TABLE IF NOT EXISTS paper_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES paper_decisions(id) UNIQUE,
    condition_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    winner TEXT NOT NULL,
    resolved_at_utc REAL NOT NULL,
    capital_deployed_usd REAL NOT NULL,
    payout_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL,
    roi REAL,
    UNIQUE (condition_id, strategy)
);
CREATE INDEX IF NOT EXISTS idx_resolutions_strategy ON paper_resolutions(strategy);

-- Un solo valor: el timestamp de arranque del validador. Los mercados con
-- open_time_utc ANTERIOR a esto ya se usaron en el backtest V1/V2/V3 y NO
-- son "datos completamente nuevos" -- se ignoran a proposito.
CREATE TABLE IF NOT EXISTS paper_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT
);
"""

_local = threading.local()
BUSY_TIMEOUT_MS = 30_000


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def reset_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def connect():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    setup = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        setup.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        setup.execute("PRAGMA journal_mode=WAL")
        setup.executescript(SCHEMA)
        setup.commit()
    finally:
        setup.close()


def get_meta(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta_if_absent(key, value):
    """INSERT OR IGNORE: si ya existe, no lo pisa -- el cutoff de arranque
    debe quedar fijo para siempre, incluso si el proceso se reinicia."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO paper_meta (key, value) VALUES (?, ?)", (key, value))
        row = conn.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
    return row["value"]


def log_event(component, event_type, detail=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO paper_events (ts, component, event_type, detail) VALUES (?, ?, ?, ?)",
            (time.time(), component, event_type, json.dumps(detail) if detail is not None else None),
        )
