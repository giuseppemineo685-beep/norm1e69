"""
V3: umbral fijo $0.99 (nunca calibrado), y la interseccion momentum/favorito
con misma liquidez/ejecucion para ambos lados.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CONDITION_ID = "0xv3"
TOKEN_UP, TOKEN_DOWN = "tok_up", "tok_down"


def _fresh_conn():
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _insert_market(conn, cid, open_ts, close_ts, winner="Up"):
    conn.execute(
        "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
        "token_up_id, token_down_id, open_time_utc, close_time_utc, window_minutes, "
        "winner, discovered_at) VALUES (?,?,?,?,?,?,?,?,5,?,0)",
        (cid, "slug", "test", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, winner))
    conn.commit()


def _insert_snapshot(conn, cid, ts, token, best_bid, best_ask, levels=None):
    cur = conn.execute(
        """INSERT INTO orderbook_snapshots (source_timestamp_utc, received_at_utc_ms,
           condition_id, token_id, outcome, best_bid, best_ask, source)
           VALUES (?,?,?,?,?,?,?,'REST')""",
        (ts, ts * 1000, cid, token, "Up", best_bid, best_ask))
    snap_id = cur.lastrowid
    if levels:
        for i, (price, size) in enumerate(levels, start=1):
            conn.execute(
                "INSERT INTO orderbook_levels (snapshot_id, side, level, price, size) VALUES (?,?,?,?,?)",
                (snap_id, "ask", i, price, size))
    conn.commit()
    return snap_id


def _insert_underlying(conn, ts, asset, price, open_ref):
    dist = (price - open_ref) / open_ref * 100
    conn.execute(
        """INSERT INTO underlying_prices (source_timestamp_utc, received_at_utc, asset_symbol,
           source, price, market_open_reference_price, distance_from_open_abs,
           distance_from_open_pct) VALUES (?,?,?,'test',?,?,?,?)""",
        (ts, ts, asset, price, open_ref, price - open_ref, dist))
    conn.commit()


def test_v3_never_completes_above_099_even_if_below_105():
    """A un costo de 1.00 (que SI cerraria en V2-B con umbral 1.05), V3 con
    su umbral fijo 0.99 NO debe completar -- confirma que el umbral de V3
    es independiente y mas estricto, nunca se afloja."""
    import backtest_v2_inventory as v2
    import backtest_v3_inventory as v3
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, CONDITION_ID, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_underlying(conn, decision_ts, "BTC", 100.05, 100.0)  # +0.05% -> momentum Up
    _insert_snapshot(conn, CONDITION_ID, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, CONDITION_ID, t, TOKEN_DOWN, 0.59, 0.60, levels=[(0.60, 100)])  # combinado 1.00

    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    r = v2.simulate_market(conn, m, v2.side_momentum, v3.V3_PAIR_THRESHOLD, threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    assert r["status"] == "NEVER_COMPLETED", "V3 completo un par a costo 1.00, por encima de su umbral fijo 0.99"

    # el MISMO escenario con el umbral de V2-B (1.05) SI deberia completar -- confirma que 1.00 esta bien,
    # es solo mas estricto que V2-B, no un bug generico del motor
    r_v2b = v2.simulate_market(conn, m, v2.side_momentum, v3.V2B_FROZEN_THRESHOLD_PAIR, threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    assert r_v2b["status"] == "COMPLETED"


def test_v3_completes_below_099():
    import backtest_v2_inventory as v2
    import backtest_v3_inventory as v3
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, CONDITION_ID, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_underlying(conn, decision_ts, "BTC", 100.05, 100.0)
    _insert_snapshot(conn, CONDITION_ID, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, CONDITION_ID, t, TOKEN_DOWN, 0.57, 0.58, levels=[(0.58, 100)])  # combinado 0.98

    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    r = v2.simulate_market(conn, m, v2.side_momentum, v3.V3_PAIR_THRESHOLD, threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    assert r["status"] == "COMPLETED"
    assert r["combined_cost"] <= 0.99 + 1e-9


def test_common_markets_requires_liquidity_on_both_sides():
    """Si momentum elige un lado sin profundidad mientras favorito si puede
    ejecutar, ese mercado debe QUEDAR AFUERA de la interseccion (no es
    'misma condicion de liquidez' si uno de los dos no puede operar)."""
    import backtest_v2_inventory as v2
    import backtest_v3_inventory as v3
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, CONDITION_ID, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_underlying(conn, decision_ts, "BTC", 100.05, 100.0)  # momentum -> Up
    # Up (lado de momentum) SIN profundidad -- solo top-of-book, sin niveles
    _insert_snapshot(conn, CONDITION_ID, decision_ts, TOKEN_UP, 0.39, 0.40, levels=None)
    _insert_snapshot(conn, CONDITION_ID, decision_ts, TOKEN_DOWN, 0.59, 0.60, levels=[(0.60, 100)])

    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    common = v3.common_markets_momentum_vs_favorite(conn, [m])
    assert common == [], "incluyo un mercado donde momentum no podia ejecutar de verdad (sin profundidad)"


def test_v3_declares_no_pairs_without_loosening_threshold():
    """Si nunca hay una oportunidad <=0.99, V3 debe reportar 0 pares --
    nunca debe 'encontrar' un par usando un costo > 0.99."""
    import backtest_v2_inventory as v2
    import backtest_v3_inventory as v3
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, CONDITION_ID, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_underlying(conn, decision_ts, "BTC", 100.05, 100.0)
    _insert_snapshot(conn, CONDITION_ID, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, CONDITION_ID, t, TOKEN_DOWN, 0.99, 1.00, levels=[(1.00, 100)])  # siempre 1.40 > 0.99

    results = v2.run_variant(conn, [conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()],
                              v2.side_momentum, v3.V3_PAIR_THRESHOLD, threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    n_pairs = sum(1 for r in results if r["status"] == "COMPLETED")
    assert n_pairs == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
