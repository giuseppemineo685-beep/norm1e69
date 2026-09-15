"""
Garantía anti-look-ahead: el contexto de un instante SOLO puede salir de
datos que existían en o antes de ese instante. Si esto se rompiera, un
backtest construido sobre `trade_context` estaría mirando el futuro y sus
resultados no valdrían nada.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CONDITION_ID = "0xlookahead"
TOKEN_UP = "tok_up"
TOKEN_DOWN = "tok_down"
TRADE_TS = 1000.0


def _fresh_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    db.reset_connection()
    db.init_db()
    return db


def _seed(db):
    """Un trade en t=1000 y dos snapshots: uno ANTES (t=999, ask 0.40) y uno
    DESPUÉS (t=1001, ask 0.90). El de después está más cerca en valor absoluto
    si el trade cayera en t=1000.4, así que una búsqueda por |distancia| lo
    elegiría -- justo el bug que este test bloquea."""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
            "token_up_id, token_down_id, discovered_at) VALUES (?,?,?,?,?,?,0)",
            (CONDITION_ID, "slug", "test", "BTC", TOKEN_UP, TOKEN_DOWN))
        conn.execute(
            """INSERT INTO leader_trades
               (id, leader_wallet, transaction_hash, source_timestamp_utc, received_at_utc,
                condition_id, asset_symbol, token_id, outcome, side, price, shares,
                usdc_amount, raw_payload, collection_method)
               VALUES (1,'0xlead','0xh',?,?,?,'BTC',?, 'Up','BUY',0.4,10,4,'{}','LIVE')""",
            (TRADE_TS, TRADE_TS, CONDITION_ID, TOKEN_UP))

        for token in (TOKEN_UP, TOKEN_DOWN):
            for ts, ask in ((TRADE_TS - 1, 0.40), (TRADE_TS + 1, 0.90)):
                conn.execute(
                    """INSERT INTO orderbook_snapshots
                       (source_timestamp_utc, received_at_utc_ms, condition_id, token_id,
                        outcome, best_bid, best_ask, source)
                       VALUES (?,?,?,?,?,?,?,'REST')""",
                    (ts, ts * 1000, CONDITION_ID, token, "Up", ask - 0.02, ask))

        for ts, price in ((TRADE_TS - 1, 100.0), (TRADE_TS + 1, 999.0)):
            conn.execute(
                """INSERT INTO underlying_prices
                   (source_timestamp_utc, received_at_utc, asset_symbol, source, price)
                   VALUES (?,?,'BTC','test',?)""", (ts, ts, price))


def test_snapshot_lookup_never_returns_a_later_snapshot(monkeypatch):
    db = _fresh_db(monkeypatch)
    _seed(db)
    import trade_context as tc

    with db.connect() as conn:
        # instante justo despues del snapshot viejo: debe devolver EL VIEJO
        snap, age = tc._nearest_snapshot(conn, CONDITION_ID, TOKEN_UP, TRADE_TS + 0.4)
        assert snap is not None
        assert snap["best_ask"] == 0.40, "uso un snapshot POSTERIOR al instante pedido"
        assert age >= 0, "la antiguedad nunca puede ser negativa"

        under = tc._nearest_underlying(conn, "BTC", TRADE_TS + 0.4)
        assert under is not None and under["price"] == 100.0, "uso un precio del futuro"


def test_built_context_uses_only_past_data(monkeypatch):
    db = _fresh_db(monkeypatch)
    _seed(db)
    import trade_context as tc

    with db.connect() as conn:
        trade = conn.execute("SELECT * FROM leader_trades WHERE id=1").fetchone()
        tc._build_one(conn, trade)
        rows = conn.execute(
            "SELECT * FROM trade_context WHERE leader_trade_id=1 AND context_available=1"
        ).fetchall()

    assert rows, "no se construyo contexto"
    for r in rows:
        # el unico snapshot valido en o antes de cualquier offset<=+1 es el de 0.40;
        # el de 0.90 (t=1001) solo puede aparecer en offsets >= +1
        if r["offset_seconds"] < 1:
            assert r["best_ask_up"] == 0.40, \
                f"offset {r['offset_seconds']}s uso datos del futuro (ask {r['best_ask_up']})"
        assert r["snapshot_age_s"] >= 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
