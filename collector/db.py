"""
SQLite storage for the research collector. WAL mode, batched transactions,
UNIQUE constraints for idempotency (insert-before-mark-processed: a row
either lands fully or not at all -- a crash mid-poll never produces a
duplicate on restart, and never marks something "seen" without it having
been persisted first).

Two independent datasets, never mixed:
  1. Leader dataset:  leader_trades, leader_inventory_timeline, trade_context
  2. Market dataset:  markets, market_trades, orderbook_snapshots,
                       orderbook_levels, underlying_prices
Plus: leader_poll_log / ws_events / collector_events for data-quality tracking.

No table here is ever written to by anything that also touches
scripts/live_trader.py's state files -- this module doesn't import or
reference the live-trading flag, the wallet private key env var, or any
order-placement code.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    market_slug TEXT UNIQUE,
    market_title TEXT,
    asset_symbol TEXT,
    token_up_id TEXT,
    token_down_id TEXT,
    open_time_utc REAL,
    close_time_utc REAL,
    resolution_time_utc REAL,
    winner TEXT,                     -- 'Up' | 'Down' | NULL while open
    resolution_source TEXT,          -- verbatim string from the API (e.g. Chainlink feed)
    open_reference_price REAL,       -- underlying price at open, if known
    window_minutes INTEGER,          -- 5 o 15: el líder opera ambas duraciones
    discovered_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS leader_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leader_wallet TEXT NOT NULL,
    trade_id TEXT,
    transaction_hash TEXT NOT NULL,
    source_timestamp_utc REAL NOT NULL,   -- leader_trade_timestamp (de la fuente)
    received_at_utc REAL NOT NULL,        -- = api_received_at (compat)
    api_received_at REAL,                 -- instante en que llegó la respuesta HTTP
    request_started_at REAL,              -- instante en que se lanzó la request
    collector_processed_at REAL,          -- instante en que se escribió esta fila
    cdn_age_s REAL,                       -- header `age` del CDN, si vino cacheada
    is_startup_batch INTEGER DEFAULT 0,   -- 1 = vino en el primer poll (historia previa
                                           -- al arranque): se EXCLUYE de latencia/cobertura
    detection_latency_ms REAL,            -- api_received_at - leader_trade_timestamp
    condition_id TEXT,
    market_slug TEXT,
    market_title TEXT,
    asset_symbol TEXT,
    token_id TEXT,
    outcome TEXT,                    -- 'Up' | 'Down'
    side TEXT,                       -- 'BUY' | 'SELL'
    price REAL,
    shares REAL,
    usdc_amount REAL,
    seconds_since_market_open REAL,
    seconds_to_market_close REAL,
    maker_taker TEXT,                -- only if evidenced by the API, else NULL
    raw_payload TEXT NOT NULL,       -- full JSON of the source trade, verbatim
    collection_method TEXT NOT NULL, -- 'LIVE' | 'BACKFILL'
    UNIQUE (leader_wallet, transaction_hash, source_timestamp_utc, token_id, shares)
);
CREATE INDEX IF NOT EXISTS idx_leader_trades_condition ON leader_trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_leader_trades_wallet_ts ON leader_trades(leader_wallet, source_timestamp_utc);

CREATE TABLE IF NOT EXISTS market_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_timestamp_utc REAL NOT NULL,
    received_at_utc REAL NOT NULL,    -- instante en que llegó ESTA respuesta
    request_started_at REAL,
    condition_id TEXT NOT NULL,
    token_id TEXT,
    outcome TEXT,
    side TEXT,
    price REAL,
    shares REAL,
    usdc_amount REAL,
    transaction_hash TEXT,
    UNIQUE (condition_id, transaction_hash, token_id, source_timestamp_utc, price, shares)
);
CREATE INDEX IF NOT EXISTS idx_market_trades_condition ON market_trades(condition_id);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_timestamp_utc REAL,       -- from the WS/book payload if present
    received_at_utc_ms REAL NOT NULL, -- instante en que llegó ESTA respuesta (por request)
    request_started_at REAL,          -- instante en que se lanzó ESTA request
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    seconds_since_open REAL,
    seconds_to_close REAL,
    best_bid REAL, best_bid_size REAL,
    best_ask REAL, best_ask_size REAL,
    mid_price REAL,
    spread REAL,
    last_trade_price REAL,
    message_seq TEXT,                -- book 'hash' or sequence id, if present
    source TEXT NOT NULL              -- 'WS' | 'REST'
);
CREATE INDEX IF NOT EXISTS idx_ob_snap_token_ts ON orderbook_snapshots(token_id, received_at_utc_ms);

CREATE TABLE IF NOT EXISTS orderbook_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES orderbook_snapshots(id),
    side TEXT NOT NULL,               -- 'bid' | 'ask'
    level INTEGER NOT NULL,           -- 1 = best
    price REAL NOT NULL,
    size REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ob_levels_snapshot ON orderbook_levels(snapshot_id);

CREATE TABLE IF NOT EXISTS underlying_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_timestamp_utc REAL,
    received_at_utc REAL NOT NULL,
    asset_symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    price REAL NOT NULL,
    market_open_reference_price REAL,
    distance_from_open_abs REAL,
    distance_from_open_pct REAL,
    seconds_to_close REAL,
    condition_id TEXT,                -- which 5-min window this reading belongs to, if any
    request_started_at REAL,          -- instante en que se lanzó la request
    open_price_method TEXT,           -- cómo se obtuvo market_open_reference_price:
                                       -- 'first_observation_after_open' = NO es el precio de
                                       -- apertura real del mercado, es la primera lectura que
                                       -- alcanzamos a tomar tras detectar la ventana
    open_price_observation_delay_s REAL  -- segundos entre la apertura de la ventana y esa lectura
);
CREATE INDEX IF NOT EXISTS idx_underlying_asset_ts ON underlying_prices(asset_symbol, received_at_utc);

CREATE TABLE IF NOT EXISTS leader_inventory_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    after_trade_id INTEGER NOT NULL REFERENCES leader_trades(id),
    computed_at REAL NOT NULL,
    up_shares REAL NOT NULL,
    down_shares REAL NOT NULL,
    up_cost REAL NOT NULL,
    down_cost REAL NOT NULL,
    vwap_up REAL,
    vwap_down REAL,
    matched_shares REAL NOT NULL,
    surplus_side TEXT,                 -- 'Up' | 'Down' | NULL if perfectly matched
    surplus_shares REAL NOT NULL,
    coverage_ratio REAL,               -- matched_shares / max(up_shares, down_shares)
    matched_cost REAL NOT NULL,
    paired_edge REAL NOT NULL,         -- matched_shares*1 - matched_cost: beneficio de la
                                        -- PORCIÓN emparejada, ignorando el sobrante
    portfolio_floor_pnl REAL NOT NULL, -- min(up,down) - (up_cost + down_cost): peor caso de
                                        -- TODA la posición (el sobrante puede valer 0)
    guaranteed_profit REAL,            -- DEPRECADO: alias histórico de paired_edge
    directional_exposure_shares REAL NOT NULL,
    time_to_first_opposite_trade_s REAL, -- primera compra del lado contrario. NO implica
                                          -- cobertura completa: es solo el primer toque
    time_to_coverage_threshold_s REAL,   -- tiempo hasta alcanzar COVERAGE_THRESHOLD real
    coverage_threshold_used REAL,
    n_sell_trades_excluded INTEGER DEFAULT 0, -- SELLs vistos en este mercado y excluidos
    time_to_hedge_second_leg_s REAL,   -- DEPRECADO: alias de time_to_first_opposite_trade_s
    UNIQUE (after_trade_id)
);
CREATE INDEX IF NOT EXISTS idx_inventory_condition ON leader_inventory_timeline(condition_id);

CREATE TABLE IF NOT EXISTS trade_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    leader_trade_id INTEGER NOT NULL REFERENCES leader_trades(id),
    offset_seconds INTEGER NOT NULL,   -- one of -10,-5,-3,-1,0,1,3,5,10
    usable_for_backtest INTEGER NOT NULL, -- 0/1: false for offset > 0 (outcome, not input)
    context_available INTEGER NOT NULL,   -- 0/1: false if no real snapshot was close enough
    underlying_available INTEGER DEFAULT 0, -- 0/1: idem para el precio del subyacente
    context_quality TEXT,                 -- 'executable' = el snapshot trae niveles de
                                           --   profundidad reales, así que executable_price y
                                           --   combined_cost_to_pair son fiables.
                                           -- 'indicative' = solo top-of-book (evento WS
                                           --   price_change): se conoce el mejor bid/ask pero
                                           --   NO qué hay detrás. depth_* y executable_price
                                           --   quedan en NULL, nunca en 0.
                                           -- NULL = sin contexto disponible.
    snapshot_age_s REAL,                  -- antigüedad del snapshot del lado del líder
                                           -- (siempre >= 0: solo snapshots EN O ANTES)
    age_up_book_s REAL,                   -- antigüedad del snapshot del libro UP
    age_down_book_s REAL,                 -- antigüedad del snapshot del libro DOWN
    age_underlying_s REAL,                -- antigüedad de la lectura del subyacente
    executable_shares_available REAL,     -- shares que la profundidad guardada podía llenar
    executable_fill_ratio REAL,           -- available / target (1.0 = se podía llenar entero)
    opposite_executable_price REAL,       -- VWAP ejecutable de la pierna CONTRARIA para el
                                           -- MISMO tamaño objetivo (no solo su best ask)
    opposite_fill_ratio REAL,
    best_bid_up REAL, best_ask_up REAL, depth_bid_up REAL, depth_ask_up REAL,
    best_bid_down REAL, best_ask_down REAL, depth_bid_down REAL, depth_ask_down REAL,
    spread_up REAL, spread_down REAL,
    underlying_price REAL,
    underlying_distance_from_open_pct REAL,
    executable_price_for_leader_size REAL,  -- VWAP to fill the leader's own trade size, from depth
    opposite_leg_price REAL,                -- best ask of the other side at this instant
    combined_cost_to_pair REAL,              -- executable_price_for_leader_size + opposite_leg_price
    leader_up_shares_snapshot REAL,
    leader_down_shares_snapshot REAL,
    UNIQUE (leader_trade_id, offset_seconds)
);
CREATE INDEX IF NOT EXISTS idx_trade_context_trade ON trade_context(leader_trade_id);

-- Data-quality tracking
CREATE TABLE IF NOT EXISTS leader_poll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    n_returned INTEGER NOT NULL,
    n_new INTEGER NOT NULL,
    error TEXT,
    oldest_ts_in_page REAL,     -- para detectar huecos reales entre polls
    newest_ts_in_page REAL,
    n_gap_filled INTEGER DEFAULT 0,  -- trades recuperados paginando hacia atrás
    gap_pages_fetched INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collector_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    component TEXT NOT NULL,          -- 'ws' | 'leader_trades' | 'market_trades' | 'orderbook' | 'underlying'
    event_type TEXT NOT NULL,         -- 'gap' | 'reconnect' | 'duplicate' | 'out_of_order' |
                                       -- 'clock_skew' | 'error' | 'start' | 'stop'
    detail TEXT                       -- free-form JSON
);
CREATE INDEX IF NOT EXISTS idx_collector_events_ts ON collector_events(ts);
"""


# Una conexión por hilo, reutilizada. Abrir/cerrar una conexión por operación
# desde 8 hilos a ~1000 escrituras/s hacía que SQLite tirara "database is
# locked" al arrancar: `PRAGMA journal_mode=WAL` pide un lock exclusivo y se
# re-ejecutaba en CADA conexión nueva. journal_mode es una propiedad
# persistente de la base -- se setea una sola vez en init_db(), no por
# conexión. `busy_timeout` sí es por conexión: hace que un escritor espere su
# turno en vez de fallar (SQLite permite un solo escritor a la vez, incluso
# en WAL).
_local = threading.local()
BUSY_TIMEOUT_MS = 30_000


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def reset_connection():
    """Cierra la conexión cacheada de este hilo. Necesario al cambiar DB_PATH
    (tests), si no se seguiría usando la conexión abierta contra la db vieja."""
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


# Migraciones no destructivas: solo se AGREGAN columnas que falten en una db
# ya existente. Nunca se borra ni se reescribe nada -- una db vieja sigue
# siendo válida, solo gana columnas nuevas en NULL.
MIGRATIONS = {
    "orderbook_snapshots": {"request_started_at": "REAL"},
    "market_trades": {"request_started_at": "REAL"},
    "underlying_prices": {
        "request_started_at": "REAL",
        "open_price_method": "TEXT",
        "open_price_observation_delay_s": "REAL",
    },
    "leader_inventory_timeline": {
        "paired_edge": "REAL",
        "portfolio_floor_pnl": "REAL",
        "time_to_first_opposite_trade_s": "REAL",
        "time_to_coverage_threshold_s": "REAL",
        "coverage_threshold_used": "REAL",
        "n_sell_trades_excluded": "INTEGER DEFAULT 0",
    },
    "leader_trades": {
        "request_started_at": "REAL",
        "api_received_at": "REAL",
        "collector_processed_at": "REAL",
        "cdn_age_s": "REAL",
        "is_startup_batch": "INTEGER DEFAULT 0",
    },
    "trade_context": {
        "underlying_available": "INTEGER DEFAULT 0",
        "context_quality": "TEXT",
        "age_up_book_s": "REAL",
        "age_down_book_s": "REAL",
        "age_underlying_s": "REAL",
        "executable_shares_available": "REAL",
        "executable_fill_ratio": "REAL",
        "opposite_executable_price": "REAL",
        "opposite_fill_ratio": "REAL",
    },
    "markets": {
        "window_minutes": "INTEGER",
    },
    "leader_poll_log": {
        "oldest_ts_in_page": "REAL",
        "newest_ts_in_page": "REAL",
        "n_gap_filled": "INTEGER DEFAULT 0",
        "gap_pages_fetched": "INTEGER DEFAULT 0",
    },
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue  # tabla aún no creada; el SCHEMA ya la trae completa
        for col, coltype in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def init_db():
    # journal_mode=WAL es persistente en el archivo: se setea una sola vez acá,
    # nunca por conexión (ver comentario en _conn()).
    setup = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        setup.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        setup.execute("PRAGMA journal_mode=WAL")
        setup.executescript(SCHEMA)
        setup.row_factory = sqlite3.Row
        _migrate(setup)
        setup.commit()
    finally:
        setup.close()


def log_event(component, event_type, detail=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO collector_events (ts, component, event_type, detail) VALUES (?, ?, ?, ?)",
            (time.time(), component, event_type, json.dumps(detail) if detail is not None else None),
        )


def executemany(sql, rows):
    """Batch insert helper -- one transaction for the whole list."""
    if not rows:
        return 0
    with connect() as conn:
        conn.executemany(sql, rows)
        return len(rows)
