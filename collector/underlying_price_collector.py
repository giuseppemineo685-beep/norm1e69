"""
Precio del subyacente (BTC/ETH/SOL) con frecuencia suficiente para
movimientos de 1/3/5/10s. Ver polymarket_api.get_underlying_price para la
justificación de la fuente elegida (Binance spot, documentada como
alternativa a Chainlink -- ver docs/LIMITATIONS.md).
"""
import concurrent.futures
import time

import db
import polymarket_api as pm
from config import ASSETS, UNDERLYING_PRICE_POLL_INTERVAL_SEC
from markets_collector import active_markets


_open_ref_cache = {}  # condition_id -> precio de referencia al abrir la ventana


def _open_reference_price(condition_id, price):
    """Devuelve el precio de referencia de apertura del mercado, sembrándolo con
    la primera lectura si aún no existe. Cacheado en memoria: es un valor fijo
    por ventana, no hacía falta ir a la DB en cada lectura de precio."""
    if condition_id in _open_ref_cache:
        return _open_ref_cache[condition_id]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT open_reference_price FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        if row is None:
            return None
        ref = row["open_reference_price"]
        if ref is None:
            ref = price
            conn.execute(
                "UPDATE markets SET open_reference_price=? WHERE condition_id=?", (ref, condition_id)
            )
    _open_ref_cache[condition_id] = ref
    return ref


def poll_once(markets: dict):
    """Los 3 assets se piden EN PARALELO: en serie eran ~400ms cada uno (1.2s
    por vuelta), lo que hacía imposible el objetivo de 1 lectura/s por asset
    necesario para analizar movimientos de 1-3 segundos. Las escrituras van
    todas en una sola transacción."""
    now = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ASSETS)) as ex:
        results = dict(zip(ASSETS, ex.map(pm.get_underlying_price, ASSETS)))

    rows = []
    for asset, result in results.items():
        if result is None:
            continue
        price, source = result
        m = markets.get(asset)
        condition_id = m.condition_id if m else None
        open_ref = None
        seconds_to_close = None
        if m:
            open_ref = _open_reference_price(condition_id, price)
            if m.end_ts:
                seconds_to_close = m.end_ts - now

        dist_abs = (price - open_ref) if open_ref else None
        dist_pct = (dist_abs / open_ref * 100) if (dist_abs is not None and open_ref) else None
        rows.append((now, now, asset.upper(), source, price, open_ref, dist_abs, dist_pct,
                     seconds_to_close, condition_id))

    if rows:
        with db.connect() as conn:
            conn.executemany(
                """INSERT INTO underlying_prices
                   (source_timestamp_utc, received_at_utc, asset_symbol, source, price,
                    market_open_reference_price, distance_from_open_abs, distance_from_open_pct,
                    seconds_to_close, condition_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )


def run():
    db.log_event("underlying", "start")
    # Cadencia por período (ver comentario en leader_trades_collector.run):
    # con 3 assets a ~400ms cada request, un sleep fijo encima dejaba el ritmo
    # muy por debajo del objetivo de 1/s por asset.
    next_at = time.time()
    while True:
        next_at += UNDERLYING_PRICE_POLL_INTERVAL_SEC
        try:
            markets = active_markets()
            poll_once(markets)
        except Exception as e:
            db.log_event("underlying", "error", {"error": str(e)})
        time.sleep(max(0.0, next_at - time.time()))


if __name__ == "__main__":
    db.init_db()
    run()
