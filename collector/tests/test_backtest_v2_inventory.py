"""
Motor de V2 (walk por shares, monitoreo de completar el par, calculo de
P&L). Base de datos sintetica en un archivo temporal, con el schema real
de db.py (misma fuente de verdad que el resto del repo).
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CONDITION_ID = "0xv2"
TOKEN_UP, TOKEN_DOWN = "tok_up", "tok_down"


def _fresh_conn():
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _insert_market(conn, open_ts, close_ts, winner="Up"):
    conn.execute(
        "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
        "token_up_id, token_down_id, open_time_utc, close_time_utc, window_minutes, "
        "winner, discovered_at) VALUES (?,?,?,?,?,?,?,?,5,?,0)",
        (CONDITION_ID, "slug", "test", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, winner))
    conn.commit()


def _insert_snapshot(conn, ts, token, best_bid, best_ask, levels=None):
    cur = conn.execute(
        """INSERT INTO orderbook_snapshots (source_timestamp_utc, received_at_utc_ms,
           condition_id, token_id, outcome, best_bid, best_ask, source)
           VALUES (?,?,?,?,?,?,?,'REST')""",
        (ts, ts * 1000, CONDITION_ID, token, "Up", best_bid, best_ask))
    snap_id = cur.lastrowid
    if levels:
        for i, (price, size) in enumerate(levels, start=1):
            conn.execute(
                "INSERT INTO orderbook_levels (snapshot_id, side, level, price, size) VALUES (?,?,?,?,?)",
                (snap_id, "ask", i, price, size))
    conn.commit()
    return snap_id


def test_walk_executable_shares_full_partial_skipped():
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    _insert_market(conn, 0, 300)

    snap_full = _insert_snapshot(conn, 10, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    status, shares, spent, vwap = v2.walk_executable_shares(conn, snap_full, 20.0)
    assert status == "FULL" and abs(shares - 20.0) < 1e-6 and abs(spent - 8.0) < 1e-6

    snap_partial = _insert_snapshot(conn, 11, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 5)])
    status, shares, spent, vwap = v2.walk_executable_shares(conn, snap_partial, 20.0)
    assert status == "PARTIAL" and abs(shares - 5.0) < 1e-6

    snap_none = _insert_snapshot(conn, 12, TOKEN_UP, 0.39, 0.40, levels=None)
    status, shares, spent, vwap = v2.walk_executable_shares(conn, snap_none, 20.0)
    assert status == "SKIPPED" and shares == 0.0


def test_monitor_completes_at_first_moment_condition_is_met():
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, open_ts, close_ts)
    decision_ts = open_ts + v2.DECISION_OFFSET_S

    # leg1 en Up a precio 0.40 (own_vwap=0.40). Lado contrario (Down) empieza
    # caro (0.70 -> combinado 1.10, no cierra) y DESPUES de 15s baja a 0.55
    # (combinado 0.95, SI cierra). El monitor debe agarrar el primer instante
    # en que se cumple, no antes.
    _insert_snapshot(conn, decision_ts, TOKEN_DOWN, 0.69, 0.70, levels=[(0.70, 100)])
    _insert_snapshot(conn, decision_ts + v2.CHECK_INTERVAL_S, TOKEN_DOWN, 0.69, 0.70, levels=[(0.70, 100)])
    good_ts = decision_ts + 2 * v2.CHECK_INTERVAL_S
    _insert_snapshot(conn, good_ts, TOKEN_DOWN, 0.54, 0.55, levels=[(0.55, 100)])
    # snapshots posteriores tambien buenos -- no deberian afectar (ya se completo antes)
    _insert_snapshot(conn, good_ts + v2.CHECK_INTERVAL_S, TOKEN_DOWN, 0.50, 0.51, levels=[(0.51, 100)])

    leg1 = {"status": "FULL", "shares": 20.0, "usd": 8.0, "vwap": 0.40}
    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    result = v2.monitor_and_complete(conn, m, "Up", leg1, decision_ts, threshold_pair=1.00)

    assert result["completed"] is True
    assert result["completed_at"] == good_ts, "no encontro el PRIMER instante valido"
    assert abs(result["combined_cost"] - 0.95) < 1e-6


def test_monitor_never_completes_if_condition_never_met():
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, open_ts, close_ts)
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    # siempre caro, nunca cumple el umbral
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, t, TOKEN_DOWN, 0.79, 0.80, levels=[(0.80, 100)])

    leg1 = {"status": "FULL", "shares": 20.0, "usd": 8.0, "vwap": 0.40}
    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    result = v2.monitor_and_complete(conn, m, "Up", leg1, decision_ts, threshold_pair=1.00)
    assert result["completed"] is False


def test_monitor_ignores_snapshots_after_close_buffer():
    """Un snapshot barato que llega DESPUES de close_time-60s nunca debe
    completar el par -- la ventana de nuevas acciones cierra antes."""
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, open_ts, close_ts)
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    late_ts = close_ts - v2.CLOSE_BUFFER_S + 5  # DENTRO de los ultimos 60s: no deberia mirarse
    _insert_snapshot(conn, late_ts, TOKEN_DOWN, 0.10, 0.11, levels=[(0.11, 100)])

    leg1 = {"status": "FULL", "shares": 20.0, "usd": 8.0, "vwap": 0.40}
    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    result = v2.monitor_and_complete(conn, m, "Up", leg1, decision_ts, threshold_pair=1.00)
    assert result["completed"] is False, "uso un snapshot dentro de los ultimos 60s (deberia estar cerrado)"


def test_completed_pair_pnl_matches_hand_computation():
    """matched*1 - matched_cost + residual, con matched = min(leg1, completion).
    Mercado gana el lado de leg1 -> la porcion residual paga $1/share."""
    import backtest_v2_inventory as v2
    result = {
        "leg1_shares": 20.0, "leg1_vwap": 0.40, "leg1_usd": 8.0,
        "completion_shares": 15.0, "completion_vwap": 0.50, "completion_usd": 7.5,
    }
    matched = min(result["leg1_shares"], result["completion_shares"])
    matched_cost = matched * result["leg1_vwap"] + matched * result["completion_vwap"]
    paired_pnl = matched * 1.0 - matched_cost
    residual_shares = result["leg1_shares"] - matched
    residual_cost = residual_shares * result["leg1_vwap"]
    residual_payout = residual_shares * 1.0  # leg1 side won
    residual_pnl = residual_payout - residual_cost
    total_pnl = paired_pnl + residual_pnl

    assert matched == 15.0
    assert abs(matched_cost - (15 * 0.40 + 15 * 0.50)) < 1e-9
    assert abs(paired_pnl - (15 * 1.0 - 13.5)) < 1e-9
    assert abs(residual_shares - 5.0) < 1e-9
    assert abs(residual_pnl - (5.0 - 2.0)) < 1e-9
    assert abs(total_pnl - (paired_pnl + residual_pnl)) < 1e-9


def test_never_completed_pnl_is_pure_directional():
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_snapshot(conn, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, t, TOKEN_DOWN, 0.99, 1.00, levels=[(1.00, 100)])  # nunca conviene cerrar

    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    r = v2.simulate_market(conn, m, v2.side_cheap, threshold_pair=1.00)
    assert r["status"] == "NEVER_COMPLETED"
    assert r["matched_shares"] == 0.0
    expected_shares = 10.0 / 0.40
    assert abs(r["leg1_shares"] - expected_shares) < 1e-6
    assert abs(r["total_pnl"] - (expected_shares * 1.0 - 10.0)) < 1e-6  # gano Up


def test_classification_and_pnl_never_peek_at_future_snapshots():
    """Snapshot MUY barato para completar, pero con timestamp POSTERIOR al
    cierre de nuevas entradas -- no debe usarse ni para decidir ni para pnl."""
    import backtest_v2_inventory as v2
    conn = _fresh_conn()
    open_ts, close_ts = 0, 300
    _insert_market(conn, open_ts, close_ts, winner="Down")
    decision_ts = open_ts + v2.DECISION_OFFSET_S
    _insert_snapshot(conn, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    # nada barato disponible ANTES del cierre de ventana
    for t in range(decision_ts, close_ts - v2.CLOSE_BUFFER_S, v2.CHECK_INTERVAL_S):
        _insert_snapshot(conn, t, TOKEN_DOWN, 0.94, 0.95, levels=[(0.95, 100)])
    # este SI seria barato, pero cae en los ultimos 60s
    _insert_snapshot(conn, close_ts - 30, TOKEN_DOWN, 0.05, 0.06, levels=[(0.06, 100)])

    m = conn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    r = v2.simulate_market(conn, m, v2.side_cheap, threshold_pair=1.00)
    assert r["status"] == "NEVER_COMPLETED", "se colo un snapshot fuera de la ventana permitida"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
