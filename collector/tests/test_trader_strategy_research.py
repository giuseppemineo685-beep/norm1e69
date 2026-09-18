"""
Tests de trader_strategy_research.py contra bases SINTETICAS (mismo SCHEMA
real de data.db y paper_validation.db, datos ficticios). Nunca tocan las
bases reales. Cubren: dedupe historico/LIVE, BUY+SELL, fills en el mismo
segundo, anti-look-ahead (evento y recepcion), snapshot futuro mas cercano,
inventario/VWAP, matched/surplus, pendientes, profundidad insuficiente,
fees, comparacion sobre mismos mercados, split TRAIN/VALIDATION/TEST,
solo-lectura a nivel de driver y ausencia de imports de red/trading.
"""
import argparse
import os
import re
import sqlite3
import sys
import tempfile

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import trader_strategy_research as tsr  # noqa: E402
import db as collector_db  # noqa: E402  (solo para el SCHEMA)
import paper_validation_db as pdb  # noqa: E402  (solo para el SCHEMA)

WALLET = tsr.DEFAULT_LEADER_WALLET
T0 = 1_789_000_000.0  # 2026-09


# ------------------------------------------------------------------ fixtures ---
def _new_db(schema, extra_sql=()):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(schema)
    for s in extra_sql:
        conn.execute(s)
    conn.commit()
    conn.close()
    return tmp.name


def _w(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def add_market(conn, cid, asset, open_ts, winner="Up", wm=5, resolution_ts=None):
    conn.execute("""INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, token_up_id,
                    token_down_id, open_time_utc, close_time_utc, resolution_time_utc, winner, window_minutes, discovered_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                 (cid, f"{asset.lower()}-updown-{wm}m-{int(open_ts)}", f"{asset} test", asset, f"{cid}_up", f"{cid}_dn",
                  open_ts, open_ts + wm * 60, resolution_ts, winner, wm))


def add_snapshot(conn, cid, token, event_ts, received_ts, best_bid, best_ask, levels=None, source="REST"):
    cur = conn.execute("""INSERT INTO orderbook_snapshots (source_timestamp_utc, received_at_utc_ms, condition_id, token_id,
                          outcome, best_bid, best_ask, source) VALUES (?,?,?,?,?,?,?,?)""",
                       (event_ts, received_ts * 1000.0, cid, token, "Up", best_bid, best_ask, source))
    sid = cur.lastrowid
    for i, (p, s) in enumerate(levels or [], start=1):
        conn.execute("INSERT INTO orderbook_levels (snapshot_id, side, level, price, size) VALUES (?,?,?,?,?)",
                     (sid, "ask", i, p, s))
    return sid


def add_underlying(conn, asset, ts, price, open_ref, cid=None):
    dist = (price - open_ref) / open_ref * 100
    conn.execute("""INSERT INTO underlying_prices (source_timestamp_utc, received_at_utc, asset_symbol, source, price,
                    market_open_reference_price, distance_from_open_abs, distance_from_open_pct, condition_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""", (ts, ts, asset, "test", price, open_ref, price - open_ref, dist, cid))


def add_hist(conn, cid, outcome, side, price, shares, ts, tx="0xh", multiplicity=1, wallet=WALLET):
    for idx in range(1, multiplicity + 1):
        conn.execute("""INSERT INTO leader_trades_v2 (leader_wallet, transaction_hash, condition_id, market_slug, market_title,
                        asset_symbol, token_id, outcome, side, price, shares, usdc_amount, source_timestamp_utc, natural_key,
                        multiplicity_index, multiplicity_total, dedup_ambiguous, origin, run_id, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                     (wallet, tx, cid, "slug", "t", "BTC", f"{cid}_{'up' if outcome == 'Up' else 'dn'}", outcome, side, price,
                      shares, price * shares, ts, f"{tx}|{cid}|{outcome}|{side}|{price}|{shares}|{int(ts)}", idx, multiplicity,
                      1 if multiplicity > 1 else 0, tsr.HISTORY_ORIGIN, "run"))


def add_live(conn, cid, outcome, side, price, shares, ts, tx="0xl", startup=0, method="LIVE", wallet=WALLET):
    conn.execute("""INSERT INTO leader_trades (leader_wallet, transaction_hash, source_timestamp_utc, received_at_utc,
                    is_startup_batch, condition_id, asset_symbol, token_id, outcome, side, price, shares, usdc_amount,
                    raw_payload, collection_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'{}',?)""",
                 (wallet, tx, ts, ts + 180, startup, cid, "BTC", f"{cid}_{'up' if outcome == 'Up' else 'dn'}", outcome, side,
                  price, shares, price * shares, method))


def make_ctx(data_path, paper_path=None, **over):
    args = argparse.Namespace(collector_db=data_path, paper_db=paper_path, output_dir=None, cutoff_ts=over.get("cutoff_ts", T0 + 10 * 86400),
                              leader_wallet=WALLET, snapshot_tolerance_s=2.5, underlying_pad_s=over.get("pad", 0.0),
                              fee_rate=over.get("fee_rate", 0.07), stake_usd=10.0, decision_offset_s=60.0, bootstrap=50, seed=1,
                              no_fills=False, skip_candidates=False)
    return tsr.Ctx(args)


@pytest.fixture
def data_db():
    return _new_db(collector_db.SCHEMA)


@pytest.fixture
def paper_db():
    return _new_db(pdb.SCHEMA, extra_sql=["ALTER TABLE paper_markets ADD COLUMN validation_cohort TEXT NOT NULL DEFAULT 'OFFICIAL'"])


# ------------------------------------------------------------- guarantees ---
def test_source_has_no_network_or_trading_imports():
    src = open(os.path.join(HERE, "..", "trader_strategy_research.py")).read()
    code = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    for pat in (r"^\s*import\s+(requests|websockets|urllib|http\.client|aiohttp|polymarket|polymarket_api|db|live_micro)\b",
                r"^\s*from\s+(requests|websockets|urllib|polymarket|polymarket_api|db|scripts|live_micro)\b",
                r"live_trader", r"PRIVATE_KEY", r"\.env\b", r"init_db", r"ALTER TABLE", r"\bINSERT INTO\b", r"\bUPDATE\s+\w+\s+SET\b", r"\bDELETE FROM\b"):
        assert not re.search(pat, code, flags=re.MULTILINE), f"patron prohibido en el analizador: /{pat}/"


def test_read_only_connection_rejects_writes(data_db):
    ctx = make_ctx(data_db)
    with pytest.raises(sqlite3.OperationalError):
        ctx.data.execute("INSERT INTO collector_events (ts, component, event_type) VALUES (1,'x','y')")


# ------------------------------------------------------------ dedupe ---
def test_history_live_multiset_dedupe(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0)
    # mismo fill en historico y en LIVE -> 1 canonico
    add_hist(c, "m1", "Up", "BUY", 0.5, 10, T0 + 30, tx="0xa")
    add_live(c, "m1", "Up", "BUY", 0.5, 10, T0 + 30, tx="0xa")
    # dos fills legitimos identicos en la MISMA respuesta (multiplicidad 2) y solo 1 visto en LIVE -> 2 canonicos
    add_hist(c, "m1", "Down", "BUY", 0.4, 5, T0 + 40, tx="0xb", multiplicity=2)
    add_live(c, "m1", "Down", "BUY", 0.4, 5, T0 + 40, tx="0xb")
    # fill solo en LIVE (posterior al historico) -> se agrega
    add_live(c, "m1", "Up", "BUY", 0.6, 3, T0 + 50, tx="0xc")
    # startup batch y BACKFILL viejo: excluidos
    add_live(c, "m1", "Up", "BUY", 0.6, 3, T0 + 55, tx="0xd", startup=1)
    add_live(c, "m1", "Up", "BUY", 0.6, 3, T0 + 56, tx="0xe", method="BACKFILL")
    c.commit()
    fills, st = tsr.load_canonical_fills(make_ctx(data_db))
    assert st["canonical_fills"] == 4
    assert st["live_matched_to_history"] == 2
    assert st["live_only"] == 1
    assert st["hist_dedup_ambiguous"] == 2
    assert st["excluded_startup_batch"] == 1 and st["excluded_old_backfill_rows"] == 1
    assert st["old_backfill_not_in_canonical"] == 1
    origins = sorted(f["origin"] for f in fills)
    assert origins == ["HISTORY_V2", "HISTORY_V2", "HISTORY_V2", "LIVE"]
    assert all(f["source_row_id"] is not None and f["source_table"] for f in fills)


# ---------------------------------------------------- inventario BUY/SELL ---
def test_buy_and_sell_inventory_and_pnl_identity(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Up")
    add_live(c, "m1", "Up", "BUY", 0.50, 10, T0 + 30, tx="1")
    add_live(c, "m1", "Up", "SELL", 0.60, 4, T0 + 40, tx="2")
    add_live(c, "m1", "Down", "BUY", 0.30, 2, T0 + 50, tx="3")
    c.commit()
    ctx = make_ctx(data_db, fee_rate=0.0)
    fills, _ = tsr.load_canonical_fills(ctx)
    m = tsr.load_markets(ctx)["m1"]
    seq, cf = tsr.reconstruct_market(ctx, m, fills)
    assert seq["net_up_shares"] == 6 and seq["net_down_shares"] == 2
    assert seq["realized_sell_pnl"] == pytest.approx(0.4)      # (0.60-0.50)*4
    assert seq["matched_shares"] == 2 and seq["surplus_side"] == "Up" and seq["surplus_shares"] == 4
    # gross = payout(6 Up ganan) + proceeds(2.4) - coste BUY total(5 + 0.6) = 6 + 2.4 - 5.6 = 2.8
    assert seq["gross_pnl"] == pytest.approx(2.8)
    assert abs(seq["pnl_identity_residual"]) < 1e-6
    assert [f["tag"] for f in cf] == ["FIRST_LEG", "SELL_REDUCE", "IMBALANCE_REDUCING_BUY"]
    assert seq["n_sell"] == 1 and seq["oversell_events"] == 0


def test_sell_more_than_held_is_flagged_not_negative(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Down")
    add_live(c, "m1", "Up", "BUY", 0.5, 2, T0 + 30, tx="1")
    add_live(c, "m1", "Up", "SELL", 0.5, 5, T0 + 40, tx="2")
    c.commit()
    ctx = make_ctx(data_db)
    fills, _ = tsr.load_canonical_fills(ctx)
    seq, _ = tsr.reconstruct_market(ctx, tsr.load_markets(ctx)["m1"], fills)
    assert seq["oversell_events"] == 1
    assert seq["net_up_shares"] == 0


def test_inventory_vwap_matched_surplus_regression(data_db):
    """Misma ventana real que collector/tests/test_inventory.py (19 fills):
    up 567.67 sh / $139.71, down 432.78 sh / $227.67."""
    FILLS = [("Up", 0.29, 100.00), ("Down", 0.48, 47.00), ("Down", 0.34, 100.00), ("Up", 0.45, 100.00),
             ("Up", 0.40, 73.06), ("Up", 0.37, 11.33), ("Down", 0.48, 10.00), ("Down", 0.53, 21.00),
             ("Down", 0.56, 100.00), ("Up", 0.32, 10.00), ("Up", 0.34, 15.00), ("Down", 0.59, 100.00),
             ("Up", 0.40, 0.76), ("Up", 0.38, 11.00), ("Down", 0.61, 15.00), ("Down", 0.78, 39.78),
             ("Up", 0.14, 46.52), ("Up", 0.06, 100.00), ("Up", 0.07, 100.00)]
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Up")
    for i, (o, p, s) in enumerate(FILLS):
        add_live(c, "m1", o, "BUY", p, s, T0 + 10 + i, tx=str(i))
    c.commit()
    ctx = make_ctx(data_db)
    fills, _ = tsr.load_canonical_fills(ctx)
    seq, cf = tsr.reconstruct_market(ctx, tsr.load_markets(ctx)["m1"], fills)
    assert seq["net_up_shares"] == pytest.approx(567.67, abs=0.01)
    assert seq["net_down_shares"] == pytest.approx(432.78, abs=0.01)
    assert seq["buy_cost_up"] == pytest.approx(139.71, abs=0.05)
    assert seq["buy_cost_down"] == pytest.approx(227.67, abs=0.05)
    assert seq["matched_shares"] == pytest.approx(432.78, abs=0.01)
    assert seq["surplus_side"] == "Up" and seq["surplus_shares"] == pytest.approx(134.89, abs=0.02)
    assert seq["vwap_up"] == pytest.approx(139.71 / 567.67, abs=1e-3)
    assert seq["coverage_ratio"] == pytest.approx(432.78 / 567.67, abs=1e-3)
    assert cf[0]["tag"] == "FIRST_LEG" and cf[1]["tag"] == "IMBALANCE_REDUCING_BUY"
    assert seq["first_leg_side"] == "Up" and seq["seconds_between_legs"] == 1.0


def test_same_second_opposite_sides_are_ordered_deterministically_and_flagged(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Up")
    add_live(c, "m1", "Down", "BUY", 0.4, 5, T0 + 30, tx="b")   # insertado primero
    add_live(c, "m1", "Up", "BUY", 0.5, 5, T0 + 30, tx="a")     # mismo segundo, lado contrario
    c.commit()
    ctx = make_ctx(data_db)
    fills, _ = tsr.load_canonical_fills(ctx)
    seq, cf = tsr.reconstruct_market(ctx, tsr.load_markets(ctx)["m1"], fills)
    assert seq["same_second_opposite_side_ambiguities"] == 1
    assert [f["source_row_id"] for f in cf] == sorted(f["source_row_id"] for f in cf)  # orden por id de fila fuente


# ------------------------------------------------------- anti look-ahead ---
def test_nearest_snapshot_requires_event_and_receipt_before_t(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0)
    t = T0 + 60
    add_snapshot(c, "m1", "m1_up", t - 1.0, t - 1.0, 0.39, 0.40)               # valido
    add_snapshot(c, "m1", "m1_up", t - 0.2, t + 3.0, 0.89, 0.90, source="WS")  # evento antes, recibido DESPUES -> invalido
    add_snapshot(c, "m1", "m1_up", t + 0.1, t + 0.1, 0.79, 0.80)               # futuro cercano -> invalido
    c.commit()
    ctx = make_ctx(data_db)
    s = tsr.nearest_snapshot(ctx.data, "m1", "m1_up", t, 2.5)
    assert s is not None and s["best_ask"] == 0.40


def test_nearest_snapshot_ignores_closer_future_snapshot(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0)
    t = T0 + 60
    add_snapshot(c, "m1", "m1_up", t - 2.0, t - 2.0, 0.39, 0.40)
    add_snapshot(c, "m1", "m1_up", t + 0.05, t + 0.05, 0.89, 0.90)  # mas cercano en |dt| pero futuro
    c.commit()
    ctx = make_ctx(data_db)
    assert tsr.nearest_snapshot(ctx.data, "m1", "m1_up", t, 2.5)["best_ask"] == 0.40
    assert tsr.nearest_snapshot(ctx.data, "m1", "m1_up", t, 1.0) is None  # demasiado viejo para la tolerancia


def test_nearest_underlying_applies_receipt_pad(data_db):
    c = _w(data_db)
    t = T0 + 60
    add_underlying(c, "BTC", t - 0.3, 100.1, 100.0)
    add_underlying(c, "BTC", t - 1.5, 99.9, 100.0)
    c.commit()
    ctx = make_ctx(data_db)
    assert tsr.nearest_underlying(ctx.data, "BTC", t, 2.5, 0.0)["price"] == 100.1
    assert tsr.nearest_underlying(ctx.data, "BTC", t, 2.5, 0.5)["price"] == 99.9  # la de -0.3s no era observable aun


# ------------------------------------------------------------ pendientes ---
def test_pending_market_is_separated_from_resolved(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner=None)
    add_live(c, "m1", "Up", "BUY", 0.5, 10, T0 + 30, tx="1")
    add_market(c, "m2", "BTC", T0 + 300, winner="Up", resolution_ts=T0 + 700)
    add_live(c, "m2", "Up", "BUY", 0.5, 10, T0 + 330, tx="2")
    add_market(c, "m3", "BTC", T0 + 600, winner="Up", resolution_ts=T0 + 999999)  # resuelto DESPUES del corte
    add_live(c, "m3", "Up", "BUY", 0.5, 10, T0 + 630, tx="3")
    c.commit()
    ctx = make_ctx(data_db, cutoff_ts=T0 + 1000)
    fills, _ = tsr.load_canonical_fills(ctx)
    ms = tsr.load_markets(ctx)
    s1, _ = tsr.reconstruct_market(ctx, ms["m1"], [f for f in fills if f["condition_id"] == "m1"])
    s2, _ = tsr.reconstruct_market(ctx, ms["m2"], [f for f in fills if f["condition_id"] == "m2"])
    s3, _ = tsr.reconstruct_market(ctx, ms["m3"], [f for f in fills if f["condition_id"] == "m3"])
    assert s1["resolved_at_cutoff"] == 0 and s1["gross_pnl"] is None and s1["pending_surplus_exposure_usd"] == 5.0
    assert s2["resolved_at_cutoff"] == 1 and s2["gross_pnl"] == pytest.approx(5.0)
    assert s3["resolved_at_cutoff"] == 0  # winner existe en la tabla pero no era conocido al corte


# -------------------------------------------------------- profundidad ---
def test_walk_asks_full_partial_no_depth(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0)
    full = add_snapshot(c, "m1", "m1_up", T0, T0, 0.39, 0.40, levels=[(0.40, 100)])
    partial = add_snapshot(c, "m1", "m1_up", T0, T0, 0.39, 0.40, levels=[(0.40, 5)])
    nodepth = add_snapshot(c, "m1", "m1_up", T0, T0, 0.39, 0.40, levels=None, source="WS_price_change")
    two = add_snapshot(c, "m1", "m1_up", T0, T0, 0.39, 0.40, levels=[(0.40, 10), (0.50, 100)])
    c.commit()
    ctx = make_ctx(data_db)
    assert tsr.walk_asks(ctx.data, full, target_usd=10)["status"] == "FULL"
    p = tsr.walk_asks(ctx.data, partial, target_usd=10)
    assert p["status"] == "PARTIAL" and p["shares"] == 5 and p["usd"] == pytest.approx(2.0)
    assert tsr.walk_asks(ctx.data, nodepth, target_usd=10)["status"] == "NO_DEPTH"
    w = tsr.walk_asks(ctx.data, two, target_shares=20)
    assert w["status"] == "FULL" and w["vwap"] == pytest.approx((10 * 0.40 + 10 * 0.50) / 20)
    s = tsr.walk_asks(ctx.data, full, target_usd=10, slippage_ticks=1)
    assert s["vwap"] == pytest.approx(0.41)


def test_candidate_skips_when_no_depth(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Up")
    add_snapshot(c, "m1", "m1_up", T0 + 59, T0 + 59, 0.59, 0.60, levels=None, source="WS_price_change")
    add_snapshot(c, "m1", "m1_dn", T0 + 59, T0 + 59, 0.39, 0.40, levels=None, source="WS_price_change")
    c.commit()
    ctx = make_ctx(data_db)
    res = tsr.simulate_candidate(ctx, tsr.load_universe(ctx), "FAVORITE", {})
    assert res[0]["status"] == "NO_LIQUIDITY"


# ------------------------------------------------------------------ fees ---
def test_taker_fee_formula_and_candidate_fees():
    assert tsr.taker_fee(100, 0.5, 0.07) == pytest.approx(1.75)  # pico documentado: $1.75 por 100 sh a 0.50
    assert tsr.taker_fee(100, 0.5, 0.0) == 0.0
    assert tsr.taker_fee(None, 0.5, 0.07) == 0.0


def test_candidate_pnl_and_fees_on_synthetic_universe(data_db):
    c = _w(data_db)
    add_market(c, "m1", "BTC", T0, winner="Up")
    t = T0 + 60
    add_snapshot(c, "m1", "m1_up", t - 1, t - 1, 0.59, 0.60, levels=[(0.60, 100)])
    add_snapshot(c, "m1", "m1_dn", t - 1, t - 1, 0.39, 0.40, levels=[(0.40, 100)])
    c.commit()
    ctx = make_ctx(data_db, fee_rate=0.07)
    res = tsr.simulate_candidate(ctx, tsr.load_universe(ctx), "FAVORITE", {})
    r = res[0]
    assert r["status"] == "TRADED" and r["side"] == "Up"
    shares = 10 / 0.60
    assert r["gross_pnl"] == pytest.approx(shares - 10)
    assert r["fees"] == pytest.approx(shares * 0.07 * 0.60 * 0.40)
    agg = tsr.aggregate(res, "FAV", ctx, bootstrap=False)
    assert agg["net_pnl_usd"] == pytest.approx(round(r["gross_pnl"] - r["fees"], 2))
    dog = tsr.simulate_candidate(ctx, tsr.load_universe(ctx), "UNDERDOG", {})[0]
    assert dog["side"] == "Down" and dog["gross_pnl"] == pytest.approx(-10.0)


# ------------------------------------------------- mismos mercados (paper) ---
def test_paper_same_markets_uses_intersection(data_db, paper_db):
    p = _w(paper_db)
    for i, cid in enumerate(("a", "b", "c")):
        p.execute("""INSERT INTO paper_markets (condition_id, market_title, asset_symbol, token_up_id, token_down_id,
                     open_time_utc, close_time_utc, decision_time_utc, discovered_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                  (cid, "t", "BTC", "u", "d", T0 + i * 300, T0 + i * 300 + 300, T0 + i * 300 + 60, T0))
    def dec(cid, strat, did):
        p.execute("""INSERT INTO paper_decisions (id, condition_id, strategy, decision_timestamp_utc, signal_present, side_chosen,
                     executable_price, shares, capital_usd, fill_status, created_at) VALUES (?,?,?,?,1,'Up',0.5,20,10,'FULL',?)""",
                  (did, cid, strat, T0, T0))
        p.execute("""INSERT INTO paper_resolutions (decision_id, condition_id, strategy, winner, resolved_at_utc,
                     capital_deployed_usd, payout_usd, pnl_usd, roi) VALUES (?,?,?,?,?,10,20,10,1.0)""", (did, cid, strat, "Up", T0 + 400))
    dec("a", "MOMENTUM_PURE", 1); dec("b", "MOMENTUM_PURE", 2)
    dec("b", "POLYMARKET_FAVORITE_BASELINE", 3); dec("c", "POLYMARKET_FAVORITE_BASELINE", 4)
    p.commit()
    ctx = make_ctx(data_db, paper_db)
    out = tsr.paper_forward(ctx)
    assert {r["strategy"]: r["n_resolved"] for r in out["summary"]} == {"MOMENTUM_PURE": 2, "POLYMARKET_FAVORITE_BASELINE": 2}
    assert all(r["n_common"] == 1 for r in out["same_markets"])  # solo 'b' en ambas
    assert out["n_hedge_checks_total_COUNT_ONLY"] == 0


# ------------------------------------------------------------- split ---
def test_chronological_split_is_disjoint_and_ordered():
    ms = [{"open_time_utc": T0 + i * 300, "condition_id": str(i)} for i in range(20)]
    tr, va, te = tsr.split_chrono(ms)
    assert len(tr) == 10 and len(va) == 5 and len(te) == 5
    assert max(m["open_time_utc"] for m in tr) < min(m["open_time_utc"] for m in va) < min(m["open_time_utc"] for m in te)
    assert not ({m["condition_id"] for m in tr} & {m["condition_id"] for m in te})


# -------------------------------------------------------- hipotesis ---
def test_hypothesis_stats_helpers():
    assert tsr.binom_two_sided_p(50, 100) == pytest.approx(1.0, abs=0.05)
    assert tsr.binom_two_sided_p(90, 100) < 0.001
    lo, hi = tsr.wilson_ci(50, 100)
    assert lo < 0.5 < hi
    assert tsr.spearman([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0
    assert tsr.spearman([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0


# --------------------------------------------------------- end to end ---
def _build_rich_universe(c, n_per_asset=8):
    """Mercados secuenciales por asset con order book (con niveles) y
    subyacente en el instante de decision; el lider opera la mitad."""
    k = 0
    for ai, asset in enumerate(("BTC", "ETH", "SOL")):
        for i in range(n_per_asset):
            open_ts = T0 + (i * 3 + ai) * 300
            cid = f"{asset}{i}"
            winner = "Up" if (i + ai) % 2 == 0 else "Down"
            add_market(c, cid, asset, open_ts, winner=winner, resolution_ts=open_ts + 400)
            up_ask = 0.55 if winner == "Up" else 0.45
            for dt in range(0, 300, 5):  # book cada 5s desde la apertura (cubre fills a +30/+90 y decision a +60)
                add_snapshot(c, cid, f"{cid}_up", open_ts + dt, open_ts + dt, up_ask - 0.01, up_ask, levels=[(up_ask, 200)])
                add_snapshot(c, cid, f"{cid}_dn", open_ts + dt, open_ts + dt, 0.99 - up_ask, 1.0 - up_ask, levels=[(1.0 - up_ask, 200)])
            for dt in range(0, 300):     # subyacente 1/s como el collector real (tolerancia 2.5s + pad 0.5s)
                add_underlying(c, asset, open_ts + dt, 100 * (1 + (0.001 if winner == "Up" else -0.001)), 100.0, cid)
            if i % 2 == 0:
                add_live(c, cid, "Up", "BUY", up_ask, 20, open_ts + 30, tx=f"t{k}"); k += 1
                add_live(c, cid, "Down", "BUY", 1.0 - up_ask, 10, open_ts + 90, tx=f"t{k}"); k += 1
                if i % 4 == 0:
                    add_live(c, cid, "Up", "SELL", up_ask + 0.02, 5, open_ts + 120, tx=f"t{k}"); k += 1


def test_cli_end_to_end_writes_all_outputs(data_db, paper_db, tmp_path):
    c = _w(data_db)
    _build_rich_universe(c)
    c.execute("INSERT INTO collector_events (ts, component, event_type, detail) VALUES (?, 'orderbook', 'gap', '{}')", (T0 + 100,))
    c.commit()
    rc = tsr.main(["--collector-db", data_db, "--paper-db", paper_db, "--output-dir", str(tmp_path),
                   "--cutoff-ts", str(T0 + 10 * 86400), "--bootstrap", "30", "--seed", "3"])
    assert rc == 0
    for name in ("strategy_research_data_map.md", "trader_behavior_summary.csv", "trader_market_sequences.csv",
                 "hypothesis_tests.csv", "strategy_candidates.csv", "strategy_research.md", "trader_fills_canonical.csv"):
        assert (tmp_path / name).exists(), name
    if tsr.openpyxl is not None:
        assert (tmp_path / "strategy_research.xlsx").exists()
        wb = tsr.openpyxl.load_workbook(tmp_path / "strategy_research.xlsx")
        assert {"Overview", "Trader Summary", "Market Sequences", "Hypotheses", "Candidates", "Forward Paper", "Data Quality"} <= set(wb.sheetnames)
    seqs = list(__import__("csv").DictReader(open(tmp_path / "trader_market_sequences.csv")))
    assert len(seqs) == 12  # el lider opero la mitad de los 24 mercados
    assert all(abs(float(s["pnl_identity_residual"])) < 1e-4 for s in seqs if s["pnl_identity_residual"])
    cands = list(__import__("csv").DictReader(open(tmp_path / "strategy_candidates.csv")))
    labels = {r["candidate"] for r in cands}
    assert {"NO_TRADE", "FAVORITE_1LEG", "UNDERDOG_1LEG", "MOMENTUM_1LEG_T0.02_frozenV1", "MOMENTUM_PARTIAL_HEDGE_paper_rules"} <= labels
    assert all(r["split"] in ("TRAIN", "VALIDATION", "TEST") for r in cands)
    fav_test = next(r for r in cands if r["candidate"] == "FAVORITE_1LEG" and r["split"] == "TEST")
    assert int(fav_test["n_traded"]) > 0                      # las candidatas SI operan cuando hay book en la decision
    assert float(fav_test["win_rate"]) == 1.0                 # en el fixture el favorito siempre gana
    mom_test = next(r for r in cands if r["candidate"] == "MOMENTUM_1LEG_T0.02_frozenV1" and r["split"] == "TEST")
    assert int(mom_test["n_traded"]) > 0 and float(mom_test["win_rate"]) == 1.0  # dist=+/-0.1% alineada con el winner
    hyps_all = list(__import__("csv").DictReader(open(tmp_path / "hypothesis_tests.csv")))
    h3 = next(h for h in hyps_all if h["hypothesis"] == "H3_first_leg_with_momentum_absdist_ge_0.02" and h["group"] == "ALL")
    assert int(h3["n"]) > 0                                   # el subyacente se resolvio en el instante del fill
    hyps = list(__import__("csv").DictReader(open(tmp_path / "hypothesis_tests.csv")))
    h1 = next(h for h in hyps if h["hypothesis"] == "H1_first_leg_is_cheaper_side" and h["group"] == "ALL")
    assert int(h1["n"]) > 0 and h1["verdict"] != "NO DATA"    # el contexto por fill se resolvio (book en el instante del fill)
    h6 = next(h for h in hyps if h["hypothesis"] == "H6_ends_balanced_cov_ge_0.9" and h["group"] == "ALL")
    assert h6["verdict"].startswith("DESCRIPTIVE")            # sin nulo 50/50 natural: nunca se declara 'distinguible'
    detail = list(__import__("csv").DictReader(open(tmp_path / "strategy_candidates_by_asset_day.csv")))
    assert {r["group_type"] for r in detail} >= {"asset", "day", "leader_traded_market"}
    dm = (tmp_path / "strategy_research_data_map.md").read_text()
    assert "leader_trades_v2" in dm and "NO usados a proposito" in dm
    assert "REPORT_CUTOFF_TS" in (tmp_path / "strategy_research.md").read_text()
