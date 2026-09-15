"""
Auditoria final de Strategy V1 -- SOLO LECTURA, no recalibra nada. Reusa
backtest_momentum_v1.gather_candidates()/simulate() tal cual estan (mismo
umbral T=0.02% ya elegido en TRAIN, mismo split cronologico), y agrega:
  - detalle trade-por-trade de los 63 TEST con profundidad disponible
  - recalculo con precio ejecutable real (recorriendo orderbook_levels para
    llenar $10), marcando FULL/PARTIAL/SKIPPED -- nunca asume fill completo
  - embudo de exclusion del universo TEST completo (no solo los candidatos
    que ya habian pasado el filtro)
  - 3 baselines sobre el MISMO universo de candidatos TEST
  - P&L, ROI, win rate, drawdown, exposicion por activo

No toca collector/data.db (corre contra una copia), no reinicia el
collector, no reactiva LIVE, no cambia ningun umbral.
"""
import argparse
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

import backtest_momentum_v1 as bt
import trade_context as tc

RANDOM_SEED = 20260915  # fijo y documentado, para que sea reproducible


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_split(conn):
    candidates, n_no_under, n_no_book = bt.gather_candidates(conn)
    candidates.sort(key=lambda c: c["open_time_utc"])
    n = len(candidates)
    split = int(n * 0.6)
    train, test = candidates[:split], candidates[split:]
    return candidates, train, test, n_no_under, n_no_book


def choose_threshold(train):
    train_results = [bt.simulate(train, t) for t in bt.CANDIDATE_THRESHOLDS_PCT]
    valid = [r for r in train_results if r["n_trades"] >= 20]
    best = max(valid, key=lambda r: r["gross_pnl"])
    return best["threshold_pct"], train_results


def walk_executable(conn, snapshot_id, target_usd):
    """Recorre orderbook_levels (side='ask') del snapshot dado hasta gastar
    target_usd. Devuelve (status, shares_filled, usd_spent, vwap_price).
    status: 'SKIPPED' (sin niveles de profundidad -- no se puede evaluar,
    NO se asume nada), 'PARTIAL' (la profundidad disponible no alcanza para
    target_usd completos) o 'FULL'."""
    levels = conn.execute(
        "SELECT price, size FROM orderbook_levels WHERE snapshot_id=? AND side='ask' ORDER BY level",
        (snapshot_id,)).fetchall()
    if not levels:
        return "SKIPPED", 0.0, 0.0, None
    remaining_usd = target_usd
    shares = 0.0
    spent = 0.0
    for lvl in levels:
        price, size = lvl["price"], lvl["size"]
        if price <= 0 or size <= 0:
            continue
        level_usd_capacity = price * size
        take_usd = min(remaining_usd, level_usd_capacity)
        take_shares = take_usd / price
        shares += take_shares
        spent += take_usd
        remaining_usd -= take_usd
        if remaining_usd <= 1e-9:
            break
    status = "FULL" if remaining_usd <= 1e-9 else "PARTIAL"
    vwap = (spent / shares) if shares > 0 else None
    return status, shares, spent, vwap


def total_ask_depth_usd(conn, snapshot_id):
    row = conn.execute(
        "SELECT COALESCE(SUM(price*size),0) usd FROM orderbook_levels WHERE snapshot_id=? AND side='ask'",
        (snapshot_id,)).fetchone()
    return row["usd"]


def run(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    candidates, train, test, n_no_under_global, n_no_book_global = get_split(conn)
    chosen_T, train_sweep = choose_threshold(train)
    test_executed = [c for c in test if c["abs_distance_pct"] >= chosen_T]
    test_result_simple = bt.simulate(test, chosen_T)
    train_result_simple = bt.simulate(train, chosen_T)

    print("#" * 78)
    print("1) UMBRAL TRAIN + TABLA DE CANDIDATOS (sin recalibrar -- reproduccion exacta)")
    print("#" * 78)
    print(f"TRAIN: {len(train)} mercados {fmt(train[0]['open_time_utc'])} -> {fmt(train[-1]['open_time_utc'])}")
    print(f"TEST:  {len(test)} mercados {fmt(test[0]['open_time_utc'])} -> {fmt(test[-1]['open_time_utc'])}")
    print(f"{'T%':>6} {'n':>5} {'wins':>5} {'win_rate':>9} {'gross_pnl':>11} {'roi':>8}")
    for r in train_sweep:
        if r["n_trades"]:
            print(f"{r['threshold_pct']:6.2f} {r['n_trades']:5d} {r['n_wins']:5d} "
                  f"{r['win_rate']*100:8.1f}% {r['gross_pnl']:10.2f}$ {r['roi']*100:7.2f}%")
        else:
            print(f"{r['threshold_pct']:6.2f} {0:5d}")
    print(f"\nUMBRAL ELEGIDO (ya fijado en la corrida anterior, reproducido igual): T={chosen_T}%")
    print(f"TEST ejecutado con ese umbral: n={len(test_executed)} (coincide con la corrida previa: {test_result_simple['n_trades']})")

    print()
    print("#" * 78)
    print("2)+3) LOS 63 TRADES TEST, CON PROFUNDIDAD Y PRECIO EJECUTABLE REAL")
    print("#" * 78)
    detail_rows = []
    for c in test_executed:
        # snapshot exacto reutilizado (mismo lookup deterministico que gather_candidates)
        target_ts = c["decision_ts"]
        token_id = None
        m = conn.execute("SELECT token_up_id, token_down_id FROM markets WHERE condition_id=?",
                          (c["condition_id"],)).fetchone()
        token_id = m["token_up_id"] if c["side"] == "Up" else m["token_down_id"]
        snap = bt._nearest_snapshot(conn, c["condition_id"], token_id, target_ts)
        depth_usd = total_ask_depth_usd(conn, snap["id"]) if snap else 0.0
        status, shares_exec, usd_spent, vwap = walk_executable(conn, snap["id"], bt.STAKE_USD) if snap else ("SKIPPED", 0.0, 0.0, None)
        won = c["side"] == c["winner"]
        payoff_exec = shares_exec * 1.0 if won else 0.0
        pnl_exec = payoff_exec - usd_spent

        # modelo simple (best_ask, fill asumido completo) -- para comparar
        shares_simple = bt.STAKE_USD / c["entry_price"]
        pnl_simple = (shares_simple * 1.0 if won else 0.0) - bt.STAKE_USD

        detail_rows.append({
            "condition_id": c["condition_id"], "market_title": c["market_title"],
            "decision_time_utc": fmt(c["decision_ts"]), "asset_symbol": c["asset_symbol"],
            "distance_from_open_pct": round(c["distance_from_open_pct"], 4), "signal_side": c["side"],
            "best_ask": c["entry_price"], "depth_available_usd": round(depth_usd, 2),
            "fill_status": status, "shares_simulated_simple": round(shares_simple, 4),
            "shares_filled_executable": round(shares_exec, 4), "usd_deployed_executable": round(usd_spent, 2),
            "vwap_executable": round(vwap, 4) if vwap else None,
            "winner": c["winner"], "won": won,
            "pnl_simple_usd": round(pnl_simple, 4), "pnl_executable_usd": round(pnl_exec, 4),
        })

    n_full = sum(1 for r in detail_rows if r["fill_status"] == "FULL")
    n_partial = sum(1 for r in detail_rows if r["fill_status"] == "PARTIAL")
    n_skipped = sum(1 for r in detail_rows if r["fill_status"] == "SKIPPED")
    print(f"fill_status: FULL={n_full}  PARTIAL={n_partial}  SKIPPED={n_skipped}  (de {len(detail_rows)} trades)")
    for r in detail_rows[:5]:
        print(" ", r)
    print(f"  ... ({len(detail_rows)} filas totales, ver Excel)")

    executable_trades = [r for r in detail_rows if r["fill_status"] in ("FULL", "PARTIAL")]
    usd_deployed_total = sum(r["usd_deployed_executable"] for r in executable_trades)
    pnl_exec_total = sum(r["pnl_executable_usd"] for r in executable_trades)
    n_wins_exec = sum(1 for r in executable_trades if r["won"])
    print(f"\nRESULTADO CON PRECIO EJECUTABLE REAL (excluye {n_skipped} SKIPPED del calculo de P&L):")
    print(f"  trades con fill real (FULL+PARTIAL): {len(executable_trades)}")
    print(f"  usd desplegado real: ${usd_deployed_total:.2f}  (vs ${bt.STAKE_USD*len(test_executed):.2f} nominal)")
    print(f"  win_rate: {n_wins_exec/len(executable_trades)*100:.1f}%")
    print(f"  gross_pnl ejecutable: ${pnl_exec_total:.2f}")
    print(f"  roi ejecutable (sobre usd realmente desplegado): {pnl_exec_total/usd_deployed_total*100:.2f}%")

    print()
    print("#" * 78)
    print("4) CONFIRMACION: subyacente y snapshot de entrada recibidos ANTES de la decision")
    print("#" * 78)
    viol_under = viol_snap = 0
    lags_under = []
    lags_snap = []
    for c in test_executed:
        m = conn.execute("SELECT token_up_id, token_down_id FROM markets WHERE condition_id=?",
                          (c["condition_id"],)).fetchone()
        token_id = m["token_up_id"] if c["side"] == "Up" else m["token_down_id"]
        under = bt._nearest_underlying(conn, c["asset_symbol"], c["decision_ts"])
        snap = bt._nearest_snapshot(conn, c["condition_id"], token_id, c["decision_ts"])
        if under is None or under["received_at_utc"] > c["decision_ts"]:
            viol_under += 1
        else:
            lags_under.append(c["decision_ts"] - under["received_at_utc"])
        if snap is None or snap["received_at_utc_ms"] / 1000.0 > c["decision_ts"]:
            viol_snap += 1
        else:
            lags_snap.append(c["decision_ts"] - snap["received_at_utc_ms"] / 1000.0)
    print(f"subyacente recibido DESPUES de la decision: {viol_under}/{len(test_executed)} "
          f"{'-- OK, ninguno' if viol_under==0 else '-- FALLA'}")
    print(f"snapshot de entrada recibido DESPUES de la decision: {viol_snap}/{len(test_executed)} "
          f"{'-- OK, ninguno' if viol_snap==0 else '-- FALLA'}")
    if lags_under:
        lags_under.sort(); print(f"  lag subyacente (decision-recepcion): min={lags_under[0]:.3f}s max={lags_under[-1]:.3f}s")
    if lags_snap:
        lags_snap.sort(); print(f"  lag snapshot (decision-recepcion): min={lags_snap[0]:.3f}s max={lags_snap[-1]:.3f}s")

    print()
    print("#" * 78)
    print("5) EMBUDO DE EXCLUSION -- universo TEST completo (no solo los candidatos ya filtrados)")
    print("#" * 78)
    test_boundary_ts = test[0]["open_time_utc"]
    all_test_markets = conn.execute("""
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc, winner
        FROM markets
        WHERE asset_symbol IN ('BTC','ETH','SOL') AND window_minutes=5
          AND winner IN ('Up','Down') AND open_time_utc >= ?
        ORDER BY open_time_utc
    """, (test_boundary_ts,)).fetchall()
    print(f"universo TEST (open_time_utc >= {fmt(test_boundary_ts)}, BTC/ETH/SOL 5min resueltos): {len(all_test_markets)}")

    cat = Counter()
    for m in all_test_markets:
        decision_ts = m["open_time_utc"] + bt.DECISION_OFFSET_S
        if decision_ts >= (m["close_time_utc"] or decision_ts + 1):
            cat["ventana_invalida"] += 1
            continue
        if tc.ORDERBOOK_GAP_START_TS <= decision_ts <= tc.ORDERBOOK_GAP_END_TS:
            cat["hueco_vpn"] += 1
            continue
        under = bt._nearest_underlying(conn, m["asset_symbol"], decision_ts)
        if under is None or under["distance_from_open_pct"] is None:
            cat["contexto_incompleto_subyacente"] += 1
            continue
        dist = under["distance_from_open_pct"]
        if dist == 0:
            cat["sin_señal_distancia_cero"] += 1
            continue
        side = "Up" if dist > 0 else "Down"
        token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
        snap = bt._nearest_snapshot(conn, m["condition_id"], token_id, decision_ts)
        if snap is None or snap["best_ask"] is None:
            cat["contexto_incompleto_orderbook"] += 1
            continue
        if abs(dist) < chosen_T:
            cat["sin_señal_bajo_umbral"] += 1
            continue
        # tiene señal y top-of-book -- chequeo de liquidez real (profundidad)
        status, _, _, _ = walk_executable(conn, snap["id"], bt.STAKE_USD)
        if status == "SKIPPED":
            cat["liquidez_insuficiente_sin_profundidad"] += 1
        elif status == "PARTIAL":
            cat["señal_ejecutada_fill_parcial"] += 1
        else:
            cat["señal_ejecutada_fill_completo"] += 1

    for k, v in cat.most_common():
        print(f"  {k:38s} {v:4d}")
    print(f"  {'TOTAL':38s} {sum(cat.values()):4d}  (debe sumar {len(all_test_markets)})")

    print()
    print("#" * 78)
    print("6) BASELINES sobre el MISMO universo de 83 candidatos TEST")
    print("#" * 78)
    rnd = random.Random(RANDOM_SEED)

    def eval_strategy(name, trades):
        n = len(trades)
        if n == 0:
            return {"strategy": name, "n_trades": 0}
        n_wins = sum(1 for t in trades if t["side"] == t["winner"])
        stake_total = bt.STAKE_USD * n
        payoff_total = sum((bt.STAKE_USD / t["entry_price"]) * 1.0 if t["side"] == t["winner"] else 0.0 for t in trades)
        gross_pnl = payoff_total - stake_total
        by_asset = Counter(t["asset_symbol"] for t in trades)
        # drawdown sobre la curva de pnl acumulado, en orden cronologico
        ordered = sorted(trades, key=lambda t: t["decision_ts"])
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in ordered:
            pnl_t = (bt.STAKE_USD / t["entry_price"]) - bt.STAKE_USD if t["side"] == t["winner"] else -bt.STAKE_USD
            cum += pnl_t
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        return {
            "strategy": name, "n_trades": n, "n_wins": n_wins, "win_rate_pct": round(n_wins/n*100, 1),
            "stake_total_usd": stake_total, "gross_pnl_usd": round(gross_pnl, 2),
            "roi_pct": round(gross_pnl/stake_total*100, 2), "max_drawdown_usd": round(max_dd, 2),
            "exposure_by_asset": dict(by_asset),
        }

    # V1 = test_executed (ya calculado)
    v1_eval = eval_strategy("V1_momentum_con_umbral", test_executed)

    # baseline b: momentum sin umbral (T=0, ya excluye distance==0 por diseño de gather_candidates)
    baseline_momentum_raw = eval_strategy("baseline_momentum_sin_umbral", test)

    # baseline a: favorito por precio (mid) de Polymarket, sobre el mismo universo `test`
    favorite_trades = []
    for c in test:
        m = conn.execute("SELECT token_up_id, token_down_id FROM markets WHERE condition_id=?",
                          (c["condition_id"],)).fetchone()
        snap_up = bt._nearest_snapshot(conn, c["condition_id"], m["token_up_id"], c["decision_ts"])
        snap_down = bt._nearest_snapshot(conn, c["condition_id"], m["token_down_id"], c["decision_ts"])
        if snap_up is None or snap_down is None:
            continue
        mid_up = (snap_up["best_bid"] + snap_up["best_ask"]) / 2 if (snap_up["best_bid"] is not None and snap_up["best_ask"] is not None) else snap_up["best_ask"]
        mid_down = (snap_down["best_bid"] + snap_down["best_ask"]) / 2 if (snap_down["best_bid"] is not None and snap_down["best_ask"] is not None) else snap_down["best_ask"]
        if mid_up is None or mid_down is None or mid_up == mid_down:
            continue
        side = "Up" if mid_up > mid_down else "Down"
        entry_price = snap_up["best_ask"] if side == "Up" else snap_down["best_ask"]
        if entry_price is None:
            continue
        favorite_trades.append({**c, "side": side, "entry_price": entry_price})
    baseline_favorite = eval_strategy("baseline_favorito_por_precio", favorite_trades)

    # baseline c: aleatorio reproducible, mismo universo `test`, entra siempre (usa best_ask del lado sorteado)
    random_trades = []
    for c in test:
        m = conn.execute("SELECT token_up_id, token_down_id FROM markets WHERE condition_id=?",
                          (c["condition_id"],)).fetchone()
        side = rnd.choice(["Up", "Down"])
        token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
        snap = bt._nearest_snapshot(conn, c["condition_id"], token_id, c["decision_ts"])
        if snap is None or snap["best_ask"] is None:
            continue
        random_trades.append({**c, "side": side, "entry_price": snap["best_ask"]})
    baseline_random = eval_strategy(f"baseline_aleatorio_seed{RANDOM_SEED}", random_trades)

    for ev in (v1_eval, baseline_favorite, baseline_momentum_raw, baseline_random):
        print(f"\n{ev['strategy']}:")
        for k, v in ev.items():
            if k != "strategy":
                print(f"    {k}: {v}")

    return {
        "chosen_T": chosen_T, "train_sweep": train_sweep, "train": train, "test": test,
        "test_executed": test_executed, "detail_rows": detail_rows,
        "n_full": n_full, "n_partial": n_partial, "n_skipped": n_skipped,
        "usd_deployed_total": usd_deployed_total, "pnl_exec_total": pnl_exec_total,
        "n_wins_exec": n_wins_exec, "executable_trades": executable_trades,
        "viol_under": viol_under, "viol_snap": viol_snap,
        "lags_under": lags_under, "lags_snap": lags_snap,
        "exclusion_funnel": dict(cat), "test_boundary_ts": test_boundary_ts,
        "n_all_test_markets": len(all_test_markets),
        "v1_eval": v1_eval, "baseline_favorite": baseline_favorite,
        "baseline_momentum_raw": baseline_momentum_raw, "baseline_random": baseline_random,
        "random_seed": RANDOM_SEED,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    args = p.parse_args()
    run(args.db_path)
