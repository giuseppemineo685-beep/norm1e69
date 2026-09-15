"""
Validador forward de PAPER TRADING -- simulacion exclusivamente.

SEGURIDAD (leer antes de tocar este archivo):
  - Este modulo NUNCA hace una request HTTP. No importa `requests`, no
    importa `polymarket_api` (que si hace requests, aunque de solo lectura),
    no importa `scripts.live_trader`, no importa ningun cliente CLOB/wallet.
  - collector/data.db se abre EXCLUSIVAMENTE en modo `mode=ro` (URI sqlite
    read-only) -- ninguna escritura es posible ni intentada ahi.
  - Toda escritura va a collector/paper_validation.db (paper_validation_db.py),
    un archivo completamente separado.
  - Ningun private key, API credential ni .env se lee en ningun punto.
  - test_paper_validation.py incluye un escaneo estatico que falla el build
    si aparece cualquiera de estos patrones en este archivo.

Consume datos que el collector en vivo (run_collector.py, proceso aparte)
ya esta escribiendo en data.db en tiempo real -- este proceso NUNCA
recolecta datos el mismo, solo lee y simula decisiones sobre lo que ya
existe.

Tres estrategias, reglas CONGELADAS (no se recalibran con los resultados
de esta validacion):
  MOMENTUM_PURE               -- ver decide_momentum()
  MOMENTUM_PARTIAL_HEDGE      -- decide_momentum() + check_and_maybe_hedge()
  POLYMARKET_FAVORITE_BASELINE -- ver decide_favorite()

Uso:
    python3 run_paper_validation.py
"""
import sqlite3
import sys
import time
from pathlib import Path

import audit_strategy_v1 as aud                # solo lectura: walk_executable()
import backtest_momentum_v1 as bt               # solo lectura: _nearest_snapshot/_nearest_underlying
import backtest_v2_inventory as v2eng           # solo lectura: walk_executable_shares()
import paper_validation_db as pdb

DATA_DB_PATH = str(Path(__file__).resolve().parent / "data.db")

DECISION_OFFSET_S = 60
STAKE_USD = 10.0
MOMENTUM_THRESHOLD_PCT = 0.02     # congelado de V1 -- NO se retoca
CLOSE_BUFFER_S = 60
HEDGE_FRACTION = 0.25
HEDGE_MAX_OPPOSITE_EXEC_PRICE = 0.10
HEDGE_MAX_COMBINED_COST = 0.90
POLL_INTERVAL_S = 5

STRATEGIES = ("MOMENTUM_PURE", "MOMENTUM_PARTIAL_HEDGE", "POLYMARKET_FAVORITE_BASELINE")


def data_conn():
    """SOLO LECTURA. La URI mode=ro hace que SQLite rechace cualquier
    escritura a nivel de driver, no solo por convencion de codigo."""
    conn = sqlite3.connect(f"file:{DATA_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------- estrategias ---
def decide_momentum(dconn, m, decision_ts):
    """MOMENTUM_PURE y la 1ra pierna de MOMENTUM_PARTIAL_HEDGE (identica
    entrada para ambas, por regla). Nunca compra la pierna contraria aqui."""
    under = bt._nearest_underlying(dconn, m["asset_symbol"], decision_ts)
    if under is None or under["distance_from_open_pct"] is None:
        return {"signal_present": 0, "side_chosen": None, "fill_status": "SKIPPED",
                "skip_or_partial_reason": "underlying price not available at decision instant (fail closed)",
                "underlying_open_price": None, "underlying_price_at_60s": None, "distance_pct": None,
                "token_id": None, "snapshot_id": None, "best_ask": None, "executable_price": None,
                "shares": None, "capital_usd": None}

    dist = under["distance_from_open_pct"]
    base = {"underlying_open_price": under["market_open_reference_price"],
            "underlying_price_at_60s": under["price"], "distance_pct": dist}

    if abs(dist) < MOMENTUM_THRESHOLD_PCT:
        return {**base, "signal_present": 0, "side_chosen": None, "fill_status": "NO_TRADE",
                "skip_or_partial_reason": f"|distance|={abs(dist):.4f}% < threshold {MOMENTUM_THRESHOLD_PCT}%",
                "token_id": None, "snapshot_id": None, "best_ask": None, "executable_price": None,
                "shares": None, "capital_usd": None}

    side = "Up" if dist > 0 else "Down"
    token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
    snap = bt._nearest_snapshot(dconn, m["condition_id"], token_id, decision_ts)
    if snap is None or snap["best_ask"] is None:
        return {**base, "signal_present": 1, "side_chosen": side, "token_id": token_id,
                "snapshot_id": None, "best_ask": None, "executable_price": None, "shares": None,
                "capital_usd": None, "fill_status": "SKIPPED",
                "skip_or_partial_reason": "no order book snapshot for chosen side at decision instant (fail closed)"}

    status, shares, spent, vwap = aud.walk_executable(dconn, snap["id"], STAKE_USD)
    reason = {"FULL": None, "PARTIAL": "insufficient depth to fill the full $10 stake",
              "SKIPPED": "snapshot has no recorded depth levels"}[status]
    return {**base, "signal_present": 1, "side_chosen": side, "token_id": token_id,
            "snapshot_id": snap["id"], "best_ask": snap["best_ask"], "executable_price": vwap,
            "shares": shares, "capital_usd": spent, "fill_status": status, "skip_or_partial_reason": reason}


def decide_favorite(dconn, m, decision_ts):
    """POLYMARKET_FAVORITE_BASELINE. NOTA DE INTERPRETACION (documentada
    tambien en docs/PAPER_VALIDATION.md): el enunciado dice 'menor best ask
    / mayor probabilidad implicita', que son opuestos bajo el mecanismo real
    de precios de Polymarket (precio ~= probabilidad, así que MAYOR precio
    = MAYOR probabilidad implicita = favorito). Se resolvio usando 'mayor
    probabilidad implicita' (precio/mid mas alto), consistente con como se
    definio 'favorito' en TODO el resto de esta sesion (V1/V2/V3)."""
    snap_up = bt._nearest_snapshot(dconn, m["condition_id"], m["token_up_id"], decision_ts)
    snap_down = bt._nearest_snapshot(dconn, m["condition_id"], m["token_down_id"], decision_ts)
    if snap_up is None or snap_down is None or snap_up["best_ask"] is None or snap_down["best_ask"] is None:
        return {"signal_present": 0, "side_chosen": None, "fill_status": "SKIPPED",
                "skip_or_partial_reason": "missing order book for Up or Down at decision instant (fail closed)",
                "underlying_open_price": None, "underlying_price_at_60s": None, "distance_pct": None,
                "token_id": None, "snapshot_id": None, "best_ask": None, "executable_price": None,
                "shares": None, "capital_usd": None}

    mid_up = (snap_up["best_bid"] + snap_up["best_ask"]) / 2 if snap_up["best_bid"] is not None else snap_up["best_ask"]
    mid_down = (snap_down["best_bid"] + snap_down["best_ask"]) / 2 if snap_down["best_bid"] is not None else snap_down["best_ask"]
    base = {"underlying_open_price": None, "underlying_price_at_60s": None, "distance_pct": None}

    if mid_up == mid_down:
        return {**base, "signal_present": 0, "side_chosen": None, "fill_status": "NO_TRADE",
                "skip_or_partial_reason": "tie between Up and Down implied probability",
                "token_id": None, "snapshot_id": None, "best_ask": None, "executable_price": None,
                "shares": None, "capital_usd": None}

    side = "Up" if mid_up > mid_down else "Down"
    token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
    snap = snap_up if side == "Up" else snap_down
    status, shares, spent, vwap = aud.walk_executable(dconn, snap["id"], STAKE_USD)
    reason = {"FULL": None, "PARTIAL": "insufficient depth to fill the full $10 stake",
              "SKIPPED": "snapshot has no recorded depth levels"}[status]
    return {**base, "signal_present": 1, "side_chosen": side, "token_id": token_id,
            "snapshot_id": snap["id"], "best_ask": snap["best_ask"], "executable_price": vwap,
            "shares": shares, "capital_usd": spent, "fill_status": status, "skip_or_partial_reason": reason}


DECIDERS = {
    "MOMENTUM_PURE": decide_momentum,
    "MOMENTUM_PARTIAL_HEDGE": decide_momentum,
    "POLYMARKET_FAVORITE_BASELINE": decide_favorite,
}


# ----------------------------------------------------------------- ciclo ---
def discover_markets(dconn, pconn, cutoff_ts):
    known = {r["condition_id"] for r in pconn.execute("SELECT condition_id FROM paper_markets").fetchall()}
    rows = dconn.execute("""
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc
        FROM markets
        WHERE asset_symbol IN ('BTC','ETH','SOL') AND window_minutes=5
          AND open_time_utc IS NOT NULL AND close_time_utc IS NOT NULL
          AND open_time_utc >= ?
    """, (cutoff_ts,)).fetchall()
    n_new = 0
    now = time.time()
    for m in rows:
        if m["condition_id"] in known:
            continue
        pconn.execute("""INSERT OR IGNORE INTO paper_markets
            (condition_id, market_title, asset_symbol, token_up_id, token_down_id,
             open_time_utc, close_time_utc, decision_time_utc, discovered_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (m["condition_id"], m["market_title"], m["asset_symbol"], m["token_up_id"], m["token_down_id"],
             m["open_time_utc"], m["close_time_utc"], m["open_time_utc"] + DECISION_OFFSET_S, now))
        n_new += 1
    return n_new


def make_decisions(dconn, pconn, m):
    decision_ts = m["decision_time_utc"]
    now = time.time()
    for strategy in STRATEGIES:
        already = pconn.execute("SELECT 1 FROM paper_decisions WHERE condition_id=? AND strategy=?",
                                 (m["condition_id"], strategy)).fetchone()
        if already:
            continue
        result = DECIDERS[strategy](dconn, m, decision_ts)
        pconn.execute("""
            INSERT OR IGNORE INTO paper_decisions
              (condition_id, strategy, decision_timestamp_utc, asset_symbol,
               underlying_open_price, underlying_price_at_60s, distance_pct, signal_present,
               side_chosen, token_id, snapshot_id, best_ask, executable_price, shares,
               capital_usd, fill_status, skip_or_partial_reason, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (m["condition_id"], strategy, decision_ts, m["asset_symbol"],
              result.get("underlying_open_price"), result.get("underlying_price_at_60s"),
              result.get("distance_pct"), result["signal_present"], result.get("side_chosen"),
              result.get("token_id"), result.get("snapshot_id"), result.get("best_ask"),
              result.get("executable_price"), result.get("shares"), result.get("capital_usd"),
              result["fill_status"], result.get("skip_or_partial_reason"), now))
    pdb.log_event("run_paper_validation", "decided", {"condition_id": m["condition_id"]})


def check_and_maybe_hedge(dconn, pconn, decision_row, now_ts):
    condition_id = decision_row["condition_id"]
    market_row = pconn.execute("SELECT * FROM paper_markets WHERE condition_id=?", (condition_id,)).fetchone()
    seconds_to_close = market_row["close_time_utc"] - now_ts
    cond_time_ok = seconds_to_close >= CLOSE_BUFFER_S

    side = decision_row["side_chosen"]
    other_token = market_row["token_down_id"] if side == "Up" else market_row["token_up_id"]
    own_vwap = decision_row["executable_price"]
    target_shares = (decision_row["shares"] or 0.0) * HEDGE_FRACTION

    if not cond_time_ok or own_vwap is None or target_shares <= 0:
        pconn.execute("""INSERT INTO paper_hedge_checks
            (decision_id, condition_id, checked_at_utc, opposite_token_id, opposite_snapshot_id,
             opposite_best_ask, opposite_executable_price, combined_cost, depth_sufficient,
             seconds_to_close, cond_price_le_010, cond_combined_le_090, cond_depth_ok, cond_time_ok,
             all_conditions_met, hedge_executed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (decision_row["id"], condition_id, now_ts, other_token, None, None, None, None, 0,
             seconds_to_close, 0, 0, 0, 1 if cond_time_ok else 0, 0))
        return

    snap = bt._nearest_snapshot(dconn, condition_id, other_token, now_ts)
    if snap is None or snap["best_ask"] is None:
        pconn.execute("""INSERT INTO paper_hedge_checks
            (decision_id, condition_id, checked_at_utc, opposite_token_id, opposite_snapshot_id,
             opposite_best_ask, opposite_executable_price, combined_cost, depth_sufficient,
             seconds_to_close, cond_price_le_010, cond_combined_le_090, cond_depth_ok, cond_time_ok,
             all_conditions_met, hedge_executed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (decision_row["id"], condition_id, now_ts, other_token, None, None, None, None, 0,
             seconds_to_close, 0, 0, 0, 1, 0))
        return

    status, shares_walked, usd_spent, vwap_opp = v2eng.walk_executable_shares(dconn, snap["id"], target_shares)
    depth_sufficient = status == "FULL"
    combined = (own_vwap + vwap_opp) if vwap_opp is not None else None
    cond_price = vwap_opp is not None and vwap_opp <= HEDGE_MAX_OPPOSITE_EXEC_PRICE
    cond_combined = combined is not None and combined <= HEDGE_MAX_COMBINED_COST
    all_met = cond_price and cond_combined and depth_sufficient and cond_time_ok

    cur = pconn.execute("""INSERT INTO paper_hedge_checks
        (decision_id, condition_id, checked_at_utc, opposite_token_id, opposite_snapshot_id,
         opposite_best_ask, opposite_executable_price, combined_cost, depth_sufficient,
         seconds_to_close, cond_price_le_010, cond_combined_le_090, cond_depth_ok, cond_time_ok,
         all_conditions_met, hedge_executed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (decision_row["id"], condition_id, now_ts, other_token, snap["id"], snap["best_ask"],
         vwap_opp, combined, 1 if depth_sufficient else 0, seconds_to_close,
         1 if cond_price else 0, 1 if cond_combined else 0, 1 if depth_sufficient else 0,
         1 if cond_time_ok else 0, 1 if all_met else 0, 1 if all_met else 0))

    if all_met:
        pconn.execute("""INSERT INTO paper_hedge_fills
            (decision_id, condition_id, executed_at_utc, target_shares, filled_shares, usd_spent,
             vwap, fill_status, snapshot_id)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (decision_row["id"], condition_id, now_ts, target_shares, shares_walked, usd_spent,
             vwap_opp, status, snap["id"]))
        pdb.log_event("run_paper_validation", "hedge_executed",
                      {"condition_id": condition_id, "combined_cost": combined, "shares": shares_walked})


def resolve_pending(dconn, pconn, now):
    """Solo resuelve decisiones con una posicion real (side_chosen + FULL/PARTIAL) --
    SKIPPED/NO_TRADE nunca generan una fila de resolucion (0 capital, nada que resolver)."""
    candidates = pconn.execute("""
        SELECT pd.*, pm.close_time_utc FROM paper_decisions pd
        JOIN paper_markets pm ON pm.condition_id = pd.condition_id
        WHERE pm.close_time_utc <= ?
          AND pd.side_chosen IS NOT NULL AND pd.fill_status IN ('FULL','PARTIAL')
          AND NOT EXISTS (SELECT 1 FROM paper_resolutions pr WHERE pr.decision_id = pd.id)
    """, (now,)).fetchall()
    n_resolved = 0
    winners_cache = {}
    for d in candidates:
        cid = d["condition_id"]
        if cid not in winners_cache:
            row = dconn.execute("SELECT winner FROM markets WHERE condition_id=?", (cid,)).fetchone()
            winners_cache[cid] = row["winner"] if row else None
        winner = winners_cache[cid]
        if winner not in ("Up", "Down"):
            continue  # todavia no resuelto en data.db -- se reintenta en el proximo ciclo
        capital = d["capital_usd"] or 0.0
        payout = (d["shares"] or 0.0) * 1.0 if d["side_chosen"] == winner else 0.0
        hedge = pconn.execute("SELECT * FROM paper_hedge_fills WHERE decision_id=?", (d["id"],)).fetchone()
        if hedge:
            hedge_side = "Down" if d["side_chosen"] == "Up" else "Up"
            capital += hedge["usd_spent"]
            payout += hedge["filled_shares"] * 1.0 if hedge_side == winner else 0.0
        pnl = payout - capital
        roi = (pnl / capital) if capital > 0 else None
        pconn.execute("""INSERT INTO paper_resolutions
            (decision_id, condition_id, strategy, winner, resolved_at_utc,
             capital_deployed_usd, payout_usd, pnl_usd, roi)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (d["id"], cid, d["strategy"], winner, now, capital, payout, pnl, roi))
        n_resolved += 1
    if n_resolved:
        pdb.log_event("run_paper_validation", "resolved", {"n": n_resolved})
    return n_resolved


def cycle(cutoff_ts):
    dconn = data_conn()
    try:
        now = time.time()
        with pdb.connect() as pconn:
            n_new = discover_markets(dconn, pconn, cutoff_ts)

            pending = pconn.execute("""
                SELECT pm.* FROM paper_markets pm
                WHERE pm.decision_time_utc <= ? AND NOT EXISTS (
                    SELECT 1 FROM paper_decisions pd WHERE pd.condition_id=pm.condition_id
                )
            """, (now,)).fetchall()
            for m in pending:
                make_decisions(dconn, pconn, m)

            hedge_candidates = pconn.execute("""
                SELECT pd.* FROM paper_decisions pd
                WHERE pd.strategy='MOMENTUM_PARTIAL_HEDGE' AND pd.side_chosen IS NOT NULL
                  AND pd.fill_status IN ('FULL','PARTIAL')
                  AND NOT EXISTS (SELECT 1 FROM paper_hedge_fills phf WHERE phf.decision_id=pd.id)
            """).fetchall()
            for d in hedge_candidates:
                check_and_maybe_hedge(dconn, pconn, d, now)

            resolve_pending(dconn, pconn, now)

        if n_new or pending or hedge_candidates:
            print(f"[paper_validation] {time.strftime('%H:%M:%S', time.gmtime())} UTC -- "
                  f"nuevos={n_new} decisiones_tomadas={len(pending)} coberturas_chequeadas={len(hedge_candidates)}",
                  flush=True)
    finally:
        dconn.close()


def run():
    pdb.init_db()
    cutoff_ts = float(pdb.set_meta_if_absent("validator_started_at", repr(time.time())))
    pdb.log_event("run_paper_validation", "start", {"cutoff_ts": cutoff_ts})
    print(f"paper validator iniciado -- SOLO SIMULACION. cutoff (mercados con open_time_utc >= esto "
          f"cuentan como 'datos completamente nuevos'): {cutoff_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(cutoff_ts))} UTC)",
          flush=True)
    while True:
        try:
            cycle(cutoff_ts)
        except Exception as e:
            pdb.log_event("run_paper_validation", "error", {"error": str(e)})
            print(f"[paper_validation] error: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    run()
