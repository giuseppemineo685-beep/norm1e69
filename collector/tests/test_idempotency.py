"""Insertar el mismo trade dos veces debe dejar una sola fila."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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


def _sample_trade(ts=1000.0):
    return {
        "timestamp": ts, "conditionId": "0xabc", "outcome": "Up", "asset": "tok1",
        "price": "0.5", "size": "10", "transactionHash": "0xhash1", "title": "test market",
        "side": "BUY", "id": "trade1",
    }


def test_duplicate_leader_trade_inserts_once(monkeypatch):
    db = _fresh_db(monkeypatch)
    import leader_trades_collector as ltc

    t = _sample_trade()
    with db.connect() as conn:
        assert ltc._insert_trade(conn, t, api_received_at=1000.5) is True
        assert ltc._insert_trade(conn, t, api_received_at=1001.0) is False  # mismo trade -> ignorado

    with db.connect() as conn:
        n = conn.execute("SELECT count(*) c FROM leader_trades").fetchone()["c"]
    assert n == 1


def test_restart_after_crash_mid_batch_does_not_duplicate(monkeypatch):
    """Simulates: process inserts trade A, 'crashes', restarts, re-polls and
    sees trade A again (same batch re-delivered) -- must still be 1 row."""
    db = _fresh_db(monkeypatch)
    import leader_trades_collector as ltc

    t = _sample_trade()
    with db.connect() as conn:
        ltc._insert_trade(conn, t, api_received_at=1000.5)
        # "restart": nothing in memory persists (no seen-set to reload, unlike the
        # old jsonl/seen_leader_keys approach) -- correctness comes entirely from
        # the UNIQUE constraint, not from remembering what we've seen
        ltc._insert_trade(conn, t, api_received_at=1000.6)
        ltc._insert_trade(conn, t, api_received_at=1000.7)

    with db.connect() as conn:
        n = conn.execute("SELECT count(*) c FROM leader_trades").fetchone()["c"]
    assert n == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
