"""
Hipotesis principal: gestion dinamica de inventario UP/DOWN (construccion de
pares rentables + exposicion direccional intencional + cobertura costosa
cuando hace falta). Esto es SOLO analisis descriptivo y clasificacion -- NO
ejecuta ningun backtest de una nueva estrategia. Usa unicamente los 3.358
trades limpios (usable_for_strategy_learning=1); el inventario se reconstruye
DESDE CERO caminando esos mismos trades en orden cronologico por mercado (no
se reusa leader_inventory_timeline, que esta calculada sobre la tabla vieja
completa y ademas no filtra SELL).

Clasificacion CAUSAL: cada trade se clasifica usando SOLO el estado ANTES de
si mismo (up/down shares acumuladas de trades anteriores en el mismo
mercado) mas su propio combined_cost_to_pair (precio propio + best_ask
contrario en su propio instante, ya anti-lookahead por construccion en
trade_context). Nunca se usa el resultado final del mercado ni ningun trade
posterior.

Uso: python3 inventory_pair_analysis.py [csv_entrada] [csv_salida]
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

BALANCED_COVERAGE_THRESHOLD = 0.9   # mismo umbral editorial que export_leader_history_excel.py
LARGE_FIRST_SURPLUS_MULTIPLE = 1.0  # desde base empatada: shares > matched_before*este_valor -> D, si no C


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_trades(csv_path):
    rows = list(csv.DictReader(open(csv_path)))
    by_market = defaultdict(list)
    for r in rows:
        by_market[r["condition_id"]].append(r)
    for cid in by_market:
        by_market[cid].sort(key=lambda r: (f(r["source_timestamp_utc"]), int(r["leader_trade_id"])))
    return by_market


def classify_and_reconstruct(by_market):
    out = []
    for condition_id, trades in by_market.items():
        up_shares = down_shares = up_cost = down_cost = 0.0
        first_trade_ts = f(trades[0]["source_timestamp_utc"])
        # primer trade de cada lado, para las metricas descriptivas de tiempo (NO usadas en clasificacion)
        first_ts_by_side = {"Up": None, "Down": None}

        for t in trades:
            ts = f(t["source_timestamp_utc"])
            outcome = t["trade_outcome"]
            price, shares = f(t["price"]), f(t["shares"])

            up_before, down_before = up_shares, down_shares
            up_cost_before, down_cost_before = up_cost, down_cost
            vwap_up_before = (up_cost_before / up_before) if up_before > 0 else None
            vwap_down_before = (down_cost_before / down_before) if down_before > 0 else None
            matched_before = min(up_before, down_before)
            surplus_before = abs(up_before - down_before)
            surplus_side_before = ("Up" if up_before > down_before else "Down") if up_before != down_before else None
            combined_vwap_before = (vwap_up_before + vwap_down_before) if (vwap_up_before is not None and vwap_down_before is not None) else None
            coverage_before = (matched_before / max(up_before, down_before)) if max(up_before, down_before) > 0 else None

            # -------- clasificacion, SOLO con el estado 'before' + el propio combined_cost_to_pair --------
            combined_cost_to_pair = f(t["combined_cost_to_pair"])  # precio propio + ask contrario, en el propio instante

            if up_before == 0 and down_before == 0:
                function_tag = "A_FIRST_LEG"
            elif surplus_side_before is not None and outcome == surplus_side_before:
                # compra del lado que YA tenia mas -- ensancha el desbalance existente,
                # salvo que el conjunto siga bien cubierto (coverage alto) tras la compra
                up_after_tmp = up_before + (shares if outcome == "Up" else 0)
                down_after_tmp = down_before + (shares if outcome == "Down" else 0)
                matched_after_tmp = min(up_after_tmp, down_after_tmp)
                max_after_tmp = max(up_after_tmp, down_after_tmp)
                coverage_after_tmp = (matched_after_tmp / max_after_tmp) if max_after_tmp > 0 else None
                if coverage_after_tmp is not None and coverage_after_tmp >= BALANCED_COVERAGE_THRESHOLD:
                    function_tag = "C_ADD_TO_MATCHED_INVENTORY"
                else:
                    function_tag = "D_DIRECTIONAL_SURPLUS"
            elif surplus_side_before is None and up_before > 0:
                # base perfectamente empatada (>0) antes de este trade: primera compra que
                # rompe el empate. Chica -> se trata como seguir armando inventario emparejado;
                # grande -> ya es una apuesta direccional nueva.
                if shares <= matched_before * LARGE_FIRST_SURPLUS_MULTIPLE:
                    function_tag = "C_ADD_TO_MATCHED_INVENTORY"
                else:
                    function_tag = "D_DIRECTIONAL_SURPLUS"
            else:
                # Compra del lado que estaba EN DESVENTAJA (reduce el desbalance existente).
                # Nombre NEUTRAL a proposito -- no se llama "profitable" ni "hedge": eso
                # atribuiria intencion (arbitraje deliberado vs cobertura defensiva) que no
                # esta evidenciada. Con solo 5 trades bajo $1 en todo el dataset, tratar esa
                # cola como una categoria propia era estadisticamente fragil y cargada de
                # narrativa. La distincion de costo se reporta aparte, en buckets (ver
                # cost_bucket_at_imbalance_reducing_buy), nunca como parte del nombre.
                function_tag = "IMBALANCE_REDUCING_BUY" if combined_cost_to_pair is not None else "F_UNCLASSIFIED"

            # -------- actualizar inventario con ESTE trade --------
            if outcome == "Up":
                up_shares += shares; up_cost += price * shares
            elif outcome == "Down":
                down_shares += shares; down_cost += price * shares
            if first_ts_by_side[outcome] is None:
                first_ts_by_side[outcome] = ts

            up_after, down_after = up_shares, down_shares
            vwap_up_after = (up_cost / up_after) if up_after > 0 else None
            vwap_down_after = (down_cost / down_after) if down_after > 0 else None
            matched_after = min(up_after, down_after)
            surplus_after = abs(up_after - down_after)
            combined_vwap_after = (vwap_up_after + vwap_down_after) if (vwap_up_after is not None and vwap_down_after is not None) else None

            cost_bucket = None
            if function_tag == "IMBALANCE_REDUCING_BUY":
                v = combined_cost_to_pair
                cost_bucket = ("<0.99" if v < 0.99 else "0.99-1.00" if v < 1.00 else
                                "1.00-1.02" if v < 1.02 else "1.02-1.05" if v < 1.05 else ">1.05")

            out.append({
                **t,
                "function_tag": function_tag,
                "up_shares_before_clean": round(up_before, 4), "down_shares_before_clean": round(down_before, 4),
                "up_cost_before_clean": round(up_cost_before, 4), "down_cost_before_clean": round(down_cost_before, 4),
                "vwap_up_before": round(vwap_up_before, 4) if vwap_up_before is not None else None,
                "vwap_down_before": round(vwap_down_before, 4) if vwap_down_before is not None else None,
                "matched_shares_before": round(matched_before, 4), "surplus_shares_before": round(surplus_before, 4),
                "surplus_side_before": surplus_side_before, "coverage_ratio_before": round(coverage_before, 4) if coverage_before is not None else None,
                "combined_vwap_cost_before": round(combined_vwap_before, 4) if combined_vwap_before is not None else None,
                "combined_vwap_cost_after": round(combined_vwap_after, 4) if combined_vwap_after is not None else None,
                "marginal_cost_to_pair_now": round(combined_cost_to_pair, 4) if combined_cost_to_pair is not None else None,
                "cost_bucket_at_imbalance_reducing_buy": cost_bucket,
                "matched_shares_after": round(matched_after, 4), "surplus_shares_after": round(surplus_after, 4),
                "seconds_since_first_leg_this_market": round(ts - first_trade_ts, 3),
                "condition_id_check": condition_id,
            })
    return out


def add_retrospective_time_to_second_leg(out):
    """SOLO para las respuestas descriptivas (pregunta 4) -- nunca se usa en la
    clasificacion. Por mercado, tiempo entre el primer trade de UP y el primer
    trade de DOWN (en cualquier orden)."""
    by_market = defaultdict(list)
    for r in out:
        by_market[r["condition_id"]].append(r)
    gaps = {}
    for cid, rows in by_market.items():
        firsts = {}
        for r in sorted(rows, key=lambda r: f(r["source_timestamp_utc"])):
            o = r["trade_outcome"]
            if o not in firsts:
                firsts[o] = f(r["source_timestamp_utc"])
        if "Up" in firsts and "Down" in firsts:
            gaps[cid] = abs(firsts["Down"] - firsts["Up"])
    return gaps


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv")
    dst = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).resolve().parent / "export" / "inventory_pair_analysis.csv")

    by_market = load_trades(src)
    out = classify_and_reconstruct(by_market)

    fieldnames = list(out[0].keys())
    with open(dst, "w", newline="") as f_out:
        w = csv.DictWriter(f_out, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)
    print(f"{len(out)} filas -> {dst}")
