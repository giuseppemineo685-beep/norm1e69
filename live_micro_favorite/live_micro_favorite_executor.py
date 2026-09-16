"""
Nucleo del ejecutor micro-LIVE para POLYMARKET_FAVORITE_BASELINE: descubre
mercados candidatos, reusa EXACTAMENTE decide_favorite() del paper validator
(import directo, no reimplementado) para generar la decision "paper" (lo que
el paper registraria a stake $10), y en paralelo intenta la misma direccion
a escala $2 -- DRY_RUN simula el fill via el mismo motor de profundidad que
ya usa el paper validator; LIVE (solo si
live_micro_favorite_config.LIVE_MICRO_FAVORITE_ENABLED) llama a
live_micro_favorite_order_client.place_fak_market_buy() (amount+max_spend+
max_price, NO limit order por shares -- ver ese archivo para la
justificacion completa), la UNICA funcion de todo este modulo que puede
mover plata real.

Precio/profundidad de EJECUCION: se re-consulta el snapshot mas fresco
disponible AL MOMENTO REAL DEL INTENTO (now_wall_clock), no el snapshot
anclado al instante de decision -- la misma correccion ya aplicada y
validada contra datos reales en live_micro/live_micro_executor.py (ver ese
commit): comparar "ahora" contra un instante fijo del pasado hacia que el
chequeo de frescura de 1s casi nunca pasara, aunque el collector si tuviera
datos frescos en tiempo real.

No importa scripts.live_trader en ningun punto. No importa `polymarket`
directamente (eso vive solo en live_micro_favorite_order_client.py). Este
paquete es independiente de live_micro/ (el ejecutor momentum) -- cero
imports cruzados entre los dos, solo comparten los modulos de SOLO LECTURA
ya existentes en collector/.
"""
import json as _json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))

import audit_strategy_v1 as aud                       # solo lectura: walk_executable() (vive en collector/)
import backtest_momentum_v1 as bt                      # solo lectura: _nearest_snapshot() contra "ahora" (vive en collector/)
import export_paper_validation as epv                  # solo lectura: estimated_taker_fee_usd() (formula de fee verificada, vive en collector/)
import run_paper_validation as pv                      # decide_favorite() + reglas congeladas del paper validator (vive en collector/)

import live_micro_favorite_config as cfg
import live_micro_favorite_db as ldb
import live_micro_favorite_kill_switch as ks
import live_micro_favorite_order_client as oc


def data_conn():
    """SOLO LECTURA, identico patron al paper validator: mode=ro rechaza
    escrituras a nivel de driver sqlite."""
    return pv.data_conn()


def select_candidate_markets(dconn, cutoff_ts, now, already_seen):
    rows = dconn.execute("""
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc
        FROM markets
        WHERE asset_symbol IN ('BTC','ETH','SOL') AND window_minutes=5
          AND open_time_utc IS NOT NULL AND close_time_utc IS NOT NULL
          AND open_time_utc >= ?
          AND (open_time_utc + ?) <= ?
        ORDER BY open_time_utc ASC
    """, (cutoff_ts, pv.DECISION_OFFSET_S, now)).fetchall()
    return [m for m in rows if m["condition_id"] not in already_seen]


def _snapshot_and_depth(dconn, snapshot_id):
    levels = dconn.execute(
        "SELECT level, price, size FROM orderbook_levels WHERE snapshot_id=? AND side='ask' ORDER BY level",
        (snapshot_id,)).fetchall()
    return [{"level": l["level"], "price": l["price"], "size": l["size"]} for l in levels]


def build_attempt(dconn, m, now_wall_clock, state, mode, client=None):
    """Procesa UN mercado candidato de principio a fin y devuelve el dict
    listo para live_micro_favorite_db.insert_attempt(). No inserta nada el
    mismo (el llamador decide cuando persistir)."""
    decision_ts = m["open_time_utc"] + pv.DECISION_OFFSET_S
    row = {
        "created_at": now_wall_clock, "mode": mode, "condition_id": m["condition_id"],
        "asset_symbol": m["asset_symbol"], "decision_time_utc": decision_ts,
        "paper_side": None, "paper_expected_price": None, "paper_fill_status": None,
        "side": None, "token_id": None, "depth_json": None,
        "snapshot_id": None, "snapshot_received_at_utc_ms": None, "snapshot_age_s": None,
        "min_order_size_shares": None, "limit_price": None, "quantity_shares": None,
        "quantity_usd": None, "api_request_json": None, "api_response_json": None,
        "fill_status": None, "shares_filled": None, "avg_fill_price": None,
        "fee_real_usd": None, "fee_estimated_usd": None, "latency_ms": None, "slippage_pct": None,
        "tx_or_order_id": None, "reject_reason": None, "error": None,
    }

    # 1) la MISMA decision congelada que POLYMARKET_FAVORITE_BASELINE en
    # paper (decide_favorite importada, no reescrita) -- "paper y LIVE
    # registrados en paralelo": esta fila SIEMPRE lleva ambas cosas juntas.
    paper = pv.decide_favorite(dconn, m, decision_ts)
    row["paper_side"] = paper.get("side_chosen")
    row["paper_expected_price"] = paper.get("executable_price")
    row["paper_fill_status"] = paper["fill_status"]
    if paper["signal_present"] != 1 or paper["fill_status"] not in ("FULL", "PARTIAL"):
        row["fill_status"] = "NO_PAPER_SIGNAL"
        row["reject_reason"] = paper.get("skip_or_partial_reason") or "sin decision FAVORITE_BASELINE (empate o datos insuficientes)"
        return row, False  # False = no se intento ninguna orden

    row["side"] = paper["side_chosen"]
    row["token_id"] = paper["token_id"]

    # 2) precio/profundidad de EJECUCION: snapshot mas fresco AHORA, no el
    # anclado al instante de decision congelado (ver docstring del modulo).
    fresh_snap = bt._nearest_snapshot(dconn, m["condition_id"], row["token_id"], now_wall_clock)
    if fresh_snap is None or fresh_snap["best_ask"] is None or fresh_snap["received_at_utc_ms"] is None:
        row["fill_status"] = "SKIPPED_STALE_SNAPSHOT"
        row["reject_reason"] = "sin snapshot ejecutable dentro de la tolerancia al momento del intento (fail closed)"
        return row, False
    row["snapshot_id"] = fresh_snap["id"]
    row["depth_json"] = _json.dumps(_snapshot_and_depth(dconn, fresh_snap["id"]))

    snapshot_age_s = now_wall_clock - (fresh_snap["received_at_utc_ms"] / 1000.0)
    row["snapshot_received_at_utc_ms"] = fresh_snap["received_at_utc_ms"]
    row["snapshot_age_s"] = snapshot_age_s
    if snapshot_age_s > cfg.MAX_SNAPSHOT_AGE_S or snapshot_age_s < 0:
        row["fill_status"] = "SKIPPED_STALE_SNAPSHOT"
        row["reject_reason"] = f"snapshot_age_s={snapshot_age_s:.3f} fuera de [0, {cfg.MAX_SNAPSHOT_AGE_S}]"
        return row, False

    limit_price = fresh_snap["best_ask"] + cfg.LIMIT_PRICE_BUFFER
    row["limit_price"] = limit_price

    # 3) minimum_order_size/tick_size REAL del mercado (lectura publica, sin
    # credenciales) -- SOLO INFORMATIVO desde la correccion del 2026-09-16:
    # verificado (15 fills LIVE reales de scripts/live_trader.py, 2 con
    # menos de 5 shares, en mercados que hoy siguen reportando
    # minimum_order_size=5) que este minimo NO aplica a
    # place_market_order(amount=...) -- solo a limit orders por shares.
    # Sigue fail-closed si el endpoint no responde (no tenemos info del
    # mercado en absoluto), pero ya NO rechaza por el valor del minimo.
    meta = oc.get_market_meta(m["condition_id"])
    if not meta or meta.get("error"):
        row["fill_status"] = "SKIPPED_META_UNAVAILABLE"
        row["reject_reason"] = f"no se pudo obtener metadata del mercado: {meta.get('error') if meta else 'sin respuesta'}"
        return row, False
    row["min_order_size_shares"] = meta.get("minimum_order_size")

    # 4) sizing: amount-based FAK, protegido por max_price Y max_spend
    # (all-in, incluye fees) -- ambos se codifican en maker_amount/
    # taker_amount de la orden FIRMADA por el SDK, no son solo un chequeo
    # previo. Ver live_micro_favorite_order_client.py y
    # docs/LIVE_MICRO_FAVORITE.md, seccion "Verificacion del mecanismo de
    # proteccion de precio real".
    amount_usd = cfg.MAX_ORDER_USD
    max_spend_usd = cfg.MAX_ORDER_USD  # tope all-in, incluye fees -- el SDK reduce amount si hace falta
    max_price = limit_price
    row["quantity_usd"] = amount_usd  # objetivo pre-fee; el gasto real (post-fee) queda <= max_spend_usd

    # 5) limites/kill switch -- ANTES de intentar nada. Se usa MAX_ORDER_USD
    # (el peor caso posible) para la contabilidad de capital desplegado,
    # nunca el monto ya reducido por fees (eso se sabe recien al ejecutar).
    ok, limit_reason = ks.check_before_order(state, amount_usd)
    if not ok:
        row["fill_status"] = "SKIPPED_KILL_SWITCH"
        row["reject_reason"] = limit_reason
        return row, False

    api_request = {"asset_id": row["token_id"], "side": "BUY", "amount": amount_usd,
                    "max_spend": max_spend_usd, "max_price": max_price, "order_type": cfg.ORDER_TYPE}
    row["api_request_json"] = _json.dumps(api_request)

    ks.record_attempt(state, amount_usd)  # a partir de aca SI cuenta como operacion intentada

    if mode == "DRY_RUN":
        t0 = time.monotonic()
        # resolved_amount: replica offline la MISMA reduccion por fee que
        # aplicaria el SDK real (adjust_buy_amount_for_fees), con la formula
        # de fee ya verificada (0.07 * shares * price * (1-price), rate=0.07
        # exponente=1) -- para que la simulacion sea fiel a lo que LIVE
        # gastaria realmente, no solo a los $2 nominales.
        effective_rate = epv.TAKER_FEE_RATE_CRYPTO * max_price * (1 - max_price)
        resolved_amount = max_spend_usd / (1 + effective_rate / max_price)
        sim_status, sim_shares, sim_spent, sim_vwap = aud.walk_executable(dconn, row["snapshot_id"], resolved_amount)
        latency_ms = (time.monotonic() - t0) * 1000.0
        row["api_response_json"] = _json.dumps({
            "simulated": True,
            "note": "DRY_RUN -- ningun endpoint de ordenes fue invocado, ningun cliente/credencial fue cargado",
            "resolved_amount_post_fee_usd": float(resolved_amount), "sim_status": sim_status})
        row["fill_status"] = "SIMULATED_DRY_RUN"
        row["shares_filled"] = sim_shares if sim_status != "SKIPPED" else 0.0
        row["avg_fill_price"] = sim_vwap
        row["latency_ms"] = latency_ms
        row["slippage_pct"] = ((sim_vwap - max_price) / max_price) if sim_vwap else None
        row["tx_or_order_id"] = None
        row["fee_real_usd"] = None  # nunca inventado
        if sim_vwap is not None and row["shares_filled"]:
            row["fee_estimated_usd"] = epv.estimated_taker_fee_usd(row["shares_filled"], sim_vwap)
        success = sim_status in ("FULL", "PARTIAL")
        if not success:
            row["reject_reason"] = "simulacion: profundidad insuficiente al precio limite (SKIPPED)"
        ks.record_execution_outcome(state, success)
        return row, True

    # mode == "LIVE" -- unico camino que puede mover plata real.
    # Nombres de campo (making_amount/taking_amount/status/order_id/
    # transactions_hashes) tomados de 15 respuestas REALES observadas en
    # state/live_trades.jsonl (scripts/live_trader.py, mismo SDK), no
    # adivinados -- ver docs/LIVE_MICRO_FAVORITE.md.
    assert cfg.LIVE_MICRO_FAVORITE_ENABLED, "LIVE alcanzado sin LIVE_MICRO_FAVORITE_ENABLED -- esto no debe pasar nunca"
    try:
        resp, latency_ms = oc.place_fak_market_buy(client, row["token_id"], amount_usd, max_spend_usd, max_price)
        row["latency_ms"] = latency_ms
        row["api_response_json"] = _json.dumps(resp, default=str)

        making_amount = getattr(resp, "making_amount", None)   # USDC efectivamente gastado
        taking_amount = getattr(resp, "taking_amount", None)   # shares efectivamente recibidas
        status = getattr(resp, "status", None)
        order_id = getattr(resp, "order_id", None)
        tx_hashes = getattr(resp, "transactions_hashes", None)

        spent = float(making_amount) if making_amount is not None else 0.0
        shares_filled = float(taking_amount) if taking_amount is not None else 0.0
        avg_fill_price = (spent / shares_filled) if shares_filled else None

        row["shares_filled"] = shares_filled
        row["avg_fill_price"] = avg_fill_price
        row["tx_or_order_id"] = order_id or (tx_hashes[0] if tx_hashes else None)
        row["fee_real_usd"] = None  # la API no informa fee por separado en las respuestas observadas -- nunca inventado
        row["slippage_pct"] = ((avg_fill_price - max_price) / max_price) if avg_fill_price else None

        if shares_filled <= 0:
            row["fill_status"] = "REJECTED"
            row["reject_reason"] = f"orden colocada pero sin fill (status={status!r}, FAK sin fill)"
            success = False
        elif spent >= amount_usd * 0.95:  # dentro del 95% del monto objetivo -- lleno para fines practicos
            row["fill_status"] = "FILLED"
            success = True
        else:
            row["fill_status"] = "PARTIAL"
            row["reject_reason"] = f"fill parcial: ${spent:.4f} de ${amount_usd:.2f} objetivo"
            success = True
        ks.record_execution_outcome(state, success)
    except Exception as e:
        row["fill_status"] = "ERROR"
        row["error"] = str(e)
        ks.record_execution_outcome(state, False)
    return row, True


def resolve_and_update_pnl(dconn, state):
    """Solo resuelve intentos con posicion real (shares_filled>0) cuyo
    mercado ya cerro y tiene winner conocido en data.db. Costo REAL de lo
    efectivamente llenado (shares_filled*avg_fill_price), nunca el tamaño
    intentado -- con un fill PARCIAL, cobrar el costo intentado completo
    sobreestimaria la perdida contra el kill switch de -$10."""
    now = time.time()
    n_resolved = 0
    for a in ldb.unresolved_filled_attempts():
        row = dconn.execute("SELECT close_time_utc, winner FROM markets WHERE condition_id=?",
                             (a["condition_id"],)).fetchone()
        if row is None or row["winner"] not in ("Up", "Down") or row["close_time_utc"] > now:
            continue
        winner = row["winner"]
        actual_cost = (a["shares_filled"] or 0.0) * (a["avg_fill_price"] or 0.0)
        payout = a["shares_filled"] if a["side"] == winner else 0.0
        pnl = payout - actual_cost
        ldb.mark_resolved(a["id"], winner, payout, pnl, now)
        ks.record_realized_pnl(state, pnl)
        n_resolved += 1
    return n_resolved
