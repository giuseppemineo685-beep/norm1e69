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

Precio/profundidad de EJECUCION (DRY_RUN y el gate inicial de staleness):
se re-consulta el snapshot mas fresco disponible AL MOMENTO REAL DEL
INTENTO (now_wall_clock), no el snapshot anclado al instante de decision --
la misma correccion ya aplicada y validada contra datos reales en
live_micro/live_micro_executor.py (ver ese commit): comparar "ahora" contra
un instante fijo del pasado hacia que el chequeo de frescura de 1s casi
nunca pasara, aunque el collector si tuviera datos frescos en tiempo real.

Precio de EJECUCION real en LIVE (2026-09-17): cada envio REAL (hasta
cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET) NO usa el snapshot de data.db como
cotizacion final -- se re-consulta el order book EN VIVO del CLOB
(live_micro_favorite_order_client.get_live_ask_levels(), lectura publica)
inmediatamente antes de cada envio, se calcula el precio ejecutable para el
monto completo, y se compara contra paper_expected_price (la decision
congelada): si se alejo mas de cfg.MAX_PAPER_PRICE_SLIPPAGE, no se envia
nada (no consume cupo) y se detiene esa oportunidad. max_price de cada
envio = min(precio ejecutable en vivo + 1 tick, paper_expected_price +
MAX_PAPER_PRICE_SLIPPAGE). Se detiene en el primer FILLED/PARTIAL, o tras
agotar los 3 intentos sin fill. El snapshot de data.db se sigue usando para
el gate de staleness inicial y como referencia en row["snapshot_id"]/
depth_json, nunca como la cotizacion que arma la orden LIVE.

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


def _walk_live_levels(levels, target_usd):
    """Misma logica que audit_strategy_v1.walk_executable, pero sobre una
    lista de niveles 'ask' ya en memoria (el book EN VIVO recien consultado
    via oc.get_live_ask_levels()), no sobre orderbook_levels en data.db.
    Devuelve (status, shares, usd_gastado, vwap) -- 'FULL' si target_usd se
    llena completo con la profundidad dada, 'PARTIAL' si no alcanza."""
    remaining_usd = target_usd
    shares = 0.0
    spent = 0.0
    for lvl in levels:
        price, size = lvl["price"], lvl["size"]
        if price <= 0 or size <= 0:
            continue
        take_usd = min(remaining_usd, price * size)
        shares += take_usd / price
        spent += take_usd
        remaining_usd -= take_usd
        if remaining_usd <= 1e-9:
            break
    status = "FULL" if remaining_usd <= 1e-9 else "PARTIAL"
    vwap = (spent / shares) if shares > 0 else None
    return status, shares, spent, vwap


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
    row["quantity_usd"] = amount_usd  # objetivo pre-fee; el gasto real (post-fee) queda <= max_spend_usd

    if mode == "DRY_RUN":
        max_price = limit_price
        # 5) limites/kill switch -- ANTES de intentar nada. Se usa MAX_ORDER_USD
        # (el peor caso posible) para la contabilidad de capital desplegado,
        # nunca el monto ya reducido por fees (eso se sabe recien al ejecutar).
        # DRY_RUN es UN solo intento simulado -- ver la rama LIVE mas abajo
        # para el chequeo por-envio (hasta 3 envios REALES independientes).
        ok, limit_reason = ks.check_before_order(state, amount_usd)
        if not ok:
            row["fill_status"] = "SKIPPED_KILL_SWITCH"
            row["reject_reason"] = limit_reason
            return row, False

        api_request = {"asset_id": row["token_id"], "side": "BUY", "amount": amount_usd,
                        "max_spend": max_spend_usd, "max_price": max_price, "order_type": cfg.ORDER_TYPE}
        row["api_request_json"] = _json.dumps(api_request)

        ks.record_attempt(state, amount_usd)  # a partir de aca SI cuenta como operacion intentada

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

    # mode == "LIVE" -- unico camino que puede mover plata real. Hasta
    # cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET envios REALES independientes para
    # ESTA oportunidad -- cada uno re-consulta el book EN VIVO del CLOB
    # (nunca el snapshot de data.db) y se detiene en el primer FILLED/
    # PARTIAL. Cada envio se chequea/cuenta contra el kill switch
    # INDIVIDUALMENTE (a diferencia de DRY_RUN, que es un unico intento
    # simulado) -- ver docs/LIVE_MICRO_FAVORITE.md, seccion "Reintentos LIVE".
    # Nombres de campo de la respuesta (making_amount/taking_amount/status/
    # order_id/transactions_hashes) tomados de 15 respuestas REALES
    # observadas en state/live_trades.jsonl (scripts/live_trader.py, mismo
    # SDK), no adivinados.
    assert cfg.LIVE_MICRO_FAVORITE_ENABLED, "LIVE alcanzado sin LIVE_MICRO_FAVORITE_ENABLED -- esto no debe pasar nunca"
    row["limit_price"] = limit_price  # referencia inicial (snapshot data.db); cada envio pisa esto con su propio max_price
    max_allowed_price = row["paper_expected_price"] + cfg.MAX_PAPER_PRICE_SLIPPAGE
    submissions = []
    final_success = False

    for attempt_n in range(1, cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET + 1):
        # a) kill switch -- ANTES de gastar una consulta al book en vivo.
        ok, limit_reason = ks.check_before_order(state, amount_usd)
        if not ok:
            row["fill_status"] = "SKIPPED_KILL_SWITCH"
            row["reject_reason"] = f"{limit_reason} (intento {attempt_n}/{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET})"
            break

        # b) book EN VIVO del CLOB, re-consultado ahora mismo -- no el
        # snapshot de data.db. Fail closed si no responde o viene vacio.
        live_levels = oc.get_live_ask_levels(row["token_id"])
        if live_levels is None:
            row["fill_status"] = "SKIPPED_LIVE_BOOK_UNAVAILABLE"
            row["reject_reason"] = (f"consulta directa al CLOB fallo o vino vacia "
                                     f"(intento {attempt_n}/{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET})")
            break
        live_status, _live_shares, _live_spent, live_vwap = _walk_live_levels(live_levels, amount_usd)
        if live_vwap is None:
            row["fill_status"] = "SKIPPED_LIVE_DEPTH_INSUFFICIENT"
            row["reject_reason"] = (f"sin profundidad en vivo para ${amount_usd:.2f} "
                                     f"(intento {attempt_n}/{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET})")
            break

        # c) gate de slippage vs. la decision paper congelada -- si el book
        # se alejo demasiado, NO se envia nada (no consume cupo de intentos
        # reales) y se detiene esta oportunidad por completo.
        if live_vwap > max_allowed_price:
            row["fill_status"] = "SKIPPED_PRICE_MOVED_TOO_FAR"
            row["reject_reason"] = (
                f"precio ejecutable en vivo ${live_vwap:.4f} supera paper "
                f"${row['paper_expected_price']:.4f} + ${cfg.MAX_PAPER_PRICE_SLIPPAGE} "
                f"(intento {attempt_n}/{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET}, ninguna orden enviada)")
            break

        tick = meta.get("tick_size") or 0.01
        submit_max_price = min(live_vwap + tick, max_allowed_price)
        row["limit_price"] = submit_max_price

        ks.record_attempt(state, amount_usd)  # a partir de aca SI cuenta como operacion intentada
        sub = {"attempt_n": attempt_n, "live_executable_price": live_vwap,
               "live_book_status": live_status, "max_price": submit_max_price,
               "live_ask_levels": live_levels}
        try:
            resp, latency_ms = oc.place_fak_market_buy(client, row["token_id"], amount_usd, max_spend_usd, submit_max_price)
            sub["latency_ms"] = latency_ms
            sub["response"] = resp
            row["latency_ms"] = latency_ms

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
            row["slippage_pct"] = ((avg_fill_price - submit_max_price) / submit_max_price) if avg_fill_price else None

            if shares_filled <= 0:
                row["fill_status"] = "REJECTED"
                row["reject_reason"] = (f"orden colocada pero sin fill (status={status!r}, FAK sin fill, "
                                         f"intento {attempt_n}/{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET})")
                success = False
            elif spent >= amount_usd * 0.95:  # dentro del 95% del monto objetivo -- lleno para fines practicos
                row["fill_status"] = "FILLED"
                success = True
            else:
                row["fill_status"] = "PARTIAL"
                row["reject_reason"] = f"fill parcial: ${spent:.4f} de ${amount_usd:.2f} objetivo"
                success = True
            ks.record_execution_outcome(state, success)
            sub["fill_status"] = row["fill_status"]
            submissions.append(sub)
            if success:
                final_success = True
                break
            if state["kill_switch_tripped"]:
                break
            # REJECTED confirmado por una respuesta limpia de la API (sin
            # fill, pero SABEMOS que no se lleno) -- unico caso seguro para
            # reintentar. Si quedan intentos, se reintenta re-consultando el
            # book en vivo (punto b) desde cero en la proxima vuelta.
        except Exception as e:
            # Auditoria 2026-09-17 (a87f8e5..4e48fdd, hallazgo #10): una
            # excepcion aca (timeout, conexion cortada, respuesta
            # malformada) NO nos dice si la orden realmente se envio o
            # incluso se lleno del lado del exchange -- solo que NOSOTROS
            # no pudimos confirmarlo. Reintentar en ese caso podria mandar
            # una SEGUNDA orden real mientras la primera sigue con destino
            # desconocido (doble gasto real, hasta 2x MAX_ORDER_USD). Por
            # eso esto SIEMPRE detiene la oportunidad por completo -- nunca
            # reintenta automaticamente tras una excepcion, a diferencia de
            # un REJECTED confirmado (rama de arriba). Requiere revision
            # manual (balance/posiciones reales) antes de cualquier intento
            # nuevo sobre este mismo mercado.
            row["fill_status"] = "ERROR"
            row["error"] = str(e)
            row["reject_reason"] = (
                f"resultado DESCONOCIDO tras excepcion en el intento {attempt_n}/"
                f"{cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET} ({e!r}) -- no se puede confirmar si la orden se "
                f"envio o se lleno del lado del exchange, asi que NO se reintenta automaticamente (evita "
                f"un posible doble envio real). Revisar balance/posiciones manualmente antes de cualquier "
                f"intento nuevo sobre este mercado.")
            sub["error"] = str(e)
            sub["fill_status"] = "ERROR"
            sub["outcome_uncertain"] = True
            ks.record_execution_outcome(state, False)
            submissions.append(sub)
            break  # NUNCA reintentar tras una excepcion -- el resultado real es desconocido

    row["api_request_json"] = _json.dumps({
        "asset_id": row["token_id"], "side": "BUY", "amount": amount_usd, "max_spend": max_spend_usd,
        "order_type": cfg.ORDER_TYPE, "max_paper_price_slippage": cfg.MAX_PAPER_PRICE_SLIPPAGE,
        "max_live_submissions_per_market": cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET})
    row["api_response_json"] = _json.dumps({"submissions": submissions}, default=str)

    last_sub_uncertain = bool(submissions) and submissions[-1].get("outcome_uncertain")
    if (not final_success and not last_sub_uncertain
            and len(submissions) >= cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET
            and row["fill_status"] in ("REJECTED", "ERROR")):
        # Solo aplica cuando se agotaron los 3 intentos con resultados
        # CONFIRMADOS (REJECTED limpio) -- un ultimo intento con resultado
        # incierto ya trae su propio reject_reason ("resultado DESCONOCIDO
        # tras excepcion...") y no debe pisarse con este mensaje generico.
        row["reject_reason"] = (
            f"{row['reject_reason']} -- agotados los {cfg.MAX_LIVE_SUBMISSIONS_PER_MARKET} "
            f"intentos reales sin fill, deteniendo esta oportunidad")

    # attempted=True solo si se envio al menos una orden REAL -- consistente
    # con DRY_RUN (donde SKIPPED_KILL_SWITCH tambien devuelve False): un
    # gate/kill-switch/book-en-vivo-no-disponible que impide cualquier envio
    # no cuenta como "se intento".
    return row, len(submissions) > 0


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
