"""
Strategy V3 -- 1ra pierna: regla de momentum V1, YA CONGELADA (T=0.02%, no
se retoca). 2da pierna: SOLO si el costo ejecutable combinado (recorriendo
profundidad real, ambas piernas) queda <= $0.99 -- umbral FIJO por
instruccion explicita, no calibrado, no se afloja si aparecen pocos casos.
Reusa el motor compartido de backtest_v2_inventory.py sin modificarlo.

Antes de V3: comparacion momentum vs favorito restringida a la INTERSECCION
de mercados donde AMBAS reglas pueden decidir Y ejecutar (mismas condiciones
de liquidez), no los universos por separado de cada una (que tienen tamaños
distintos porque momentum exige señal y favorito no).
"""
import argparse
import sqlite3
from pathlib import Path

import backtest_v2_inventory as v2

V3_PAIR_THRESHOLD = 0.99          # FIJO -- no calibrado, no se toca aunque haya pocos casos
V2B_FROZEN_THRESHOLD_PAIR = 1.05  # el que quedo congelado en la corrida de V2 (reproducido, no recalibrado)


def common_markets_momentum_vs_favorite(conn, markets):
    """Interseccion: momentum tiene señal, favorito tiene lado, y AMBOS
    pueden abrir $10 reales (ninguno de los dos SKIPPED por falta de
    liquidez) -- mismas condiciones de liquidez y ejecucion para los dos."""
    common = []
    for m in markets:
        decision_ts = m["open_time_utc"] + v2.DECISION_OFFSET_S
        if decision_ts >= m["close_time_utc"] - v2.CLOSE_BUFFER_S:
            continue
        mom_side, _ = v2.side_momentum(conn, m, decision_ts, threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
        fav_side, _ = v2.side_favorite(conn, m, decision_ts)
        if mom_side is None or fav_side is None:
            continue
        mom_leg1 = v2.open_leg1(conn, m, decision_ts, mom_side)
        fav_leg1 = v2.open_leg1(conn, m, decision_ts, fav_side)
        if mom_leg1["status"] == "SKIPPED" or fav_leg1["status"] == "SKIPPED":
            continue
        common.append(m)
    return common


def run_on_common(conn, common_markets, side_selector, label, **kwargs):
    results = []
    for m in common_markets:
        decision_ts = m["open_time_utc"] + v2.DECISION_OFFSET_S
        side, _ = side_selector(conn, m, decision_ts, **kwargs)
        leg1 = v2.open_leg1(conn, m, decision_ts, side)
        winner = m["winner"]
        payout = leg1["shares"] * 1.0 if side == winner else 0.0
        pnl = payout - leg1["usd"]
        results.append({"status": "NEVER_COMPLETED", "side": side, "total_capital": leg1["usd"],
                         "total_pnl": pnl, "paired_pnl": 0.0, "residual_pnl": pnl,
                         "matched_shares": 0.0, "residual_shares": leg1["shares"], "winner": winner,
                         "asset_symbol": m["asset_symbol"], "open_time_utc": m["open_time_utc"],
                         "condition_id": m["condition_id"]})
    return v2.aggregate(results, label)


def run(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    universe = v2.get_full_market_universe(conn)
    train, test = v2.split_universe(universe)
    print(f"TEST: {len(test)} mercados ({v2.fmt(test[0]['open_time_utc'])} -> {v2.fmt(test[-1]['open_time_utc'])})")

    print("\n" + "#" * 78)
    print("PASO PREVIO: momentum vs favorito, MISMOS mercados, misma liquidez/ejecucion")
    print("#" * 78)
    common = common_markets_momentum_vs_favorite(conn, test)
    print(f"interseccion (momentum con señal Y favorito con lado Y ambos ejecutables): {len(common)} mercados")
    same_side = sum(1 for m in common
                     if v2.side_momentum(conn, m, m["open_time_utc"] + v2.DECISION_OFFSET_S,
                                          threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)[0]
                     == v2.side_favorite(conn, m, m["open_time_utc"] + v2.DECISION_OFFSET_S)[0])
    print(f"de esos, mismo lado elegido por momentum y favorito: {same_side} ({same_side/len(common)*100:.1f}%)")

    common_momentum = run_on_common(conn, common, v2.side_momentum, "momentum_mismos_mercados",
                                     threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    common_favorite = run_on_common(conn, common, v2.side_favorite, "favorito_mismos_mercados")
    for agg in (common_momentum, common_favorite):
        print(f"\n{agg['strategy']}:")
        for k, val in agg.items():
            if k != "strategy":
                print(f"    {k}: {val}")

    print("\n" + "=" * 78)
    print(f"STRATEGY V3 -- 1ra pierna momentum (T={v2.V1_MOMENTUM_THRESHOLD_PCT}%, congelado), "
          f"2da pierna SOLO si costo ejecutable combinado <= ${V3_PAIR_THRESHOLD} (FIJO, sin calibrar)")
    print("=" * 78)
    results_v3 = v2.run_variant(conn, test, v2.side_momentum, V3_PAIR_THRESHOLD,
                                 threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    agg_v3 = v2.aggregate(results_v3, "V3_momentum_par_099")
    print(json_like(agg_v3))

    n_initial = sum(1 for r in results_v3 if r["status"] in ("COMPLETED", "NEVER_COMPLETED"))
    n_pairs = sum(1 for r in results_v3 if r["status"] == "COMPLETED")
    n_residual_only = sum(1 for r in results_v3 if r["status"] == "NEVER_COMPLETED")
    print(f"\ntrades iniciales (1ra pierna abierta): {n_initial}")
    print(f"pares rentables encontrados (costo ejecutable <= ${V3_PAIR_THRESHOLD}): {n_pairs}")
    print(f"posiciones direccionales que quedaron SIN completar: {n_residual_only}")
    if n_pairs == 0:
        print(f"\n*** DECLARACION EXPLICITA: NO aparecio ningun par <= ${V3_PAIR_THRESHOLD} en TEST bajo estas "
              f"condiciones. El umbral NO se afloja para fabricar resultados -- V3 opera 100% como estrategia "
              f"direccional pura (identica a momentum puro) en este periodo. ***")
    elif n_pairs < 10:
        print(f"\n*** ADVERTENCIA: solo {n_pairs} pares encontrados -- muestra demasiado chica para "
              f"generalizar el 'margen asegurado' o el comportamiento de pares de V3. Se reporta igual, "
              f"etiquetado como evidencia minima. ***")

    print("\n--- comparacion V3 vs momentum puro / favorito / V2-B / no operar ---")
    momentum_puro = v2.run_single_leg_baseline(conn, test, v2.side_momentum, "momentum_puro_TEST_completo",
                                                threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    favorito_full = v2.run_single_leg_baseline(conn, test, v2.side_favorite, "favorito_TEST_completo")
    results_v2b = v2.run_variant(conn, test, v2.side_momentum, V2B_FROZEN_THRESHOLD_PAIR,
                                  threshold_pct=v2.V1_MOMENTUM_THRESHOLD_PCT)
    agg_v2b = v2.aggregate(results_v2b, "V2-B_reproducido")
    noop = {"strategy": "no_operar", "n_trades": 0, "total_pnl_usd": 0.0, "roi_pct": 0.0,
            "total_capital_usd": 0.0, "max_drawdown_usd": 0.0}

    for agg in (agg_v3, momentum_puro, favorito_full, agg_v2b, noop):
        print(f"\n{agg['strategy']}:")
        for k, val in agg.items():
            if k != "strategy":
                print(f"    {k}: {val}")

    return {
        "test": test, "common_markets": common, "common_momentum": common_momentum,
        "common_favorite": common_favorite, "results_v3": results_v3, "agg_v3": agg_v3,
        "momentum_puro": momentum_puro, "favorito_full": favorito_full, "agg_v2b": agg_v2b, "noop": noop,
        "n_initial": n_initial, "n_pairs": n_pairs, "n_residual_only": n_residual_only,
    }


def json_like(d):
    return "\n".join(f"  {k}: {v}" for k, v in d.items() if k != "strategy")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    args = p.parse_args()
    run(args.db_path)
