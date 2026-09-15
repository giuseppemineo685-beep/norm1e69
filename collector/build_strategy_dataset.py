"""
Dataset limpio para aprendizaje de estrategia (Fase 2): une, por cada trade
usable_for_strategy_learning=1, el estado de mercado disponible en ese
instante (offset=0, bajo la regla conservadora anti-lookahead) + el
inventario previo del lider (offset=-1) + la decision observada + el
resultado posterior (resolucion del mercado, solo para evaluacion, nunca
como input). No es copy-trading: el objetivo es tener, por fila, todo lo
que una estrategia AUTONOMA habria podido observar en ese instante.

Uso:  python3 build_strategy_dataset.py --db-path /ruta/db.db [salida.csv]
"""
import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def build(db_path, out_csv):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
          lt.id AS leader_trade_id, lt.condition_id, lt.market_title, lt.asset_symbol,
          lt.source_timestamp_utc, lt.outcome AS trade_outcome, lt.side, lt.price, lt.shares,
          lt.usdc_amount, lt.seconds_since_market_open, lt.seconds_to_market_close,

          tc0.best_bid_up, tc0.best_ask_up, tc0.depth_bid_up, tc0.depth_ask_up,
          tc0.best_bid_down, tc0.best_ask_down, tc0.depth_bid_down, tc0.depth_ask_down,
          tc0.spread_up, tc0.spread_down,
          tc0.underlying_price, tc0.underlying_distance_from_open_pct,
          tc0.executable_price_for_leader_size, tc0.opposite_leg_price, tc0.combined_cost_to_pair,
          tc0.context_quality, tc0.snapshot_age_s,
          tc0.snapshot_up_id, tc0.snapshot_down_id, tc0.underlying_price_id,

          tcm1.leader_up_shares_snapshot AS up_shares_before,
          tcm1.leader_down_shares_snapshot AS down_shares_before,

          m.winner AS market_winner, m.window_minutes, m.open_time_utc, m.close_time_utc

        FROM leader_trades lt
        JOIN trade_context tc0 ON tc0.leader_trade_id = lt.id AND tc0.offset_seconds = 0
        LEFT JOIN trade_context tcm1 ON tcm1.leader_trade_id = lt.id AND tcm1.offset_seconds = -1
        LEFT JOIN markets m ON m.condition_id = lt.condition_id
        WHERE tc0.usable_for_strategy_learning = 1
        ORDER BY lt.source_timestamp_utc ASC
    """).fetchall()

    fieldnames = list(rows[0].keys()) if rows else []
    # derivadas puramente descriptivas (no inputs de decision)
    fieldnames += ["traded_side_won"]

    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            d = dict(r)
            winner = d.get("market_winner")
            d["traded_side_won"] = (None if winner is None
                                     else int(d["trade_outcome"] == winner))
            w.writerow(d)

    print(f"{len(rows)} filas -> {out_csv}")
    return len(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    p.add_argument("out_csv", nargs="?", default=None)
    args = p.parse_args()
    out = args.out_csv or str(Path(__file__).resolve().parent / "export" / "strategy_learning_dataset.csv")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sys.exit(0 if build(args.db_path, out) else 1)
