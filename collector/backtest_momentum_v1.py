"""
Backtest cronologico de Draft Strategy V1 (momentum de apertura), SIN mirar
al lider en ningun punto de la decision -- el lider solo sirvio para
DESCUBRIR el patron (ver ANALYSIS en el Excel final). La regla se evalua de
forma independiente para TODOS los mercados BTC/ETH/SOL de 5min resueltos,
no solo los que el lider opero.

Regla V1:
  - Instante de decision: open_time_utc + DECISION_OFFSET_S (60s adentro de
    la ventana de 5min -- deja tiempo de sobra para conseguir el order book
    correcto, y ~86% del tiempo restante).
  - Senal: signo(underlying_distance_from_open_pct) en ese instante, SI su
    magnitud >= THRESHOLD_PCT. Si no llega al umbral, NO TRADE.
  - Entrada: best_ask del lado elegido, del snapshot mas reciente <=
    instante de decision (misma regla conservadora anti-lookahead que
    trade_context -- reimplementada acá contra orderbook_snapshots crudo,
    ya que estos mercados no tienen fila en trade_context al no haberlos
    operado el lider).
  - Tamano: stake fijo en USD (STAKE_USD), shares = stake/precio_entrada.
  - Payoff: shares*$1 si el lado elegido gano, $0 si perdio.
  - P&L = payoff - stake.  ESTO ES P&L BRUTO: no incluye fees, slippage mas
    alla del best_ask observado, ni redenciones reales.

Split cronologico: TRAIN = primer 60% de mercados por open_time_utc (se usa
SOLO para elegir THRESHOLD_PCT, barriendo candidatos y quedandose con el que
maximiza P&L en TRAIN). TEST = ultimo 40%, evaluado UNA sola vez con el
umbral ya congelado -- nunca se reajusta con datos de TEST.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone

DECISION_OFFSET_S = 60
STAKE_USD = 10.0
SNAPSHOT_TOLERANCE_S = 2.5
CANDIDATE_THRESHOLDS_PCT = [0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _nearest_underlying(conn, asset_symbol, target_ts):
    row = conn.execute(
        """SELECT * FROM underlying_prices WHERE asset_symbol=? AND received_at_utc<=?
           ORDER BY received_at_utc DESC LIMIT 1""",
        (asset_symbol, target_ts)).fetchone()
    if row is None or (target_ts - row["received_at_utc"]) > SNAPSHOT_TOLERANCE_S:
        return None
    return row


def _nearest_snapshot(conn, condition_id, token_id, target_ts):
    row = conn.execute(
        """SELECT *, COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) AS ts
           FROM orderbook_snapshots
           WHERE condition_id=? AND token_id=?
             AND COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) <= ?
             AND received_at_utc_ms/1000.0 <= ?
           ORDER BY received_at_utc_ms DESC LIMIT 1""",
        (condition_id, token_id, target_ts, target_ts)).fetchone()
    if row is None or (target_ts - row["ts"]) > SNAPSHOT_TOLERANCE_S:
        return None
    return row


def gather_candidates(conn):
    """Un candidato por mercado: estado en el instante de decision + resultado.
    NUNCA usa datos de despues del instante de decision (misma regla que
    trade_context). Devuelve solo mercados donde tanto el subyacente como el
    order book del lado señalado estaban realmente disponibles ahi."""
    markets = conn.execute("""
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc, winner
        FROM markets
        WHERE asset_symbol IN ('BTC','ETH','SOL') AND window_minutes=5
          AND winner IN ('Up','Down') AND open_time_utc IS NOT NULL
        ORDER BY open_time_utc ASC
    """).fetchall()

    out = []
    n_no_underlying = n_no_book = 0
    for m in markets:
        decision_ts = m["open_time_utc"] + DECISION_OFFSET_S
        if decision_ts >= (m["close_time_utc"] or decision_ts + 1):
            continue
        under = _nearest_underlying(conn, m["asset_symbol"], decision_ts)
        if under is None or under["distance_from_open_pct"] is None:
            n_no_underlying += 1
            continue
        dist = under["distance_from_open_pct"]
        side = "Up" if dist > 0 else ("Down" if dist < 0 else None)
        if side is None:
            continue
        token_id = m["token_up_id"] if side == "Up" else m["token_down_id"]
        snap = _nearest_snapshot(conn, m["condition_id"], token_id, decision_ts)
        if snap is None or snap["best_ask"] is None:
            n_no_book += 1
            continue
        out.append({
            "condition_id": m["condition_id"], "market_title": m["market_title"],
            "asset_symbol": m["asset_symbol"], "open_time_utc": m["open_time_utc"],
            "decision_ts": decision_ts, "winner": m["winner"],
            "distance_from_open_pct": dist, "abs_distance_pct": abs(dist),
            "side": side, "entry_price": snap["best_ask"],
        })
    return out, n_no_underlying, n_no_book


def simulate(candidates, threshold_pct):
    trades = [c for c in candidates if c["abs_distance_pct"] >= threshold_pct]
    n = len(trades)
    if n == 0:
        return {"threshold_pct": threshold_pct, "n_trades": 0, "n_wins": 0, "n_losses": 0,
                "win_rate": None, "stake_total": 0.0, "payoff_total": 0.0,
                "gross_pnl": 0.0, "roi": None}
    n_wins = sum(1 for c in trades if c["side"] == c["winner"])
    stake_total = STAKE_USD * n
    payoff_total = 0.0
    for c in trades:
        shares = STAKE_USD / c["entry_price"]
        payoff_total += shares * 1.0 if c["side"] == c["winner"] else 0.0
    gross_pnl = payoff_total - stake_total
    return {
        "threshold_pct": threshold_pct, "n_trades": n, "n_wins": n_wins, "n_losses": n - n_wins,
        "win_rate": n_wins / n, "stake_total": stake_total, "payoff_total": payoff_total,
        "gross_pnl": gross_pnl, "roi": gross_pnl / stake_total,
    }


def run(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    candidates, n_no_under, n_no_book = gather_candidates(conn)
    candidates.sort(key=lambda c: c["open_time_utc"])
    n = len(candidates)
    split = int(n * 0.6)
    train, test = candidates[:split], candidates[split:]

    print(f"candidatos totales (subyacente Y order book disponibles en el instante de decision): {n}")
    print(f"  descartados por falta de subyacente en ese instante: {n_no_under}")
    print(f"  descartados por falta de order book del lado señalado: {n_no_book}")
    print(f"TRAIN: {len(train)} mercados ({fmt(train[0]['open_time_utc']) if train else '-'} -> "
          f"{fmt(train[-1]['open_time_utc']) if train else '-'})")
    print(f"TEST:  {len(test)} mercados ({fmt(test[0]['open_time_utc']) if test else '-'} -> "
          f"{fmt(test[-1]['open_time_utc']) if test else '-'})")

    print("\n--- barrido de umbral SOLO sobre TRAIN ---")
    train_results = [simulate(train, t) for t in CANDIDATE_THRESHOLDS_PCT]
    for r in train_results:
        if r["n_trades"] > 0:
            print(f"  T={r['threshold_pct']:.2f}%  n={r['n_trades']:4d}  win_rate={r['win_rate']*100:5.1f}%  "
                  f"gross_pnl=${r['gross_pnl']:8.2f}  roi={r['roi']*100:6.2f}%")
        else:
            print(f"  T={r['threshold_pct']:.2f}%  n=0")

    valid = [r for r in train_results if r["n_trades"] >= 20]  # umbral minimo de muestra para elegir T
    if not valid:
        print("\nNo hay ningun umbral con >=20 trades en TRAIN -- no se elige T, V1 queda sin validar.")
        return
    best = max(valid, key=lambda r: r["gross_pnl"])
    chosen_T = best["threshold_pct"]
    print(f"\nUMBRAL ELEGIDO (maximiza gross_pnl en TRAIN, con >=20 trades): T={chosen_T}%")

    print(f"\n--- evaluacion UNICA sobre TEST, con T={chosen_T}% ya congelado ---")
    test_result = simulate(test, chosen_T)
    print(json.dumps(test_result, indent=2))

    train_result_at_chosen = simulate(train, chosen_T)
    print(f"\n(referencia, TRAIN con el mismo T): n={train_result_at_chosen['n_trades']} "
          f"win_rate={train_result_at_chosen['win_rate']*100:.1f}% gross_pnl=${train_result_at_chosen['gross_pnl']:.2f} "
          f"roi={train_result_at_chosen['roi']*100:.2f}%")

    test_trades_executed = [c for c in test if c["abs_distance_pct"] >= chosen_T]
    return {
        "n_candidates": n, "n_no_underlying": n_no_under, "n_no_book": n_no_book,
        "train_range": (fmt(train[0]["open_time_utc"]), fmt(train[-1]["open_time_utc"])) if train else None,
        "test_range": (fmt(test[0]["open_time_utc"]), fmt(test[-1]["open_time_utc"])) if test else None,
        "train_sweep": train_results, "chosen_threshold_pct": chosen_T,
        "train_at_chosen": train_result_at_chosen, "test_result": test_result,
        "test_trades_executed": test_trades_executed, "all_test_candidates": test,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    args = p.parse_args()
    run(args.db_path)
