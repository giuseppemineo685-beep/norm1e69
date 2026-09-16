"""
Logica pura de limites y kill switch para el ejecutor FAVORITE_BASELINE --
sin I/O, sin red, sin DB. Mismo diseño que live_micro/live_micro_kill_switch.py
(momentum), reimplementado aca para que este paquete sea completamente
independiente -- ningun import cruzado entre los dos ejecutores nuevos.

No incluye sizing por shares (a diferencia de una version anterior): se
verifico que minimum_order_size (en shares) no aplica a
place_market_order(amount=...) -- el sizing ahora es directo en dolares
(amount/max_spend/max_price), ver live_micro_favorite_executor.py.
"""
import live_micro_favorite_config as cfg


def fresh_state():
    return {
        "n_orders_attempted": 0,
        "consecutive_failures": 0,
        "capital_deployed_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "kill_switch_tripped": False,
        "kill_switch_reason": None,
    }


def check_before_order(state, order_usd):
    if state["kill_switch_tripped"]:
        return False, f"kill switch ya activado: {state['kill_switch_reason']}"
    if state["n_orders_attempted"] >= cfg.MAX_ORDERS:
        return False, f"limite de operaciones alcanzado ({cfg.MAX_ORDERS})"
    if state["consecutive_failures"] >= cfg.MAX_CONSECUTIVE_FAILURES:
        return False, f"{cfg.MAX_CONSECUTIVE_FAILURES} ordenes fallidas consecutivas"
    if state["capital_deployed_usd"] + order_usd > cfg.MAX_CAPITAL_DEPLOYED_USD + 1e-9:
        return False, (f"capital desplegado ${state['capital_deployed_usd']:.2f} + orden "
                        f"${order_usd:.2f} superaria el tope ${cfg.MAX_CAPITAL_DEPLOYED_USD:.2f}")
    if state["realized_pnl_usd"] <= -cfg.KILL_SWITCH_LOSS_USD:
        return False, (f"perdida acumulada realizada ${-state['realized_pnl_usd']:.2f} "
                        f">= kill switch ${cfg.KILL_SWITCH_LOSS_USD:.2f}")
    return True, None


def record_attempt(state, order_usd):
    state["n_orders_attempted"] += 1
    state["capital_deployed_usd"] += order_usd


def record_execution_outcome(state, success):
    """Unica entrada del circuit breaker de N fallos consecutivos -- sin
    relacion con si la posicion despues gana o pierde plata."""
    if success:
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1

    if state["consecutive_failures"] >= cfg.MAX_CONSECUTIVE_FAILURES:
        state["kill_switch_tripped"] = True
        state["kill_switch_reason"] = (
            f"{state['consecutive_failures']} ordenes fallidas consecutivas")
    elif state["n_orders_attempted"] >= cfg.MAX_ORDERS:
        state["kill_switch_tripped"] = True
        state["kill_switch_reason"] = f"limite de {cfg.MAX_ORDERS} operaciones alcanzado"


def record_realized_pnl(state, pnl_delta):
    """Unica entrada del kill switch de perdida acumulada -- independiente
    de exito/fallo de ejecucion."""
    state["realized_pnl_usd"] += pnl_delta
    if state["realized_pnl_usd"] <= -cfg.KILL_SWITCH_LOSS_USD:
        state["kill_switch_tripped"] = True
        state["kill_switch_reason"] = (
            f"perdida acumulada realizada ${-state['realized_pnl_usd']:.2f} "
            f">= ${cfg.KILL_SWITCH_LOSS_USD:.2f}")
