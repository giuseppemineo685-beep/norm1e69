"""
Reconciliacion pedida antes de proponer V2. Solo lectura, no backtest.
Corre contra /tmp/rebuild_test.db (copia, no data.db).
"""
import csv
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import audit_strategy_v1 as aud
import backtest_momentum_v1 as bt
import export_leader_history_excel as exh

LEADER_WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_clean(csv_path):
    return list(csv.DictReader(open(csv_path)))


# ============================================================ 1 ==========
def section1(conn, clean_rows):
    print("#" * 78)
    print("1) 103 mercados VWAP<1 (historico) vs 5 PROFITABLE_PAIR_COMPLETION (marginal)")
    print("#" * 78)

    combined = exh._combined_unique_trades(conn, LEADER_WALLET)
    by_cond_all = defaultdict(list)
    for t in combined:
        if t["condition_id"]:
            by_cond_all[t["condition_id"]].append(t)

    both_sided_lt1 = []
    for cid, trades in by_cond_all.items():
        up_cost = down_cost = up_shares = down_shares = 0.0
        for t in trades:
            if t["outcome"] not in ("Up", "Down") or t["side"] != "BUY" or t["price"] is None or t["shares"] is None:
                continue
            if t["outcome"] == "Up":
                up_cost += t["price"] * t["shares"]; up_shares += t["shares"]
            else:
                down_cost += t["price"] * t["shares"]; down_shares += t["shares"]
        if up_shares <= 0 or down_shares <= 0:
            continue
        vwap_up, vwap_down = up_cost / up_shares, down_cost / down_shares
        matched = min(up_shares, down_shares)
        coverage = matched / max(up_shares, down_shares)
        if coverage >= 0.9 and (vwap_up + vwap_down) < 1.0:
            both_sided_lt1.append({"condition_id": cid, "vwap_sum": vwap_up + vwap_down,
                                    "coverage": coverage, "n_trades_all_sources": len(trades)})
    print(f"[universo GRANDE: historico+vivo combinado, {len(by_cond_all)} mercados con >=1 trade]")
    print(f"mercados con VWAP historico UP+DOWN<1 y coverage>=0.9 (reproduccion del calculo original): {len(both_sided_lt1)}")

    clean_conditions = set(r["condition_id"] for r in clean_rows)
    overlap = [m for m in both_sided_lt1 if m["condition_id"] in clean_conditions]
    print(f"de esos, cuantos estan tambien en el universo LIMPIO (3.358 trades, {len(clean_conditions)} mercados): {len(overlap)}")

    # dentro del overlap: para cada trade INDIVIDUAL (dataset limpio) de esos mercados,
    # comparar 3 nociones de costo distintas
    overlap_cids = set(m["condition_id"] for m in overlap)
    trades_in_overlap = [r for r in clean_rows if r["condition_id"] in overlap_cids]
    print(f"trades limpios dentro de esos {len(overlap)} mercados: {len(trades_in_overlap)}")

    n_marginal_lt1 = sum(1 for r in trades_in_overlap if f(r["combined_cost_to_pair"]) is not None and f(r["combined_cost_to_pair"]) < 1.0)
    print(f"de esos, cuantos tuvieron combined_cost_to_pair (marginal, propio precio + ask contrario EN ESE INSTANTE) < 1.0: {n_marginal_lt1}")

    print("\nTRES NOCIONES DE COSTO, para que quede explicito que no es la misma cifra:")
    print("  a) coste historico medio del inventario (VWAP): promedio de TODOS los fills BUY de cada lado,")
    print("     a lo largo de TODA la vida del mercado -- mezcla precios de instantes MUY distintos.")
    print("  b) coste marginal del trade (combined_cost_to_pair): precio de ESTE fill + best_ask del lado")
    print("     contrario, en el instante de ESTE trade -- una foto puntual, no un promedio.")
    print("  c) coste ejecutable de ambas piernas en el order book: caminar profundidad real para el tamano")
    print("     de la pierna propia Y de la contraria (VWAP de 2 patas simultaneas) -- mas estricto que (b),")
    print("     que solo mira el best_ask (nivel 1) de la contraria.")

    # (c) para los pocos candidatos con (b)<1: verificar tambien contra profundidad real
    print("\n(c) verificacion de profundidad real para los trades con costo marginal (b) < 1.0 en TODO el dataset limpio (no solo el overlap):")
    b_candidates = [r for r in clean_rows if f(r["combined_cost_to_pair"]) is not None and f(r["combined_cost_to_pair"]) < 1.0]
    for r in b_candidates:
        snap_own_id = r["snapshot_up_id"] if r["trade_outcome"] == "Up" else r["snapshot_down_id"]
        snap_other_id = r["snapshot_down_id"] if r["trade_outcome"] == "Up" else r["snapshot_up_id"]
        shares = f(r["shares"])
        status_own, sh_own, usd_own, vwap_own = aud.walk_executable(conn, int(snap_own_id), shares * f(r["price"])) if snap_own_id else ("SKIPPED", 0, 0, None)
        status_other, sh_other, usd_other, vwap_other = aud.walk_executable(conn, int(snap_other_id), shares * f(r["opposite_leg_price"])) if snap_other_id and r["opposite_leg_price"] else ("SKIPPED", 0, 0, None)
        print(f"  trade {r['leader_trade_id']} mercado {r['condition_id'][:12]}...  combined_marginal={f(r['combined_cost_to_pair']):.4f}  "
              f"pierna_propia: status={status_own} vwap_ejecutable={vwap_own}  pierna_contraria: status={status_other} vwap_ejecutable={vwap_other}")

    return both_sided_lt1, overlap, trades_in_overlap


# ============================================================ 2 ==========
def section2(clean_rows):
    print()
    print("#" * 78)
    print("2) COSTLY_HEDGE -> IMBALANCE_REDUCING_BUY, subdividido por coste combinado")
    print("#" * 78)
    print("Fusiono B_PROFITABLE_PAIR_COMPLETION + E_COSTLY_HEDGE en una sola categoria neutral:")
    print("IMBALANCE_REDUCING_BUY = 'compra del lado que antes de este trade tenia MENOS shares'.")
    print("No se atribuye intencion (rentable vs cobertura) -- solo se describe el costo, en buckets.\n")

    import inventory_pair_analysis as ipa
    by_market = ipa.load_trades(str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv"))
    out = ipa.classify_and_reconstruct(by_market)

    imbalance_reducing = [r for r in out if r["function_tag"] == "IMBALANCE_REDUCING_BUY"]
    buckets = [("<0.99", lambda v: v < 0.99), ("0.99-1.00", lambda v: 0.99 <= v < 1.00),
               ("1.00-1.02", lambda v: 1.00 <= v < 1.02), ("1.02-1.05", lambda v: 1.02 <= v < 1.05),
               (">1.05", lambda v: v >= 1.05)]
    n_total = len(imbalance_reducing)
    print(f"IMBALANCE_REDUCING_BUY total: {n_total} trades ({n_total/len(out)*100:.1f}% del dataset limpio)")
    bucket_rows = []
    for label, cond in buckets:
        sub = [r for r in imbalance_reducing if f(r["marginal_cost_to_pair_now"]) is not None and cond(f(r["marginal_cost_to_pair_now"]))]
        cap = sum(f(r["usdc_amount"]) or 0 for r in sub)
        print(f"  {label:12s} n={len(sub):5d} ({len(sub)/n_total*100:5.1f}%)   capital=${cap:9.2f}")
        bucket_rows.append({"bucket": label, "n": len(sub), "pct": round(len(sub)/n_total*100, 1), "capital_usd": round(cap, 2)})
    n_none = sum(1 for r in imbalance_reducing if f(r["marginal_cost_to_pair_now"]) is None)
    print(f"  sin combined_cost_to_pair disponible: {n_none}")
    return out, imbalance_reducing, bucket_rows


# ============================================================ 3 ==========
def section3(clean_rows):
    print()
    print("#" * 78)
    print("3) Precio pagado vs bid/ask propio (spread) -- NO determina maker/taker, solo posicion relativa")
    print("#" * 78)
    EPS = 0.005
    cats = Counter()
    examples = defaultdict(list)
    n_no_data = 0
    for r in clean_rows:
        price = f(r["price"])
        if r["trade_outcome"] == "Up":
            bid, ask = f(r["best_bid_up"]), f(r["best_ask_up"])
        else:
            bid, ask = f(r["best_bid_down"]), f(r["best_ask_down"])
        if price is None or bid is None or ask is None:
            n_no_data += 1
            continue
        if abs(price - bid) <= EPS:
            cat = "en_best_bid"
        elif abs(price - ask) <= EPS:
            cat = "en_best_ask"
        elif bid < price < ask:
            cat = "entre_bid_y_ask"
        elif price < bid:
            cat = "por_debajo_del_bid"
        elif price > ask:
            cat = "por_encima_del_ask"
        else:
            cat = "otro"
        cats[cat] += 1
        if len(examples[cat]) < 3:
            examples[cat].append((r["leader_trade_id"], price, bid, ask))
    n = sum(cats.values())
    for cat, c in cats.most_common():
        print(f"  {cat:22s} n={c:5d} ({c/n*100:5.1f}%)   ej: {examples[cat]}")
    print(f"  sin datos de bid/ask propio: {n_no_data}")
    print(f"\n  NOTA: bid/ask son de NUESTRO snapshot (con latencia mediana ~0.18s, regla conservadora). Un trade "
          f"'fuera del spread' puede reflejar staleness del snapshot, no necesariamente que el fill real haya "
          f"sido fuera del spread verdadero en ese instante exacto. maker/taker sigue sin conocerse -- esto NO "
          f"lo determina, solo da una pista indirecta (en best_bid es compatible con post maker, en/cerca best_ask "
          f"es compatible con tomar liquidez, pero no es prueba).")
    return cats


# ============================================================ 4 ==========
def section4(conn, clean_rows):
    print()
    print("#" * 78)
    print("4) Recalculo del beneficio de cubrir ($599.62) -- capital equivalente, sin atribucion causal")
    print("#" * 78)
    by_market = defaultdict(list)
    for r in clean_rows:
        by_market[r["condition_id"]].append(r)

    rows_out = []
    for cid, trs in by_market.items():
        trs_sorted = sorted(trs, key=lambda r: f(r["source_timestamp_utc"]))
        winner = trs_sorted[0]["market_winner"]
        if winner not in ("Up", "Down"):
            continue
        up_shares = down_shares = up_cost = down_cost = 0.0
        peak = None
        states = []
        for r in trs_sorted:
            price, shares, outcome = f(r["price"]), f(r["shares"]), r["trade_outcome"]
            if outcome == "Up":
                up_shares += shares; up_cost += price * shares
            else:
                down_shares += shares; down_cost += price * shares
            surplus = abs(up_shares - down_shares)
            state = {"up_shares": up_shares, "down_shares": down_shares, "up_cost": up_cost,
                     "down_cost": down_cost, "surplus": surplus}
            states.append(state)
            if peak is None or surplus > peak["surplus"]:
                peak = state

        final = states[-1]

        def econ(state):
            up_s, down_s, up_c, down_c = state["up_shares"], state["down_shares"], state["up_cost"], state["down_cost"]
            capital = up_c + down_c
            vwap_up = (up_c / up_s) if up_s > 0 else 0.0
            vwap_down = (down_c / down_s) if down_s > 0 else 0.0
            matched = min(up_s, down_s)
            matched_cost = matched * (vwap_up + vwap_down)
            surplus_side = "Up" if up_s > down_s else ("Down" if down_s > up_s else None)
            surplus_shares = abs(up_s - down_s)
            surplus_cost = surplus_shares * (vwap_up if surplus_side == "Up" else vwap_down if surplus_side == "Down" else 0)
            surplus_payout = surplus_shares * 1.0 if surplus_side == winner else 0.0
            payout = matched * 1.0 + surplus_payout
            pnl = payout - capital
            roi = (pnl / capital) if capital > 0 else None
            return {"capital": capital, "payout": payout, "pnl": pnl, "roi": roi}

        e_peak, e_final = econ(peak), econ(final)
        rows_out.append({
            "condition_id": cid, "winner": winner,
            "peak_capital": round(e_peak["capital"], 2), "peak_payout": round(e_peak["payout"], 2),
            "peak_pnl": round(e_peak["pnl"], 2), "peak_roi": round(e_peak["roi"], 4) if e_peak["roi"] is not None else None,
            "final_capital": round(e_final["capital"], 2), "final_payout": round(e_final["payout"], 2),
            "final_pnl": round(e_final["pnl"], 2), "final_roi": round(e_final["roi"], 4) if e_final["roi"] is not None else None,
            "capital_added_after_peak": round(e_final["capital"] - e_peak["capital"], 2),
            "pnl_diff": round(e_final["pnl"] - e_peak["pnl"], 2),
        })

    total_peak_capital = sum(r["peak_capital"] for r in rows_out)
    total_final_capital = sum(r["final_capital"] for r in rows_out)
    total_peak_pnl = sum(r["peak_pnl"] for r in rows_out)
    total_final_pnl = sum(r["final_pnl"] for r in rows_out)

    print(f"EXPLICACION DEL CONTRAFACTUAL: para cada mercado, 'peak' = el estado de inventario (shares+coste de "
          f"AMBOS lados) justo en el trade donde el desbalance (|up-down|) alcanzo su maximo. 'final' = el "
          f"estado en el ultimo trade limpio de ese mercado. Comparo capital desplegado, payout y PnL en AMBOS "
          f"estados -- NO asumo que la diferencia se deba solo a 'cobertura': el capital desplegado tambien "
          f"CAMBIA entre peak y final (normalmente sube, porque seguir comprando el lado chico cuesta dinero "
          f"nuevo), asi que una comparacion de PnL en dolares sin mirar el capital es enganosa.\n")
    print(f"mercados evaluados: {len(rows_out)}")
    print(f"capital total en el momento peak: ${total_peak_capital:.2f}   ROI agregado: {(total_peak_pnl/total_peak_capital*100):.2f}%")
    print(f"capital total en el momento final: ${total_final_capital:.2f}   ROI agregado: {(total_final_pnl/total_final_capital*100):.2f}%")
    print(f"capital ADICIONAL desplegado entre peak y final (dinero nuevo, no 'gratis'): ${total_final_capital-total_peak_capital:.2f}")
    print(f"PnL peak: ${total_peak_pnl:.2f}   PnL final: ${total_final_pnl:.2f}   diferencia bruta: ${total_final_pnl-total_peak_pnl:.2f}")
    print(f"  (la cifra de ${total_final_pnl-total_peak_pnl:.2f} NO es 'ganancia gratis por cubrir' -- una parte")
    print(f"   importante es simplemente que se desplego mas capital (+${total_final_capital-total_peak_capital:.2f}). "
          f"ROI final ({(total_final_pnl/total_final_capital*100):.2f}%) vs ROI peak ({(total_peak_pnl/total_peak_capital*100):.2f}%) "
          f"es la comparacion correcta por capital equivalente.")

    n_helped = sum(1 for r in rows_out if r["pnl_diff"] > 0.01)
    n_hurt = sum(1 for r in rows_out if r["pnl_diff"] < -0.01)
    print(f"\npor mercado: PnL final > PnL peak en {n_helped}, PnL final < PnL peak en {n_hurt}, "
          f"igual en {len(rows_out)-n_helped-n_hurt}")

    out_csv = str(Path(__file__).resolve().parent / "export" / "peak_vs_final_by_market.csv")
    with open(out_csv, "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows_out[0].keys()))
        w.writeheader(); w.writerows(rows_out)
    print(f"\ntabla completa por mercado -> {out_csv}")
    return rows_out


# ============================================================ 5 ==========
def section5(clean_rows):
    print()
    print("#" * 78)
    print("5) FIRST_LEG: patron de eleccion (momentum / contrarian / barato / favorito / sin patron)")
    print("#" * 78)
    import inventory_pair_analysis as ipa
    by_market = ipa.load_trades(str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv"))
    out = ipa.classify_and_reconstruct(by_market)
    first_legs = [r for r in out if r["function_tag"] == "A_FIRST_LEG"]
    n_total = len(first_legs)
    print(f"n FIRST_LEG total: {n_total}\n")

    n_mom = n_con = n_mom_denom = 0
    n_cheap = n_fav = n_price_denom = 0
    for r in first_legs:
        dist = f(r["underlying_distance_from_open_pct"])
        if dist is not None and abs(dist) >= 0.01:
            n_mom_denom += 1
            aligned = (dist > 0 and r["trade_outcome"] == "Up") or (dist < 0 and r["trade_outcome"] == "Down")
            if aligned:
                n_mom += 1
            else:
                n_con += 1
        own_ask = f(r["best_ask_up"]) if r["trade_outcome"] == "Up" else f(r["best_ask_down"])
        other_ask = f(r["best_ask_down"]) if r["trade_outcome"] == "Up" else f(r["best_ask_up"])
        if own_ask is not None and other_ask is not None and own_ask != other_ask:
            n_price_denom += 1
            if own_ask < other_ask:
                n_cheap += 1
            else:
                n_fav += 1

    print(f"MOMENTUM/CONTRARIAN (n={n_mom_denom} con señal de distancia >=0.01%):")
    print(f"  alineado con momentum: {n_mom} ({n_mom/n_mom_denom*100:.1f}%)   vs 50% esperado bajo 'sin patron'")
    print(f"  contrario al momentum: {n_con} ({n_con/n_mom_denom*100:.1f}%)")
    se = (0.5*0.5/n_mom_denom)**0.5
    z = (n_mom/n_mom_denom - 0.5) / se
    print(f"  error estandar bajo H0 (50/50): {se*100:.1f} puntos porcentuales -- z={z:.2f} "
          f"({'diferencia notable' if abs(z)>=2 else 'NO se distingue claramente de 50/50 con esta muestra'})")

    print(f"\nBARATO/FAVORITO (n={n_price_denom} con ambos asks distintos):")
    print(f"  elige el lado MAS BARATO (underdog por precio): {n_cheap} ({n_cheap/n_price_denom*100:.1f}%)")
    print(f"  elige el lado FAVORITO (mas caro/mas probable por precio): {n_fav} ({n_fav/n_price_denom*100:.1f}%)")
    se2 = (0.5*0.5/n_price_denom)**0.5
    z2 = (n_cheap/n_price_denom - 0.5) / se2
    print(f"  error estandar: {se2*100:.1f}pp -- z={z2:.2f} "
          f"({'diferencia notable' if abs(z2)>=2 else 'NO se distingue claramente de 50/50 con esta muestra'})")
    return first_legs


# ============================================================ 6 ==========
def section6(clean_rows):
    print()
    print("#" * 78)
    print("6) Formula de sizing")
    print("#" * 78)
    import inventory_pair_analysis as ipa
    by_market = ipa.load_trades(str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv"))
    out = ipa.classify_and_reconstruct(by_market)

    first_legs = [r for r in out if r["function_tag"] == "A_FIRST_LEG"]
    sizes_first = sorted(f(r["shares"]) for r in first_legs if f(r["shares"]) is not None)
    n1 = len(sizes_first)
    print(f"tamaño FIRST_LEG (n={n1}): mediana={sizes_first[n1//2]:.2f}  p25={sizes_first[n1//4]:.2f}  p75={sizes_first[3*n1//4]:.2f}")

    # tamaño de la primera vez que toca el lado contrario, por mercado
    by_cond = defaultdict(list)
    for r in out:
        by_cond[r["condition_id"]].append(r)
    second_leg_sizes = []
    for cid, trs in by_cond.items():
        trs_sorted = sorted(trs, key=lambda r: f(r["source_timestamp_utc"]))
        first_side = trs_sorted[0]["trade_outcome"]
        for r in trs_sorted[1:]:
            if r["trade_outcome"] != first_side:
                second_leg_sizes.append(f(r["shares"]))
                break
    second_leg_sizes.sort()
    n2 = len(second_leg_sizes)
    print(f"tamaño de la 2da pierna (primera compra del lado contrario, n={n2}): mediana={second_leg_sizes[n2//2]:.2f} "
          f"p25={second_leg_sizes[n2//4]:.2f}  p75={second_leg_sizes[3*n2//4]:.2f}")

    # relacion tamaño vs desbalance previo (todos los trades, no solo first leg)
    print("\ntamaño (shares) por bucket de surplus_shares_before (desbalance previo), TODOS los trades:")
    buckets_surplus = [(0, 0.01), (0.01, 10), (10, 30), (30, 60), (60, 1e9)]
    for lo, hi in buckets_surplus:
        sub = sorted(f(r["shares"]) for r in out if lo <= f(r["surplus_shares_before"]) < hi and f(r["shares"]) is not None)
        if sub:
            n = len(sub)
            print(f"  surplus_before [{lo:6.2f},{hi:6.2f}): n={n:4d}  mediana_shares={sub[n//2]:.2f}")

    print("\ntamaño (shares) por bucket de seconds_to_market_close, TODOS los trades:")
    buckets_time = [(240, 300), (180, 240), (120, 180), (60, 120), (0, 60)]
    for lo, hi in buckets_time:
        sub = sorted(f(r["shares"]) for r in out if f(r["seconds_to_market_close"]) is not None
                     and lo <= f(r["seconds_to_market_close"]) < hi and f(r["shares"]) is not None)
        if sub:
            n = len(sub)
            print(f"  seconds_to_close [{lo:3d},{hi:3d}): n={n:4d}  mediana_shares={sub[n//2]:.2f}")

    print("\ntamaño (shares) por bucket de precio pagado, TODOS los trades:")
    buckets_price = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)]
    for lo, hi in buckets_price:
        sub = sorted(f(r["shares"]) for r in out if f(r["price"]) is not None
                     and lo <= f(r["price"]) < hi and f(r["shares"]) is not None)
        if sub:
            n = len(sub)
            print(f"  price [{lo:.1f},{hi:.1f}): n={n:4d}  mediana_shares={sub[n//2]:.2f}")


if __name__ == "__main__":
    csv_path = str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv")
    clean_rows = load_clean(csv_path)
    conn = sqlite3.connect("file:/tmp/rebuild_test.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    section1(conn, clean_rows)
    section2(clean_rows)
    section3(clean_rows)
    section4(conn, clean_rows)
    section5(clean_rows)
    section6(clean_rows)
