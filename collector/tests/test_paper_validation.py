"""
Paper trading validator: seguridad (escaneo estatico + conexion read-only
real) y logica (decisiones, cobertura, resolucion) -- SIN tocar collector/
data.db, todo contra bases sinteticas temporales.
"""
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PAPER_FILES = ["run_paper_validation.py", "paper_validation_db.py", "export_paper_validation.py"]
COLLECTOR_DIR = os.path.join(os.path.dirname(__file__), "..")

FORBIDDEN_PATTERNS = [
    (r"\bimport\s+requests\b", "importa requests (posible llamada de red / de ejecucion)"),
    (r"\bfrom\s+requests\b", "importa requests (posible llamada de red / de ejecucion)"),
    (r"\bimport\s+polymarket_api\b", "importa polymarket_api (hace requests HTTP; el validador no debe)"),
    (r"\bscripts\.live_trader\b", "referencia al ejecutor real"),
    (r"\blive_trader\b", "referencia al ejecutor real"),
    (r"\bSecureClient\b", "cliente de firma de ordenes del ejecutor real"),
    (r"\bpy_clob_client\b", "cliente CLOB de ordenes"),
    (r"\bclob_client\b", "cliente CLOB de ordenes"),
    (r"POLY_PRIVATE_KEY", "variable de entorno de clave privada"),
    (r"\bPRIVATE_KEY\b", "clave privada"),
    (r"\.env\b", "referencia a archivo de credenciales"),
    (r"create_order|post_order|cancel_order|place_order", "funcion de creacion/cancelacion de ordenes"),
    (r"requests\.post|requests\.put|requests\.delete", "verbo HTTP de escritura (creacion/cancelacion)"),
]


def _read(fname):
    with open(os.path.join(COLLECTOR_DIR, fname)) as f:
        return f.read()


def _read_code_only(fname):
    """Igual que _read pero sacando docstrings/comentarios -- el escaneo de
    seguridad debe reaccionar a IMPORTS y LLAMADAS reales, no a que un
    comentario explique en prosa que algo NO se hace (eso es documentacion
    util, no una violacion)."""
    src = _read(fname)
    src = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    src = re.sub(r"'''.*?'''", "", src, flags=re.DOTALL)
    lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    return "\n".join(lines)


def test_no_trading_imports_or_calls_in_paper_validation_files():
    for fname in PAPER_FILES:
        src = _read_code_only(fname)
        for pattern, why in FORBIDDEN_PATTERNS:
            assert not re.search(pattern, src), f"{fname} contiene un patron prohibido ({why}): /{pattern}/"


def test_no_network_library_imported_anywhere_in_paper_files():
    """Ademas del escaneo por patron: ninguno de los 3 archivos debe importar
    absolutamente ninguna libreria de red (requests, httpx, urllib, websockets,
    aiohttp, http.client) -- el validador es 100% sqlite3 + stdlib no-red."""
    network_libs = ("requests", "httpx", "urllib", "websockets", "aiohttp", "http.client", "socket")
    for fname in PAPER_FILES:
        src = _read(fname)
        import_lines = [l for l in src.splitlines() if l.strip().startswith(("import ", "from "))]
        for line in import_lines:
            for lib in network_libs:
                assert lib not in line, f"{fname}: linea de import sospechosa de red: {line!r}"


def test_paper_db_path_is_separate_from_collector_data_db():
    import config
    import paper_validation_db as pdb
    assert os.path.basename(pdb.DB_PATH) == "paper_validation.db"
    assert os.path.abspath(pdb.DB_PATH) != os.path.abspath(config.DB_PATH)


def test_readonly_uri_connection_actually_rejects_writes():
    """Prueba real, no solo de codigo: una conexion sqlite abierta con
    mode=ro (la misma tecnica que usa run_paper_validation.data_conn())
    debe rechazar un INSERT a nivel del propio driver."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    setup = sqlite3.connect(tmp.name)
    setup.execute("CREATE TABLE t (x INTEGER)")
    setup.commit()
    setup.close()

    ro = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
    try:
        ro.execute("INSERT INTO t (x) VALUES (1)")
        ro.commit()
        assert False, "una conexion mode=ro permitio escribir -- no deberia ser posible"
    except sqlite3.OperationalError:
        pass  # esperado: "attempt to write a readonly database"


def test_run_paper_validation_never_calls_data_conn_without_mode_ro():
    src = _read("run_paper_validation.py")
    assert "mode=ro" in src
    assert 'sqlite3.connect(DATA_DB_PATH' not in src.replace(" ", "")


# ==================================================================== logica ===
CONDITION_ID = "0xpv"
TOKEN_UP, TOKEN_DOWN = "tok_up", "tok_down"


def _fresh_data_conn():
    """Simula collector/data.db (schema real, via db.SCHEMA) en un archivo
    temporal -- nunca el data.db verdadero."""
    import db as collector_db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(collector_db.SCHEMA)
    return conn


def _fresh_paper_conn(monkeypatch):
    import paper_validation_db as pdb
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(pdb, "DB_PATH", tmp.name)
    pdb.reset_connection()
    pdb.init_db()
    return pdb


def _insert_market(conn, open_ts, close_ts, winner=None, cid=CONDITION_ID):
    conn.execute(
        "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
        "token_up_id, token_down_id, open_time_utc, close_time_utc, window_minutes, "
        "winner, discovered_at) VALUES (?,?,?,?,?,?,?,?,5,?,0)",
        (cid, f"slug-{cid}", "test", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, winner))
    conn.commit()


def _insert_snapshot(conn, ts, token, best_bid, best_ask, levels=None, cid=CONDITION_ID):
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


def test_decide_momentum_no_trade_below_threshold():
    import run_paper_validation as rpv
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S
    _insert_underlying(dconn, decision_ts, "BTC", 100.005, 100.0)  # +0.005%, bajo el umbral 0.02%
    m = dconn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()

    r = rpv.decide_momentum(dconn, m, decision_ts)
    assert r["signal_present"] == 0
    assert r["side_chosen"] is None
    assert r["fill_status"] == "NO_TRADE"


def test_decide_momentum_trades_above_threshold_walks_depth():
    import run_paper_validation as rpv
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S
    _insert_underlying(dconn, decision_ts, "BTC", 100.05, 100.0)  # +0.05% -> Up
    _insert_snapshot(dconn, decision_ts, TOKEN_UP, 0.39, 0.40, levels=[(0.40, 100)])
    m = dconn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()

    r = rpv.decide_momentum(dconn, m, decision_ts)
    assert r["signal_present"] == 1
    assert r["side_chosen"] == "Up"
    assert r["fill_status"] == "FULL"
    assert abs(r["capital_usd"] - rpv.STAKE_USD) < 1e-6


def test_decide_momentum_fails_closed_when_no_underlying_data():
    import run_paper_validation as rpv
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S
    m = dconn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()

    r = rpv.decide_momentum(dconn, m, decision_ts)
    assert r["fill_status"] == "SKIPPED"
    assert r["side_chosen"] is None, "no debe inventar un lado sin datos"


def test_decide_favorite_picks_higher_implied_probability():
    import run_paper_validation as rpv
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S
    _insert_snapshot(dconn, decision_ts, TOKEN_UP, 0.68, 0.70, levels=[(0.70, 100)])   # mid 0.69 (favorito)
    _insert_snapshot(dconn, decision_ts, TOKEN_DOWN, 0.28, 0.30, levels=[(0.30, 100)])  # mid 0.29
    m = dconn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()

    r = rpv.decide_favorite(dconn, m, decision_ts)
    assert r["side_chosen"] == "Up", "debe elegir el lado con MAYOR precio/probabilidad implicita"
    assert r["fill_status"] == "FULL"


def test_hedge_executes_only_when_all_conditions_met(monkeypatch):
    import run_paper_validation as rpv
    pdb = _fresh_paper_conn(monkeypatch)
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S

    with pdb.connect() as pconn:
        pconn.execute("""INSERT INTO paper_markets (condition_id, market_title, asset_symbol,
            token_up_id, token_down_id, open_time_utc, close_time_utc, decision_time_utc, discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "t", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, decision_ts, 0))
        cur = pconn.execute("""INSERT INTO paper_decisions
            (condition_id, strategy, decision_timestamp_utc, asset_symbol, signal_present,
             side_chosen, token_id, snapshot_id, best_ask, executable_price, shares, capital_usd,
             fill_status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "MOMENTUM_PARTIAL_HEDGE", decision_ts, "BTC", 1, "Up", TOKEN_UP,
             1, 0.40, 0.40, 20.0, 8.0, "FULL", 0))
        decision_id = cur.lastrowid

    # oportunidad CARA: no debe cubrir (precio contrario 0.50 > 0.10)
    now1 = decision_ts + 5
    _insert_snapshot(dconn, now1, TOKEN_DOWN, 0.49, 0.50, levels=[(0.50, 100)])
    with pdb.connect() as pconn:
        d = pconn.execute("SELECT * FROM paper_decisions WHERE id=?", (decision_id,)).fetchone()
        rpv.check_and_maybe_hedge(dconn, pconn, d, now1)
        assert pconn.execute("SELECT count(*) c FROM paper_hedge_fills").fetchone()["c"] == 0

    # oportunidad BUENA: precio contrario 0.09 (<=0.10), combinado 0.49 (<=0.90), profundidad de sobra
    now2 = decision_ts + 10
    _insert_snapshot(dconn, now2, TOKEN_DOWN, 0.08, 0.09, levels=[(0.09, 1000)])
    with pdb.connect() as pconn:
        d = pconn.execute("SELECT * FROM paper_decisions WHERE id=?", (decision_id,)).fetchone()
        rpv.check_and_maybe_hedge(dconn, pconn, d, now2)
        fills = pconn.execute("SELECT * FROM paper_hedge_fills").fetchall()
        assert len(fills) == 1
        assert abs(fills[0]["target_shares"] - 5.0) < 1e-6  # 25% de 20 shares
        assert fills[0]["fill_status"] == "FULL"

    dconn.close()


def test_hedge_never_more_than_25_percent():
    import run_paper_validation as rpv
    target = 20.0 * rpv.HEDGE_FRACTION
    assert abs(target - 5.0) < 1e-9
    assert rpv.HEDGE_FRACTION == 0.25


def test_hedge_fails_closed_past_close_buffer(monkeypatch):
    import run_paper_validation as rpv
    pdb = _fresh_paper_conn(monkeypatch)
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts)
    decision_ts = open_ts + rpv.DECISION_OFFSET_S

    with pdb.connect() as pconn:
        pconn.execute("""INSERT INTO paper_markets (condition_id, market_title, asset_symbol,
            token_up_id, token_down_id, open_time_utc, close_time_utc, decision_time_utc, discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "t", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, decision_ts, 0))
        cur = pconn.execute("""INSERT INTO paper_decisions
            (condition_id, strategy, decision_timestamp_utc, asset_symbol, signal_present,
             side_chosen, token_id, snapshot_id, best_ask, executable_price, shares, capital_usd,
             fill_status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "MOMENTUM_PARTIAL_HEDGE", decision_ts, "BTC", 1, "Up", TOKEN_UP,
             1, 0.40, 0.40, 20.0, 8.0, "FULL", 0))
        decision_id = cur.lastrowid

    # un instante muy barato, pero DENTRO de los ultimos 60s (close-60=240)
    late_now = close_ts - 30
    _insert_snapshot(dconn, late_now, TOKEN_DOWN, 0.04, 0.05, levels=[(0.05, 1000)])
    with pdb.connect() as pconn:
        d = pconn.execute("SELECT * FROM paper_decisions WHERE id=?", (decision_id,)).fetchone()
        rpv.check_and_maybe_hedge(dconn, pconn, d, late_now)
        assert pconn.execute("SELECT count(*) c FROM paper_hedge_fills").fetchone()["c"] == 0

    dconn.close()


def test_resolve_pending_includes_hedge_leg(monkeypatch):
    import run_paper_validation as rpv
    pdb = _fresh_paper_conn(monkeypatch)
    dconn = _fresh_data_conn()
    open_ts, close_ts = 0, 300
    _insert_market(dconn, open_ts, close_ts, winner="Up")
    decision_ts = open_ts + rpv.DECISION_OFFSET_S

    with pdb.connect() as pconn:
        pconn.execute("""INSERT INTO paper_markets (condition_id, market_title, asset_symbol,
            token_up_id, token_down_id, open_time_utc, close_time_utc, decision_time_utc, discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "t", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, decision_ts, 0))
        cur = pconn.execute("""INSERT INTO paper_decisions
            (condition_id, strategy, decision_timestamp_utc, asset_symbol, signal_present,
             side_chosen, token_id, snapshot_id, best_ask, executable_price, shares, capital_usd,
             fill_status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (CONDITION_ID, "MOMENTUM_PARTIAL_HEDGE", decision_ts, "BTC", 1, "Up", TOKEN_UP,
             1, 0.40, 0.40, 20.0, 8.0, "FULL", 0))
        decision_id = cur.lastrowid
        pconn.execute("""INSERT INTO paper_hedge_fills
            (decision_id, condition_id, executed_at_utc, target_shares, filled_shares, usd_spent,
             vwap, fill_status, snapshot_id) VALUES (?,?,?,?,?,?,?,?,?)""",
            (decision_id, CONDITION_ID, decision_ts + 10, 5.0, 5.0, 0.45, 0.09, "FULL", 2))

    with pdb.connect() as pconn:
        n = rpv.resolve_pending(dconn, pconn, close_ts + 1)
        assert n == 1
        res = pconn.execute("SELECT * FROM paper_resolutions WHERE decision_id=?", (decision_id,)).fetchone()
        # capital = 8.0 (leg1) + 0.45 (hedge) = 8.45; payout: leg1 gano (Up), hedge (Down) perdio
        assert abs(res["capital_deployed_usd"] - 8.45) < 1e-6
        assert abs(res["payout_usd"] - 20.0) < 1e-6  # solo la pierna Up paga, la cobertura Down no
        assert abs(res["pnl_usd"] - (20.0 - 8.45)) < 1e-6

    dconn.close()


def test_discover_markets_respects_cutoff(monkeypatch):
    import run_paper_validation as rpv
    pdb = _fresh_paper_conn(monkeypatch)
    dconn = _fresh_data_conn()
    _insert_market(dconn, open_ts=1000, close_ts=1300, cid="0xold")   # antes del cutoff
    _insert_market(dconn, open_ts=5000, close_ts=5300, cid="0xnew")   # despues del cutoff

    with pdb.connect() as pconn:
        n = rpv.discover_markets(dconn, pconn, cutoff_ts=2000)
        assert n == 1
        rows = pconn.execute("SELECT condition_id FROM paper_markets").fetchall()
        assert [r["condition_id"] for r in rows] == ["0xnew"], \
            "el mercado anterior al cutoff (datos ya usados en V1/V2/V3) no debe entrar"

    dconn.close()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
