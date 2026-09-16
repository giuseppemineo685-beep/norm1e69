"""
Punto de entrada del ejecutor micro-LIVE para POLYMARKET_FAVORITE_BASELINE.
SIEMPRE arranca en DRY_RUN salvo que se cumplan LAS DOS condiciones a la vez
(ver live_micro_favorite_config.py):
  LIVE_MICRO_FAVORITE=1
  LIVE_MICRO_FAVORITE_CONFIRM=I_UNDERSTAND_REAL_MONEY

No reutiliza ni importa scripts/live_trader.py ni su workflow. No importa ni
comparte estado con live_micro/ (el ejecutor momentum) -- proceso NUEVO Y
SEPARADO, puede correr en paralelo al paper validator (PID 52466), al
collector (PID 38099) y al ejecutor momentum, sin tocar ninguno de los tres.

Uso:
    python3 run_live_micro_favorite.py                 # DRY_RUN (default seguro)
    LIVE_MICRO_FAVORITE=1 LIVE_MICRO_FAVORITE_CONFIRM=I_UNDERSTAND_REAL_MONEY \\
        LIVE_MICRO_FAVORITE_PRIVATE_KEY=... LIVE_MICRO_FAVORITE_FUNDER=... \\
        python3 run_live_micro_favorite.py              # LIVE

Ver docs/LIVE_MICRO_FAVORITE.md para el comando exacto de activacion/detencion.
"""
import sys
import time

import live_micro_favorite_config as cfg
import live_micro_favorite_db as ldb
import live_micro_favorite_executor as ex
import live_micro_favorite_kill_switch as ks
import live_micro_favorite_order_client as oc


def _state_from_db():
    row = ldb.load_run_state()
    return {
        "n_orders_attempted": row["n_orders_attempted"],
        "consecutive_failures": row["consecutive_failures"],
        "capital_deployed_usd": row["capital_deployed_usd"],
        "realized_pnl_usd": row["realized_pnl_usd"],
        "kill_switch_tripped": bool(row["kill_switch_tripped"]),
        "kill_switch_reason": row["kill_switch_reason"],
    }


def _seen_condition_ids():
    conn = ldb.connect()
    return {r["condition_id"] for r in conn.execute("SELECT DISTINCT condition_id FROM live_micro_favorite_attempts").fetchall()}


def cycle(cutoff_ts, mode, state, seen, client=None):
    """Devuelve (n_processed, primer_row_procesado_o_None) -- lo segundo se
    usa para STOP_AFTER_FIRST_ATTEMPT (parar el proceso tras el primer
    mercado procesado, sea cual sea su fill_status)."""
    dconn = ex.data_conn()
    first_row = None
    try:
        now = time.time()
        candidates = ex.select_candidate_markets(dconn, cutoff_ts, now, seen)
        n_processed = 0
        for m in candidates:
            if state["kill_switch_tripped"]:
                break
            if cfg.STOP_AFTER_FIRST_ATTEMPT and n_processed >= 1:
                break  # un solo mercado por corrida cuando STOP_AFTER_FIRST_ATTEMPT
            row, attempted = ex.build_attempt(dconn, m, time.time(), state, mode, client=client)
            ldb.insert_attempt(row)
            seen.add(m["condition_id"])
            ldb.save_run_state(state)
            n_processed += 1
            if first_row is None:
                first_row = row
            ldb.log_event("run_live_micro_favorite", "attempt_logged",
                          {"condition_id": m["condition_id"], "fill_status": row["fill_status"],
                           "reject_reason": row.get("reject_reason")})
            if state["kill_switch_tripped"]:
                ldb.log_event("run_live_micro_favorite", "kill_switch_tripped", {"reason": state["kill_switch_reason"]})
                print(f"[live_micro_favorite] KILL SWITCH ACTIVADO: {state['kill_switch_reason']}", flush=True)

        n_resolved = ex.resolve_and_update_pnl(dconn, state)
        if n_resolved:
            ldb.save_run_state(state)
            if state["kill_switch_tripped"]:
                ldb.log_event("run_live_micro_favorite", "kill_switch_tripped", {"reason": state["kill_switch_reason"]})
                print(f"[live_micro_favorite] KILL SWITCH ACTIVADO: {state['kill_switch_reason']}", flush=True)

        if n_processed or n_resolved:
            print(f"[live_micro_favorite] {time.strftime('%H:%M:%S', time.gmtime())} UTC -- "
                  f"mode={mode} procesados={n_processed} resueltos={n_resolved} "
                  f"ordenes={state['n_orders_attempted']}/{cfg.MAX_ORDERS} "
                  f"capital=${state['capital_deployed_usd']:.2f}/${cfg.MAX_CAPITAL_DEPLOYED_USD:.2f} "
                  f"pnl=${state['realized_pnl_usd']:+.2f} fallos_consec={state['consecutive_failures']}",
                  flush=True)
        return n_processed, first_row
    finally:
        dconn.close()


def run():
    mode = "LIVE" if cfg.LIVE_MICRO_FAVORITE_ENABLED else "DRY_RUN"
    ldb.init_db(mode=mode)
    cutoff_ts = float(ldb.set_meta_if_absent("executor_started_at", repr(time.time())))
    ldb.log_event("run_live_micro_favorite", "start", {"mode": mode, "cutoff_ts": cutoff_ts})

    print(f"live_micro_favorite iniciado -- MODE={mode}. cutoff (mercados con open_time_utc >= esto): "
          f"{cutoff_ts} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(cutoff_ts))} UTC)", flush=True)
    print(f"limites: max_order=${cfg.MAX_ORDER_USD} max_orders={cfg.MAX_ORDERS} "
          f"max_capital=${cfg.MAX_CAPITAL_DEPLOYED_USD} kill_switch_loss=${cfg.KILL_SWITCH_LOSS_USD} "
          f"max_fallos_consecutivos={cfg.MAX_CONSECUTIVE_FAILURES} max_snapshot_age_s={cfg.MAX_SNAPSHOT_AGE_S}",
          flush=True)

    client = None
    if mode == "LIVE":
        client = oc.make_client()
        bal = oc.get_usdc_balance(client)
        print(f"[live_micro_favorite] LIVE -- wallet (LIVE_MICRO_FAVORITE_FUNDER)={cfg.LIVE_MICRO_FAVORITE_FUNDER} "
              f"balance_real_usdc=${bal:.2f}", flush=True)
        ldb.log_event("run_live_micro_favorite", "live_balance_check",
                      {"funder": cfg.LIVE_MICRO_FAVORITE_FUNDER, "balance_usdc": bal})
        if bal < cfg.MAX_ORDER_USD:
            print(f"[live_micro_favorite] balance insuficiente (${bal:.2f} < ${cfg.MAX_ORDER_USD}), abortando arranque LIVE",
                  flush=True)
            sys.exit(1)

    state = _state_from_db()
    seen = _seen_condition_ids()

    while True:
        if state["kill_switch_tripped"]:
            print(f"[live_micro_favorite] kill switch activo ({state['kill_switch_reason']}) -- proceso detenido, "
                  f"no se procesan mas mercados. Sigue vivo solo para resolver posiciones abiertas.", flush=True)
            try:
                dconn = ex.data_conn()
                ex.resolve_and_update_pnl(dconn, state)
                dconn.close()
                ldb.save_run_state(state)
            except Exception as e:
                ldb.log_event("run_live_micro_favorite", "error_post_kill_switch", {"error": str(e)})
            time.sleep(cfg.POLL_INTERVAL_S * 6)
            continue
        try:
            n_processed, first_row = cycle(cutoff_ts, mode, state, seen, client=client)
        except Exception as e:
            ldb.log_event("run_live_micro_favorite", "error", {"error": str(e)})
            print(f"[live_micro_favorite] error: {e}", file=sys.stderr, flush=True)
            n_processed, first_row = 0, None

        if cfg.STOP_AFTER_FIRST_ATTEMPT and first_row is not None:
            print(f"[live_micro_favorite] STOP_AFTER_FIRST_ATTEMPT: primer intento procesado "
                  f"(condition_id={first_row['condition_id']} fill_status={first_row['fill_status']} "
                  f"reject_reason={first_row.get('reject_reason')}) -- deteniendo el proceso automaticamente.",
                  flush=True)
            ldb.log_event("run_live_micro_favorite", "stop_after_first_attempt",
                          {"condition_id": first_row["condition_id"], "fill_status": first_row["fill_status"]})
            return
        time.sleep(cfg.POLL_INTERVAL_S)


if __name__ == "__main__":
    run()
