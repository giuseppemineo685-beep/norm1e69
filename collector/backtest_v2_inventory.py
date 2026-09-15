"""
Backtest V2 -- gestion dinamica de inventario UP/DOWN, autonoma (nunca mira
al lider ni usa sus trades como señal). Tres variantes de 1ra pierna sobre
el MISMO motor de pares/cobertura:

  V2-A: 1ra pierna en el lado mas BARATO (menor ask) a los 60s de abierta la
        ventana. Siempre abre (si hay liquidez).
  V2-B: 1ra pierna segun momentum (signo de underlying_distance_from_open_pct,
        igual que V1), solo si supera el umbral de V1 (T=0.02%, ya congelado
        en la corrida anterior -- no se recalibra acá). Si no hay señal, no
        abre ese mercado.
  V2-C: NO abre salvo que a los 60s el costo combinado de top-of-book
        (best_ask_up+best_ask_down) ya sea <= un umbral propio, calibrado en
        TRAIN y congelado antes de TEST ('oportunidad temporal'). Si abre,
        entra en el lado mas barato (subconjunto mas selectivo de A).

Motor compartido (las 3 variantes lo comparten):
  1. Entrada de 1ra pierna a los 60s exactos, $10 de stake, recorriendo
     profundidad real (FULL/PARTIAL/SKIPPED -- nunca fill asumido).
  2. Monitoreo continuo (cada CHECK_INTERVAL_S=5s) del costo de completar el
     par: costo_real_ya_pagado_por_share + precio_ejecutable_ahora del lado
     contrario para igualar las shares de la 1ra pierna. Completa la PRIMERA
     vez que ese combinado <= umbral_pair (calibrado en TRAIN, congelado).
  3. Cierra la ventana de nuevas entradas (1ra pierna Y completar par) 60s
     antes del cierre del mercado.
  4. Nunca usa snapshots recibidos despues del instante de decision (misma
     regla conservadora que trade_context/V1: event_ts Y received_ts <=
     instante).
  5. Capital limitado por mercado: como mucho 1ra pierna + 1 intento de
     completar (bounded por diseño, no hay reintentos ni escalado).

Universo: TODOS los mercados BTC/ETH/SOL de 5min resueltos con order book
en la ventana -- no solo donde opero el lider. Split TRAIN/TEST 60/40
cronologico sobre el universo COMPLETO (no sobre un subconjunto ya filtrado
por señal), para que las 3 variantes y los baselines compartan exactamente
la misma particion temporal.
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

import audit_strategy_v1 as aud
import backtest_momentum_v1 as bt

DECISION_OFFSET_S = 60
STAKE_USD = 10.0
CHECK_INTERVAL_S = 5
CLOSE_BUFFER_S = 60  # no abrir ni completar en los ultimos 60s
V1_MOMENTUM_THRESHOLD_PCT = 0.02  # ya elegido y congelado en la corrida de V1 -- no se retoca acá

PAIR_THRESHOLD_CANDIDATES = [0.98, 0.99, 1.00, 1.005, 1.01, 1.02, 1.03, 1.05]
OPPORTUNITY_THRESHOLD_CANDIDATES = [0.95, 0.97, 0.98, 0.99, 1.00, 1.02]


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------- datos ---
def get_full_market_universe(conn):
    rows = conn.execute("""
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc, winner
        FROM markets
        WHERE asset_symbol IN ('BTC','ETH','SOL') AND window_minutes=5
          AND winner IN ('Up','Down') AND open_time_utc IS NOT NULL AND close_time_utc IS NOT NULL
        ORDER BY open_time_utc
    """).fetchall()
    return list(rows)


def split_universe(markets):
    markets = sorted(markets, key=lambda m: m["open_time_utc"])
    n = len(markets)
    split = int(n * 0.6)
    return markets[:split], markets[split:]


def walk_executable_shares(conn, snapshot_id, target_shares):
    """Como aud.walk_executable pero el objetivo es un NUMERO DE SHARES (para
    igualar exactamente lo que ya se tiene en la otra pata), no un monto en
    dolares. FULL/PARTIAL/SKIPPED, nunca asume fill."""
    levels = conn.execute(
        "SELECT price, size FROM orderbook_levels WHERE snapshot_id=? AND side='ask' ORDER BY level",
        (snapshot_id,)).fetchall()
    if not levels:
        return "SKIPPED", 0.0, 0.0, None
    remaining = target_shares
    shares = 0.0
    spent = 0.0
    for lvl in levels:
        price, size = lvl["price"], lvl["size"]
        if price <= 0 or size <= 0:
            continue
        take = min(remaining, size)
        shares += take
        spent += take * price
        remaining -= take
        if remaining <= 1e-9:
            break
    status = "FULL" if remaining <= 1e-9 else "PARTIAL"
    vwap = (spent / shares) if shares > 0 else None
    return status, shares, spent, vwap


def _snapshot(conn, condition_id, token_id, target_ts):
    return bt._nearest_snapshot(conn, condition_id, token_id, target_ts)


def _underlying(conn, asset_symbol, target_ts):
    return bt._nearest_underlying(conn, asset_symbol, target_ts)


# ------------------------------------------------------- selectores 1ra pierna ---
def side_cheap(conn, m, decision_ts):
    snap_up = _snapshot(conn, m["condition_id"], m["token_up_id"], decision_ts)
    snap_down = _snapshot(conn, m["condition_id"], m["token_down_id"], decision_ts)
    if snap_up is None or snap_down is None or snap_up["best_ask"] is None or snap_down["best_ask"] is None:
        return None, None
    side = "Up" if snap_up["best_ask"] < snap_down["best_ask"] else "Down"
    return side, (snap_up, snap_down)


def side_momentum(conn, m, decision_ts, threshold_pct=V1_MOMENTUM_THRESHOLD_PCT):
    under = _underlying(conn, m["asset_symbol"], decision_ts)
    if under is None or under["distance_from_open_pct"] is None:
        return None, None
    dist = under["distance_from_open_pct"]
    if abs(dist) < threshold_pct:
        return None, None
    side = "Up" if dist > 0 else "Down"
    return side, None


def side_opportunity(conn, m, decision_ts, threshold_open):
    snap_up = _snapshot(conn, m["condition_id"], m["token_up_id"], decision_ts)
    snap_down = _snapshot(conn, m["condition_id"], m["token_down_id"], decision_ts)
    if snap_up is None or snap_down is None or snap_up["best_ask"] is None or snap_down["best_ask"] is None:
        return None, None
    combined = snap_up["best_ask"] + snap_down["best_ask"]
    if combined > threshold_open:
        return None, None
    side = "Up" if snap_up["best_ask"] < snap_down["best_ask"] else "Down"
    return side, (snap_up, snap_down)


# ------------------------------------------------------------- motor compartido ---
def open_leg1(conn, m, decision_ts, side):
    """Stake fijo de $10, recorriendo profundidad real por MONTO en dolares
    (no por shares -- asi $10 es realmente lo que se gasta, sin importar que
    niveles mas profundos tengan peor precio)."""
    token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
    snap = _snapshot(conn, m["condition_id"], token_id, decision_ts)
    if snap is None or snap["best_ask"] is None:
        return {"status": "SKIPPED", "shares": 0.0, "usd": 0.0, "vwap": None}
    status, shares, spent, vwap = aud.walk_executable(conn, snap["id"], STAKE_USD)
    return {"status": status, "shares": shares, "usd": spent, "vwap": vwap}


def monitor_and_complete(conn, m, side, leg1, decision_ts, threshold_pair):
    """Camina el tiempo en pasos de CHECK_INTERVAL_S desde decision_ts hasta
    close_time_utc-CLOSE_BUFFER_S. Completa la PRIMERA vez que
    own_vwap + vwap_ejecutable_contrario(target=shares_leg1) <= threshold_pair.
    Un solo intento (no reintenta si sale PARTIAL); si nunca se cumple la
    condicion, la posicion queda puramente direccional."""
    other_side = "Down" if side == "Up" else "Up"
    other_token = m["token_down_id"] if side == "Up" else m["token_up_id"]
    own_vwap = leg1["vwap"]
    target_shares = leg1["shares"]
    close_limit = m["close_time_utc"] - CLOSE_BUFFER_S

    t = decision_ts + CHECK_INTERVAL_S
    while t <= close_limit:
        snap_other = _snapshot(conn, m["condition_id"], other_token, t)
        if snap_other is not None and snap_other["best_ask"] is not None:
            quick_combined = own_vwap + snap_other["best_ask"]  # chequeo barato antes de caminar profundidad
            if quick_combined <= threshold_pair:
                status, shares, spent, vwap_other = walk_executable_shares(conn, snap_other["id"], target_shares)
                if status != "SKIPPED":
                    real_combined = own_vwap + vwap_other
                    if real_combined <= threshold_pair:
                        return {"completed": True, "status": status, "shares": shares, "usd": spent,
                                "vwap": vwap_other, "combined_cost": real_combined, "completed_at": t,
                                "seconds_to_complete": t - decision_ts}
        t += CHECK_INTERVAL_S
    return {"completed": False}


def simulate_market(conn, m, side_selector, threshold_pair, **selector_kwargs):
    decision_ts = m["open_time_utc"] + DECISION_OFFSET_S
    if decision_ts >= m["close_time_utc"] - CLOSE_BUFFER_S:
        return {"status": "WINDOW_TOO_SHORT"}
    side, _ = side_selector(conn, m, decision_ts, **selector_kwargs)
    if side is None:
        return {"status": "NO_SIGNAL"}
    leg1 = open_leg1(conn, m, decision_ts, side)
    if leg1["status"] == "SKIPPED":
        return {"status": "LEG1_SKIPPED_NO_LIQUIDITY", "side": side}

    completion = monitor_and_complete(conn, m, side, leg1, decision_ts, threshold_pair)
    winner = m["winner"]

    own_capital = leg1["usd"]
    if completion["completed"]:
        matched = min(leg1["shares"], completion["shares"])
        matched_cost = matched * leg1["vwap"] + matched * completion["vwap"]
        paired_pnl = matched * 1.0 - matched_cost
        residual_shares = leg1["shares"] - matched  # solo si completion fue PARTIAL
        residual_cost = residual_shares * leg1["vwap"]
        residual_payout = residual_shares * 1.0 if side == winner else 0.0
        residual_pnl = residual_payout - residual_cost
        total_capital = leg1["usd"] + completion["usd"]
        total_pnl = paired_pnl + residual_pnl
        return {
            "status": "COMPLETED", "side": side, "leg1_shares": leg1["shares"], "leg1_vwap": leg1["vwap"],
            "leg1_usd": leg1["usd"], "leg1_status": leg1["status"],
            "completion_status": completion["status"], "completion_shares": completion["shares"],
            "completion_vwap": completion["vwap"], "completion_usd": completion["usd"],
            "combined_cost": completion["combined_cost"], "seconds_to_complete": completion["seconds_to_complete"],
            "matched_shares": matched, "paired_pnl": paired_pnl, "residual_shares": residual_shares,
            "residual_pnl": residual_pnl, "total_capital": total_capital, "total_pnl": total_pnl,
            "winner": winner,
        }
    else:
        payout = leg1["shares"] * 1.0 if side == winner else 0.0
        total_pnl = payout - own_capital
        return {
            "status": "NEVER_COMPLETED", "side": side, "leg1_shares": leg1["shares"], "leg1_vwap": leg1["vwap"],
            "leg1_usd": leg1["usd"], "leg1_status": leg1["status"], "total_capital": own_capital,
            "total_pnl": total_pnl, "paired_pnl": 0.0, "residual_pnl": total_pnl,
            "matched_shares": 0.0, "residual_shares": leg1["shares"], "winner": winner,
        }


def run_variant(conn, markets, side_selector, threshold_pair, **selector_kwargs):
    results = []
    for m in markets:
        r = simulate_market(conn, m, side_selector, threshold_pair, **selector_kwargs)
        r["condition_id"] = m["condition_id"]
        r["market_title"] = m["market_title"]
        r["asset_symbol"] = m["asset_symbol"]
        r["open_time_utc"] = m["open_time_utc"]
        results.append(r)
    return results


def aggregate(results, label):
    traded = [r for r in results if r["status"] in ("COMPLETED", "NEVER_COMPLETED")]
    completed = [r for r in traded if r["status"] == "COMPLETED"]
    never = [r for r in traded if r["status"] == "NEVER_COMPLETED"]
    n = len(traded)
    if n == 0:
        return {"strategy": label, "n_trades": 0}

    total_capital = sum(r["total_capital"] for r in traded)
    total_pnl = sum(r["total_pnl"] for r in traded)
    paired_pnl = sum(r["paired_pnl"] for r in traded)
    residual_pnl = sum(r["residual_pnl"] for r in traded)
    n_wins = sum(1 for r in traded if (r["matched_shares"] > 0 or r["residual_shares"] > 0)
                 and r["total_pnl"] > 0)

    secs = sorted(r["seconds_to_complete"] for r in completed if r.get("seconds_to_complete") is not None)
    combined_costs = sorted(r["combined_cost"] for r in completed if r.get("combined_cost") is not None)

    ordered = sorted(traded, key=lambda r: r["open_time_utc"])
    cum = peak = max_dd = 0.0
    for r in ordered:
        cum += r["total_pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    exposure_by_asset = defaultdict(lambda: {"n": 0, "capital": 0.0, "pnl": 0.0})
    for r in traded:
        e = exposure_by_asset[r["asset_symbol"]]
        e["n"] += 1; e["capital"] += r["total_capital"]; e["pnl"] += r["total_pnl"]

    return {
        "strategy": label, "n_trades": n, "n_pairs_completed": len(completed),
        "pct_pairs_completed": round(len(completed) / n * 100, 1),
        "n_never_completed": len(never),
        "mean_seconds_between_legs": round(sum(secs) / len(secs), 1) if secs else None,
        "median_seconds_between_legs": round(secs[len(secs) // 2], 1) if secs else None,
        "mean_combined_cost": round(sum(combined_costs) / len(combined_costs), 4) if combined_costs else None,
        "median_combined_cost": round(combined_costs[len(combined_costs) // 2], 4) if combined_costs else None,
        "mean_assured_margin": round(sum(1 - c for c in combined_costs) / len(combined_costs), 4) if combined_costs else None,
        "pair_pnl_usd": round(paired_pnl, 2), "residual_pnl_usd": round(residual_pnl, 2),
        "total_pnl_usd": round(total_pnl, 2), "total_capital_usd": round(total_capital, 2),
        "roi_pct": round(total_pnl / total_capital * 100, 2) if total_capital else None,
        "win_rate_pct": round(n_wins / n * 100, 1),
        "max_drawdown_usd": round(max_dd, 2),
        "max_capital_single_market_usd": round(max((r["total_capital"] for r in traded), default=0), 2),
        "exposure_by_asset": {k: {"n": v["n"], "capital": round(v["capital"], 2), "pnl": round(v["pnl"], 2)}
                               for k, v in exposure_by_asset.items()},
    }


def calibrate_threshold_pair(conn, train_markets, side_selector, **selector_kwargs):
    scored = []
    for t in PAIR_THRESHOLD_CANDIDATES:
        results = run_variant(conn, train_markets, side_selector, t, **selector_kwargs)
        agg = aggregate(results, f"T={t}")
        if agg.get("n_trades", 0) >= 20:
            scored.append((t, agg))
    if not scored:
        return PAIR_THRESHOLD_CANDIDATES[len(PAIR_THRESHOLD_CANDIDATES) // 2], scored
    best = max(scored, key=lambda x: x[1]["total_pnl_usd"])
    return best[0], scored


def calibrate_threshold_open(conn, train_markets, threshold_pair):
    scored = []
    for t in OPPORTUNITY_THRESHOLD_CANDIDATES:
        results = run_variant(conn, train_markets, side_opportunity, threshold_pair, threshold_open=t)
        agg = aggregate(results, f"open<= {t}")
        if agg.get("n_trades", 0) >= 15:
            scored.append((t, agg))
    if not scored:
        return OPPORTUNITY_THRESHOLD_CANDIDATES[-1], scored
    best = max(scored, key=lambda x: x[1]["total_pnl_usd"])
    return best[0], scored


# --------------------------------------------------------------- baselines ---
def run_single_leg_baseline(conn, markets, side_selector, label, **selector_kwargs):
    """Como una variante V2 pero SIN intentar completar el par nunca -- para
    comparar contra 'favorito', 'underdog', 'momentum V1' puros de este mismo
    motor/universo (apples-to-apples con V2)."""
    results = []
    for m in markets:
        decision_ts = m["open_time_utc"] + DECISION_OFFSET_S
        if decision_ts >= m["close_time_utc"] - CLOSE_BUFFER_S:
            continue
        side, _ = side_selector(conn, m, decision_ts, **selector_kwargs)
        if side is None:
            continue
        leg1 = open_leg1(conn, m, decision_ts, side)
        if leg1["status"] == "SKIPPED":
            continue
        winner = m["winner"]
        payout = leg1["shares"] * 1.0 if side == winner else 0.0
        pnl = payout - leg1["usd"]
        results.append({"status": "NEVER_COMPLETED", "side": side, "total_capital": leg1["usd"],
                         "total_pnl": pnl, "paired_pnl": 0.0, "residual_pnl": pnl,
                         "matched_shares": 0.0, "residual_shares": leg1["shares"], "winner": winner,
                         "asset_symbol": m["asset_symbol"], "open_time_utc": m["open_time_utc"],
                         "condition_id": m["condition_id"]})
    return aggregate(results, label)


def side_favorite(conn, m, decision_ts):
    snap_up = _snapshot(conn, m["condition_id"], m["token_up_id"], decision_ts)
    snap_down = _snapshot(conn, m["condition_id"], m["token_down_id"], decision_ts)
    if snap_up is None or snap_down is None:
        return None, None
    mid_up = (snap_up["best_bid"] + snap_up["best_ask"]) / 2 if (snap_up["best_bid"] is not None and snap_up["best_ask"] is not None) else snap_up["best_ask"]
    mid_down = (snap_down["best_bid"] + snap_down["best_ask"]) / 2 if (snap_down["best_bid"] is not None and snap_down["best_ask"] is not None) else snap_down["best_ask"]
    if mid_up is None or mid_down is None or mid_up == mid_down:
        return None, None
    return ("Up" if mid_up > mid_down else "Down"), None


# ------------------------------------------------------------------ main ---
def run(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    universe = get_full_market_universe(conn)
    train, test = split_universe(universe)
    print(f"universo TOTAL: {len(universe)} mercados BTC/ETH/SOL 5min resueltos")
    print(f"TRAIN: {len(train)} ({fmt(train[0]['open_time_utc'])} -> {fmt(train[-1]['open_time_utc'])})")
    print(f"TEST:  {len(test)} ({fmt(test[0]['open_time_utc'])} -> {fmt(test[-1]['open_time_utc'])})")

    print("\n--- calibrando threshold_pair en TRAIN (usando el selector de V2-A, compartido por las 3 variantes) ---")
    threshold_pair, sweep_pair = calibrate_threshold_pair(conn, train, side_cheap)
    for t, agg in sweep_pair:
        print(f"  T={t:.3f}  n={agg['n_trades']:4d}  pairs={agg['n_pairs_completed']:4d}  "
              f"pnl=${agg['total_pnl_usd']:8.2f}  roi={agg['roi_pct']:6.2f}%")
    print(f"UMBRAL DE PAR ELEGIDO (congelado): {threshold_pair}")

    print("\n--- calibrando threshold_open de V2-C en TRAIN (con threshold_pair ya congelado) ---")
    threshold_open, sweep_open = calibrate_threshold_open(conn, train, threshold_pair)
    for t, agg in sweep_open:
        print(f"  open<={t:.3f}  n={agg['n_trades']:4d}  pairs={agg['n_pairs_completed']:4d}  "
              f"pnl=${agg['total_pnl_usd']:8.2f}  roi={agg['roi_pct']:6.2f}%")
    print(f"UMBRAL DE OPORTUNIDAD V2-C ELEGIDO (congelado): {threshold_open}")

    print("\n" + "=" * 78)
    print("EVALUACION UNICA SOBRE TEST -- umbrales ya congelados, sin retocar")
    print("=" * 78)

    results_a = run_variant(conn, test, side_cheap, threshold_pair)
    results_b = run_variant(conn, test, side_momentum, threshold_pair, threshold_pct=V1_MOMENTUM_THRESHOLD_PCT)
    results_c = run_variant(conn, test, side_opportunity, threshold_pair, threshold_open=threshold_open)

    agg_a = aggregate(results_a, "V2-A_lado_barato")
    agg_b = aggregate(results_b, "V2-B_momentum")
    agg_c = aggregate(results_c, "V2-C_oportunidad_temporal")

    bl_favorite = run_single_leg_baseline(conn, test, side_favorite, "baseline_favorito")
    bl_underdog = run_single_leg_baseline(conn, test, side_cheap, "baseline_underdog_puro")
    bl_momentum = run_single_leg_baseline(conn, test, side_momentum, "baseline_momentum_V1_puro", threshold_pct=V1_MOMENTUM_THRESHOLD_PCT)
    bl_noop = {"strategy": "baseline_no_operar", "n_trades": 0, "total_pnl_usd": 0.0, "roi_pct": 0.0,
               "total_capital_usd": 0.0, "max_drawdown_usd": 0.0}

    for agg in (agg_a, agg_b, agg_c, bl_favorite, bl_underdog, bl_momentum, bl_noop):
        print(f"\n{agg['strategy']}:")
        for k, v in agg.items():
            if k != "strategy":
                print(f"    {k}: {v}")

    return {
        "universe": universe, "train": train, "test": test,
        "threshold_pair": threshold_pair, "threshold_open": threshold_open,
        "sweep_pair": sweep_pair, "sweep_open": sweep_open,
        "results_a": results_a, "results_b": results_b, "results_c": results_c,
        "agg_a": agg_a, "agg_b": agg_b, "agg_c": agg_c,
        "bl_favorite": bl_favorite, "bl_underdog": bl_underdog, "bl_momentum": bl_momentum, "bl_noop": bl_noop,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    args = p.parse_args()
    run(args.db_path)
