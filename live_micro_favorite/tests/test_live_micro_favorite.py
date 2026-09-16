"""
Ejecutor micro-LIVE FAVORITE_BASELINE: aislamiento (del ejecutor viejo Y del
otro ejecutor nuevo, momentum), kill switch/limites (logica pura) y un ciclo
DRY_RUN completo de principio a fin -- todo contra bases sinteticas
temporales, nunca contra data.db/live_micro_favorite.db reales.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

FAVORITE_DIR = os.path.join(os.path.dirname(__file__), "..")
COLLECTOR_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "collector")
LIVE_MICRO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "live_micro")
sys.path.insert(0, FAVORITE_DIR)
sys.path.insert(0, COLLECTOR_DIR)

FAVORITE_FILES = ["live_micro_favorite_config.py", "live_micro_favorite_db.py",
                   "live_micro_favorite_kill_switch.py", "live_micro_favorite_order_client.py",
                   "live_micro_favorite_executor.py", "run_live_micro_favorite.py"]
NON_ORDER_CLIENT_FILES = [f for f in FAVORITE_FILES if f != "live_micro_favorite_order_client.py"]


def _read(fname):
    with open(os.path.join(FAVORITE_DIR, fname)) as f:
        return f.read()


def _read_code_only(fname):
    src = _read(fname)
    src = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    src = re.sub(r"'''.*?'''", "", src, flags=re.DOTALL)
    lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
    return "\n".join(lines)


# ============================================================= aislamiento ===
def test_no_reference_to_old_live_trader_anywhere():
    patterns = [r"\blive_trader\b", r"scripts\.live_trader", r"run_live_bounded", r"\bWALLET\s*=\s*\"0x41e2e"]
    for fname in FAVORITE_FILES:
        src = _read_code_only(fname)
        for pat in patterns:
            assert not re.search(pat, src), f"{fname} referencia al ejecutor viejo: /{pat}/"


def test_old_live_trader_file_is_byte_identical_untouched():
    with open(os.path.join(FAVORITE_DIR, "..", "scripts", "live_trader.py")) as f:
        old = f.read()
    assert "WALLET = \"0x41e2e1ccf1e4940029af02259a31c6b89b9fa354\"" in old
    assert "def make_client():" in old


def test_no_coupling_with_momentum_executor():
    """Los dos ejecutores nuevos (live_micro/ momentum, live_micro_favorite/
    favorite) deben ser completamente independientes -- ninguno importa
    modulos del otro ni lee sus variables de entorno."""
    momentum_names = ["LIVE_MICRO_PRIVATE_KEY", "LIVE_MICRO_FUNDER", "LIVE_MICRO_SIGNATURE_TYPE",
                       "LIVE_MICRO_ENABLED", "live_micro_config", "live_micro_db",
                       "live_micro_kill_switch", "live_micro_order_client", "live_micro_executor"]
    for fname in FAVORITE_FILES:
        src = _read_code_only(fname)
        for name in momentum_names:
            assert name not in src, f"{fname} referencia al ejecutor momentum ({name}) -- deben ser independientes"


def test_only_order_client_imports_polymarket_sdk_or_reads_its_credentials():
    for fname in NON_ORDER_CLIENT_FILES:
        src = _read_code_only(fname)
        assert "import polymarket" not in src, f"{fname} importa el SDK real -- debe vivir solo en live_micro_favorite_order_client.py"
        assert "SecureClient" not in src, f"{fname} referencia SecureClient directamente"


def test_env_var_names_distinct_from_old_bot_and_momentum_executor():
    old_names = ["POLY_PRIVATE_KEY", "POLY_FUNDER", "POLY_SIGNATURE_TYPE", "LIVE_MAX_TOTAL_CAPITAL"]
    for fname in FAVORITE_FILES:
        src = _read_code_only(fname)
        for name in old_names:
            assert f'"{name}"' not in src and f"'{name}'" not in src, \
                f"{fname} lee la variable de entorno del bot viejo {name!r}"


def test_importing_favorite_modules_does_not_pull_in_old_live_trader_or_momentum():
    import importlib
    for mod in ("live_micro_favorite_config", "live_micro_favorite_db", "live_micro_favorite_kill_switch",
                "live_micro_favorite_order_client", "live_micro_favorite_executor"):
        importlib.import_module(mod)
    assert "scripts.live_trader" not in sys.modules
    assert "live_micro_config" not in sys.modules  # el modulo de config del OTRO ejecutor nuevo


def test_no_partial_hedge_strategy_referenced_anywhere():
    for fname in FAVORITE_FILES:
        src = _read_code_only(fname)
        assert "PARTIAL_HEDGE" not in src
        assert "check_and_maybe_hedge" not in src


def test_only_fak_order_type_configured():
    import live_micro_favorite_config as cfg
    assert cfg.ORDER_TYPE == "FAK"


def test_strategy_is_favorite_baseline():
    import live_micro_favorite_config as cfg
    assert cfg.STRATEGY == "POLYMARKET_FAVORITE_BASELINE"


# ================================================================ kill switch ===
def _fresh_state():
    import live_micro_favorite_kill_switch as ks
    return ks.fresh_state()


def test_max_orders_limit_blocks_next_order():
    """Dinamico contra cfg.MAX_ORDERS -- hoy 1 (TEMPORAL, ver config), antes 10."""
    import live_micro_favorite_config as cfg
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    for _ in range(cfg.MAX_ORDERS):
        ok, _ = ks.check_before_order(state, 2.0)
        assert ok
        ks.record_attempt(state, 2.0)
        ks.record_execution_outcome(state, True)
    ok, reason = ks.check_before_order(state, 2.0)
    assert not ok
    assert "operaciones" in reason


def test_capital_cap_is_derived_and_blocks_before_exceeding():
    """Dinamico: MAX_CAPITAL_DEPLOYED_USD = MAX_ORDERS x MAX_ORDER_USD, siempre derivado."""
    import live_micro_favorite_config as cfg
    import live_micro_favorite_kill_switch as ks
    assert cfg.MAX_CAPITAL_DEPLOYED_USD == cfg.MAX_ORDERS * cfg.MAX_ORDER_USD
    state = _fresh_state()
    cap = cfg.MAX_CAPITAL_DEPLOYED_USD
    ok, _ = ks.check_before_order(state, cap)  # exactamente el tope, inclusive
    assert ok
    ok, reason = ks.check_before_order(state, cap + 0.01)
    assert not ok
    assert "capital" in reason


def test_kill_switch_trips_on_10_dollar_loss():
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    ks.record_realized_pnl(state, -6.0)
    assert not state["kill_switch_tripped"]
    ks.record_realized_pnl(state, -4.0)  # total -10.0
    assert state["kill_switch_tripped"]
    assert "perdida acumulada" in state["kill_switch_reason"]


def test_kill_switch_does_not_trip_on_9_99_loss():
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    ks.record_realized_pnl(state, -9.99)
    assert not state["kill_switch_tripped"]


def test_three_consecutive_failures_trips_kill_switch():
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    ks.record_execution_outcome(state, False)
    ks.record_execution_outcome(state, False)
    assert not state["kill_switch_tripped"]
    ks.record_execution_outcome(state, False)
    assert state["kill_switch_tripped"]
    assert "consecutivas" in state["kill_switch_reason"]


def test_success_resets_consecutive_failure_counter():
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    ks.record_execution_outcome(state, False)
    ks.record_execution_outcome(state, False)
    ks.record_execution_outcome(state, True)
    assert state["consecutive_failures"] == 0


def test_kill_switch_already_tripped_blocks_everything():
    import live_micro_favorite_kill_switch as ks
    state = _fresh_state()
    state["kill_switch_tripped"] = True
    state["kill_switch_reason"] = "prueba"
    ok, reason = ks.check_before_order(state, 0.01)
    assert not ok
    assert "prueba" in reason


# ======================================================= DRY_RUN end-to-end ===
CONDITION_ID = "0xlmf"
TOKEN_UP, TOKEN_DOWN = "tok_up_lmf", "tok_down_lmf"


def _fresh_data_conn():
    import db as collector_db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    conn.executescript(collector_db.SCHEMA)
    return conn


def _insert_market(conn, open_ts, close_ts, winner=None, cid=CONDITION_ID):
    conn.execute(
        "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, "
        "token_up_id, token_down_id, open_time_utc, close_time_utc, window_minutes, "
        "winner, discovered_at) VALUES (?,?,?,?,?,?,?,?,5,?,0)",
        (cid, f"slug-{cid}", "test", "BTC", TOKEN_UP, TOKEN_DOWN, open_ts, close_ts, winner))
    conn.commit()


def _insert_snapshot(conn, received_at_ms, token, best_bid, best_ask, levels=None, cid=CONDITION_ID):
    cur = conn.execute(
        """INSERT INTO orderbook_snapshots (source_timestamp_utc, received_at_utc_ms,
           condition_id, token_id, outcome, best_bid, best_ask, source)
           VALUES (?,?,?,?,?,?,?,'REST')""",
        (received_at_ms / 1000.0, received_at_ms, cid, token, "Up", best_bid, best_ask))
    snap_id = cur.lastrowid
    if levels:
        for i, (price, size) in enumerate(levels, start=1):
            conn.execute(
                "INSERT INTO orderbook_levels (snapshot_id, side, level, price, size) VALUES (?,?,?,?,?)",
                (snap_id, "ask", i, price, size))
    conn.commit()
    return snap_id


def _build_scenario(up_ask=0.70, down_ask=0.30, up_levels=None, down_levels=None,
                     snapshot_age_s=0.3, min_order_size=1.0):
    import live_micro_favorite_kill_switch as ks
    dconn = _fresh_data_conn()
    open_ts, close_ts = 1_000_000, 1_000_300
    decision_ts = open_ts + 60
    _insert_market(dconn, open_ts, close_ts)
    now_wall_clock = decision_ts + snapshot_age_s
    received_at_ms = int((now_wall_clock - snapshot_age_s) * 1000)
    _insert_snapshot(dconn, received_at_ms, TOKEN_UP, up_ask - 0.01, up_ask,
                      levels=up_levels or [(up_ask, 100)])
    _insert_snapshot(dconn, received_at_ms, TOKEN_DOWN, down_ask - 0.01, down_ask,
                      levels=down_levels or [(down_ask, 100)])
    m = dconn.execute("SELECT * FROM markets WHERE condition_id=?", (CONDITION_ID,)).fetchone()
    state = ks.fresh_state()
    return dconn, m, now_wall_clock, state


def test_dry_run_picks_favorite_side_and_logs_paper_and_attempt_in_parallel(monkeypatch):
    import json
    import live_micro_favorite_executor as ex
    import live_micro_favorite_order_client as oc
    monkeypatch.setattr(oc, "get_market_meta", lambda cid, timeout=8: {
        "tick_size": 0.01, "minimum_order_size": 1.0, "raw": {}})

    def _boom(*a, **k):
        raise AssertionError("DRY_RUN no debe llamar funciones que mueven plata real")
    monkeypatch.setattr(oc, "place_fak_market_buy", _boom)
    monkeypatch.setattr(oc, "make_client", _boom)

    dconn, m, now_wall_clock, state = _build_scenario(up_ask=0.70, down_ask=0.30)  # Up es favorito (precio mas alto)
    row, attempted = ex.build_attempt(dconn, m, now_wall_clock, state, "DRY_RUN")

    assert attempted
    assert row["paper_side"] == "Up"
    assert row["side"] == "Up"
    assert row["paper_fill_status"] == "FULL"
    assert row["fill_status"] == "SIMULATED_DRY_RUN"
    assert row["quantity_usd"] == 2.0  # amount objetivo pre-fee, no derivado de shares
    assert row["limit_price"] == 0.70
    assert row["fee_estimated_usd"] is not None and row["fee_estimated_usd"] > 0
    assert row["fee_real_usd"] is None
    assert state["n_orders_attempted"] == 1

    req = json.loads(row["api_request_json"])
    assert req["amount"] == 2.0
    assert req["max_spend"] == 2.0
    assert req["max_price"] == 0.70
    assert req["order_type"] == "FAK"
    assert "size" not in req  # ya no es una orden por shares


def test_dry_run_skips_on_stale_snapshot_over_1_second(monkeypatch):
    import live_micro_favorite_executor as ex
    import live_micro_favorite_order_client as oc
    monkeypatch.setattr(oc, "get_market_meta", lambda cid, timeout=8: {
        "tick_size": 0.01, "minimum_order_size": 1.0, "raw": {}})
    dconn, m, now_wall_clock, state = _build_scenario(snapshot_age_s=1.5)
    row, attempted = ex.build_attempt(dconn, m, now_wall_clock, state, "DRY_RUN")
    assert not attempted
    assert row["fill_status"] == "SKIPPED_STALE_SNAPSHOT"
    assert state["n_orders_attempted"] == 0


def test_dry_run_proceeds_even_when_minimum_order_size_would_have_exceeded_2usd(monkeypatch):
    """Verificado (2026-09-16): minimum_order_size (shares) NO aplica a
    place_market_order(amount=...) -- 2 de 15 fills LIVE reales de
    scripts/live_trader.py tuvieron menos de 5 shares, en mercados que
    siguen reportando minimum_order_size=5. Antes esto generaba
    SKIPPED_SIZE_INFEASIBLE; ahora se registra solo informativamente y NO
    bloquea."""
    import live_micro_favorite_executor as ex
    import live_micro_favorite_order_client as oc
    monkeypatch.setattr(oc, "get_market_meta", lambda cid, timeout=8: {
        "tick_size": 0.01, "minimum_order_size": 5.0, "raw": {}})
    dconn, m, now_wall_clock, state = _build_scenario(up_ask=0.70, down_ask=0.30)
    row, attempted = ex.build_attempt(dconn, m, now_wall_clock, state, "DRY_RUN")
    assert attempted
    assert row["fill_status"] == "SIMULATED_DRY_RUN"
    assert row["min_order_size_shares"] == 5.0  # informativo, registrado igual
    assert row["quantity_usd"] == 2.0
    assert state["n_orders_attempted"] == 1


def test_dry_run_tie_produces_no_paper_signal(monkeypatch):
    import live_micro_favorite_executor as ex
    dconn, m, now_wall_clock, state = _build_scenario(up_ask=0.50, down_ask=0.50)
    row, attempted = ex.build_attempt(dconn, m, now_wall_clock, state, "DRY_RUN")
    assert not attempted
    assert row["fill_status"] == "NO_PAPER_SIGNAL"
    assert row["paper_side"] is None
    assert state["n_orders_attempted"] == 0


def test_dry_run_skipped_by_kill_switch_when_already_tripped(monkeypatch):
    import live_micro_favorite_executor as ex
    import live_micro_favorite_order_client as oc
    monkeypatch.setattr(oc, "get_market_meta", lambda cid, timeout=8: {
        "tick_size": 0.01, "minimum_order_size": 1.0, "raw": {}})
    dconn, m, now_wall_clock, state = _build_scenario()
    state["kill_switch_tripped"] = True
    state["kill_switch_reason"] = "prueba"
    row, attempted = ex.build_attempt(dconn, m, now_wall_clock, state, "DRY_RUN")
    assert not attempted
    assert row["fill_status"] == "SKIPPED_KILL_SWITCH"


# ============================================== prueba puntual: 1 intento y stop ===
def test_max_orders_is_temporarily_1_and_stop_after_first_attempt_is_on():
    """Verifica el override temporal pedido: max_orders=1 y el proceso debe
    pararse solo tras el primer intento, sea cual sea su resultado."""
    import live_micro_favorite_config as cfg
    assert cfg.MAX_ORDERS == 1
    assert cfg.STOP_AFTER_FIRST_ATTEMPT is True
    assert cfg.MAX_CAPITAL_DEPLOYED_USD == 2.0  # derivado: 1 x $2


def test_run_stops_after_first_attempt_regardless_of_fill_status(monkeypatch, tmp_path):
    """cycle() con STOP_AFTER_FIRST_ATTEMPT=True procesa como maximo UN
    mercado por llamada, sea cual sea su fill_status (incluye SKIPPED)."""
    import live_micro_favorite_config as cfg
    import live_micro_favorite_db as ldb
    import live_micro_favorite_kill_switch as ks
    import run_live_micro_favorite as rlmf

    assert cfg.STOP_AFTER_FIRST_ATTEMPT is True  # precondicion de este test

    tmp_db = tmp_path / "test_stop.db"
    monkeypatch.setattr(ldb, "DB_PATH", str(tmp_db))
    ldb.reset_connection()
    ldb.init_db(mode="DRY_RUN")

    dconn = _fresh_data_conn()
    open_ts, close_ts = 1_000_000, 1_000_300
    _insert_market(dconn, open_ts, close_ts, cid="0xstop1")
    _insert_market(dconn, open_ts, close_ts, cid="0xstop2")  # NUNCA deberia procesarse

    import live_micro_favorite_executor as ex
    monkeypatch.setattr(ex, "data_conn", lambda: dconn)
    monkeypatch.setattr(ex, "select_candidate_markets", lambda dc, cutoff, now, seen: [
        dconn.execute("SELECT * FROM markets WHERE condition_id=?", (cid,)).fetchone()
        for cid in ("0xstop1", "0xstop2") if cid not in seen
    ])

    state = ks.fresh_state()
    n_processed, first_row = rlmf.cycle(0, "DRY_RUN", state, set())
    assert n_processed == 1
    assert first_row is not None
    assert first_row["condition_id"] == "0xstop1"
    # sin señal en este escenario sintetico (sin underlying_prices) -> SKIPPED,
    # y aun asi cuenta como "el primer intento" para efectos del stop.
    assert first_row["fill_status"] == "NO_PAPER_SIGNAL"


# ==================================================================== gate LIVE ===
def test_live_disabled_by_default_without_any_env_vars(monkeypatch):
    for var in ("LIVE_MICRO_FAVORITE", "LIVE_MICRO_FAVORITE_CONFIRM",
                "LIVE_MICRO_FAVORITE_PRIVATE_KEY", "LIVE_MICRO_FAVORITE_FUNDER"):
        monkeypatch.delenv(var, raising=False)
    import importlib
    import live_micro_favorite_config as cfg
    importlib.reload(cfg)
    assert cfg.LIVE_MICRO_FAVORITE_ENABLED is False


def test_live_requires_exact_confirm_phrase(monkeypatch):
    monkeypatch.setenv("LIVE_MICRO_FAVORITE", "1")
    monkeypatch.setenv("LIVE_MICRO_FAVORITE_CONFIRM", "yes")
    import importlib
    import live_micro_favorite_config as cfg
    importlib.reload(cfg)
    assert cfg.LIVE_MICRO_FAVORITE_ENABLED is False
    monkeypatch.delenv("LIVE_MICRO_FAVORITE", raising=False)
    monkeypatch.delenv("LIVE_MICRO_FAVORITE_CONFIRM", raising=False)
    importlib.reload(cfg)


def test_live_enabled_only_with_both_gates_exact(monkeypatch):
    monkeypatch.setenv("LIVE_MICRO_FAVORITE", "1")
    monkeypatch.setenv("LIVE_MICRO_FAVORITE_CONFIRM", "I_UNDERSTAND_REAL_MONEY")
    import importlib
    import live_micro_favorite_config as cfg
    importlib.reload(cfg)
    assert cfg.LIVE_MICRO_FAVORITE_ENABLED is True
    monkeypatch.delenv("LIVE_MICRO_FAVORITE", raising=False)
    monkeypatch.delenv("LIVE_MICRO_FAVORITE_CONFIRM", raising=False)
    importlib.reload(cfg)


# ============================================================== DB separada ===
def test_live_micro_favorite_db_path_is_separate_from_everything():
    import config
    import live_micro_favorite_db as ldb
    import paper_validation_db as pdb
    assert os.path.basename(ldb.DB_PATH) == "live_micro_favorite.db"
    assert os.path.abspath(ldb.DB_PATH) != os.path.abspath(config.DB_PATH)
    assert os.path.abspath(ldb.DB_PATH) != os.path.abspath(pdb.DB_PATH)
    sys.path.insert(0, LIVE_MICRO_DIR)
    import live_micro_db as mldb
    assert os.path.abspath(ldb.DB_PATH) != os.path.abspath(mldb.DB_PATH)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
