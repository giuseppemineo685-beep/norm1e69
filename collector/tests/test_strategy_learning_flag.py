"""
usable_for_strategy_learning y sus tres correcciones: regla conservadora
(event_ts Y received_ts <= trade_ts), snapshot_up_id/snapshot_down_id
trazables, y exclusion del hueco de order book de 96.8min.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CONDITION_ID = "0xlearn"
TOKEN_UP = "tok_up"
TOKEN_DOWN = "tok_down"


def _fresh_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import config
    monkeypatch.setattr(config, "DB_PATH", tmp.name)
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    db.reset_connection()
    db.init_db()
    return db


def _insert_market(db, condition_id=CONDITION_ID):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
            "token_up_id, token_down_id, discovered_at) VALUES (?,?,?,?,?,?,0)",
            (condition_id, "slug", "test", "BTC", TOKEN_UP, TOKEN_DOWN))


def _insert_trade(db, trade_id, trade_ts, collection_method="LIVE", is_startup_batch=0,
                   condition_id=CONDITION_ID):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO leader_trades
               (id, leader_wallet, transaction_hash, source_timestamp_utc, received_at_utc,
                condition_id, asset_symbol, token_id, outcome, side, price, shares,
                usdc_amount, raw_payload, collection_method, is_startup_batch)
               VALUES (?,'0xlead',?,?,?,?,'BTC',?,'Up','BUY',0.4,10,4,'{}',?,?)""",
            (trade_id, f"0xh{trade_id}", trade_ts, trade_ts, condition_id, TOKEN_UP,
             collection_method, is_startup_batch))


def _insert_snapshot(db, ts, received_ms, condition_id=CONDITION_ID, token=TOKEN_UP, ask=0.5):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO orderbook_snapshots
               (source_timestamp_utc, received_at_utc_ms, condition_id, token_id,
                outcome, best_bid, best_ask, source)
               VALUES (?,?,?,?,?,?,?,'REST')""",
            (ts, received_ms, condition_id, token, "Up", ask - 0.02, ask))


def _insert_underlying(db, ts, price=100.0):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO underlying_prices (source_timestamp_utc, received_at_utc,
               asset_symbol, source, price) VALUES (?,?,'BTC','test',?)""",
            (ts, ts, price))


def test_conservative_rule_rejects_late_received_snapshot(monkeypatch):
    """Evento declara ts=999 (antes del trade en 1000), pero llego REALMENTE
    al collector en received=1002 (despues del trade) -- la regla vieja lo
    aceptaba (solo miraba el ts declarado), la nueva debe rechazarlo."""
    db = _fresh_db(monkeypatch)
    _insert_market(db)
    _insert_trade(db, 1, 1000.0)
    _insert_snapshot(db, ts=999.0, received_ms=1002.0 * 1000, token=TOKEN_UP)
    _insert_snapshot(db, ts=999.0, received_ms=1002.0 * 1000, token=TOKEN_DOWN)
    _insert_underlying(db, 999.5)

    import trade_context as tc
    with db.connect() as conn:
        snap, age = tc._nearest_snapshot(conn, CONDITION_ID, TOKEN_UP, 1000.0)
    assert snap is None, "acepto un snapshot recibido DESPUES del trade -- regla conservadora rota"


def test_conservative_rule_accepts_snapshot_received_before_trade(monkeypatch):
    db = _fresh_db(monkeypatch)
    _insert_market(db)
    _insert_snapshot(db, ts=999.0, received_ms=999.2 * 1000, token=TOKEN_UP)

    import trade_context as tc
    with db.connect() as conn:
        snap, age = tc._nearest_snapshot(conn, CONDITION_ID, TOKEN_UP, 1000.0)
    assert snap is not None
    assert age is not None and age >= 0


def test_snapshot_up_down_ids_and_usable_flag_end_to_end(monkeypatch):
    db = _fresh_db(monkeypatch)
    _insert_market(db)
    _insert_trade(db, 1, 1000.0)
    _insert_snapshot(db, ts=999.8, received_ms=999.9 * 1000, token=TOKEN_UP, ask=0.55)
    _insert_snapshot(db, ts=999.7, received_ms=999.85 * 1000, token=TOKEN_DOWN, ask=0.48)
    _insert_underlying(db, 999.9)

    import trade_context as tc
    with db.connect() as conn:
        trade = conn.execute("SELECT * FROM leader_trades WHERE id=1").fetchone()
        tc._build_one(conn, trade)
        row0 = conn.execute(
            "SELECT * FROM trade_context WHERE leader_trade_id=1 AND offset_seconds=0"
        ).fetchone()

    assert row0["snapshot_up_id"] is not None
    assert row0["snapshot_down_id"] is not None
    assert row0["snapshot_up_id"] != row0["snapshot_down_id"]
    assert row0["underlying_price_id"] is not None
    assert row0["usable_for_strategy_learning"] == 1


def test_gap_trades_never_usable_for_strategy_learning(monkeypatch):
    """Un trade con contexto PERFECTO (todo disponible, todo conservador)
    pero cuyo timestamp cae dentro del hueco confirmado de 96.8min debe
    quedar usable_for_strategy_learning=0 igual -- el hueco es irrecuperable
    independientemente de que, por casualidad, hubiera algun snapshot cerca."""
    db = _fresh_db(monkeypatch)
    _insert_market(db)
    import trade_context as tc
    gap_ts = tc.ORDERBOOK_GAP_START_TS + 60  # bien adentro del hueco
    _insert_trade(db, 1, gap_ts)
    _insert_snapshot(db, ts=gap_ts - 0.2, received_ms=(gap_ts - 0.1) * 1000, token=TOKEN_UP)
    _insert_snapshot(db, ts=gap_ts - 0.2, received_ms=(gap_ts - 0.1) * 1000, token=TOKEN_DOWN)
    _insert_underlying(db, gap_ts - 0.1)

    with db.connect() as conn:
        trade = conn.execute("SELECT * FROM leader_trades WHERE id=1").fetchone()
        tc._build_one(conn, trade)
        row0 = conn.execute(
            "SELECT * FROM trade_context WHERE leader_trade_id=1 AND offset_seconds=0"
        ).fetchone()

    assert row0["context_available"] == 1, "el setup del test debe tener contexto disponible"
    assert row0["usable_for_strategy_learning"] == 0, "un trade dentro del hueco nunca debe ser usable"


def test_startup_and_backfill_never_usable_for_strategy_learning(monkeypatch):
    db = _fresh_db(monkeypatch)
    _insert_market(db)
    _insert_trade(db, 1, 1000.0, collection_method="LIVE", is_startup_batch=1)
    _insert_trade(db, 2, 1000.0, collection_method="BACKFILL", is_startup_batch=0)
    for tid in (1, 2):
        pass
    _insert_snapshot(db, ts=999.8, received_ms=999.9 * 1000, token=TOKEN_UP)
    _insert_snapshot(db, ts=999.7, received_ms=999.85 * 1000, token=TOKEN_DOWN)
    _insert_underlying(db, 999.9)

    import trade_context as tc
    with db.connect() as conn:
        for tid in (1, 2):
            trade = conn.execute("SELECT * FROM leader_trades WHERE id=?", (tid,)).fetchone()
            tc._build_one(conn, trade)
        rows = conn.execute(
            "SELECT leader_trade_id, usable_for_strategy_learning FROM trade_context WHERE offset_seconds=0"
        ).fetchall()

    for r in rows:
        assert r["usable_for_strategy_learning"] == 0, \
            f"trade {r['leader_trade_id']} (startup o backfill) nunca debe ser usable"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
