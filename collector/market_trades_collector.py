"""
Dataset 2 (mercado): TODAS las ejecuciones públicas de los mercados
trackeados (no solo del líder) -- permite calcular precio ejecutable con
profundidad real y separar al líder del resto del flujo del mercado.
"""
import time

import db
import polymarket_api as pm
from config import MARKET_TRADES_POLL_INTERVAL_SEC
from markets_collector import active_markets


def poll_market(condition_id):
    now = time.time()
    n_new = 0
    try:
        trades = pm.get_market_trades(condition_id, limit=100)
    except Exception as e:
        db.log_event("market_trades", "error", {"condition_id": condition_id, "error": str(e)})
        return 0
    rows = []
    for t in trades:
        try:
            rows.append((
                float(t["timestamp"]), now, condition_id, t.get("asset"), t.get("outcome"),
                t.get("side"), float(t["price"]), float(t["size"]),
                float(t["price"]) * float(t["size"]), t.get("transactionHash"),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return 0
    with db.connect() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO market_trades
                   (source_timestamp_utc, received_at_utc, condition_id, token_id, outcome, side,
                    price, shares, usdc_amount, transaction_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                r,
            )
            n_new += cur.rowcount > 0
    return n_new


def run():
    db.log_event("market_trades", "start")
    while True:
        try:
            markets = active_markets()
            for m in markets.values():
                poll_market(m.condition_id)
        except Exception as e:
            db.log_event("market_trades", "error", {"error": str(e)})
        time.sleep(MARKET_TRADES_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    db.init_db()
    run()
