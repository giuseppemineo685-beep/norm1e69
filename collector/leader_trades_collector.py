"""
Dataset 1 (líder): TODOS los trades detectados de LEADER_WALLET, incluidos
los <$1 y los que cualquier otro sistema descartaría. Poll independiente
al mismo endpoint que ya usa scripts/live_trader.py, pero proceso y
máquina distintos -- esto nunca escribe en state/live_state.json ni
depende de que live_trader.py esté corriendo.
"""
import json
import time

import db
import polymarket_api as pm
from config import LEADER_WALLET, LEADER_POLL_INTERVAL_SEC, MAX_GAP_FILL_PAGES

_market_cache = {}  # condition_id -> (market_slug, asset_symbol, open_ts, close_ts), refreshed periodically


def _market_info(conn, condition_id, cache_ttl_s=30):
    now = time.time()
    cached = _market_cache.get(condition_id)
    if cached and now - cached[-1] < cache_ttl_s:
        return cached[:-1]
    row = conn.execute(
        "SELECT market_slug, asset_symbol, open_time_utc, close_time_utc FROM markets WHERE condition_id=?",
        (condition_id,),
    ).fetchone()
    info = (row["market_slug"], row["asset_symbol"], row["open_time_utc"], row["close_time_utc"]) if row \
        else (None, None, None, None)
    _market_cache[condition_id] = info + (now,)
    return info


def _insert_trade(conn, t, now):
    condition_id = t.get("conditionId")
    slug, asset_symbol, open_ts, close_ts = _market_info(conn, condition_id) if condition_id \
        else (None, None, None, None)
    source_ts = float(t["timestamp"])
    row = (
        LEADER_WALLET,
        t.get("id"),
        t["transactionHash"],
        source_ts,
        now,
        (now - source_ts) * 1000.0,
        condition_id,
        slug,
        t.get("title"),
        asset_symbol,
        t.get("asset"),
        t.get("outcome"),
        t.get("side"),
        float(t["price"]),
        float(t["size"]),
        float(t["price"]) * float(t["size"]),
        (source_ts - open_ts) if open_ts else None,
        (close_ts - source_ts) if close_ts else None,
        t.get("makerTaker") or t.get("maker_taker"),
        json.dumps(t, default=str),
        "LIVE",
    )
    cur = conn.execute(
        """INSERT OR IGNORE INTO leader_trades
           (leader_wallet, trade_id, transaction_hash, source_timestamp_utc, received_at_utc,
            detection_latency_ms, condition_id, market_slug, market_title, asset_symbol,
            token_id, outcome, side, price, shares, usdc_amount,
            seconds_since_market_open, seconds_to_market_close, maker_taker, raw_payload,
            collection_method)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        row,
    )
    return cur.rowcount > 0


# Marca de agua: el timestamp más nuevo que ya tenemos guardado. Se carga
# desde la DB al arrancar, así un reinicio no pierde la referencia ni
# vuelve a considerar "hueco" todo lo que ya estaba capturado.
_high_water = {"ts": None}


def _load_high_water():
    with db.connect() as conn:
        row = conn.execute(
            "SELECT max(source_timestamp_utc) m FROM leader_trades WHERE leader_wallet=?",
            (LEADER_WALLET,),
        ).fetchone()
    _high_water["ts"] = row["m"] if row and row["m"] else None
    return _high_water["ts"]


def _insert_page(trades, now):
    """Toda la página en UNA transacción: antes era una por trade (100 commits
    por poll), lo que junto al firehose del order book hacía que el poll del
    líder se arrastrara a 1 cada 5s en vez de 1/s."""
    n_new = 0
    with db.connect() as conn:
        for t in trades:
            try:
                if _insert_trade(conn, t, now):
                    n_new += 1
            except Exception as e:
                # un trade malo nunca debe abortar el resto del lote (este bug exacto
                # está documentado en live_trader.py como causa de demoras de +300s)
                db.log_event("leader_trades", "error",
                             {"trade": t.get("transactionHash"), "error": str(e)})
    return n_new


def _fill_gap(oldest_in_page, now):
    """Hay hueco: el trade más viejo de la página 0 es MÁS NUEVO que lo último
    que teníamos guardado, así que entre medio hubo trades que nunca vimos.
    Se pagina hacia atrás con offset hasta cruzar la marca de agua.

    Verificado empíricamente que `offset` funciona en data-api
    (páginas consecutivas devuelven rangos de tiempo distintos, solapamiento
    ~0), así que esto RECUPERA los trades perdidos en vez de solo avisar."""
    hw = _high_water["ts"]
    n_filled = 0
    pages = 0
    for page in range(1, MAX_GAP_FILL_PAGES + 1):
        try:
            extra = pm.get_leader_trades(LEADER_WALLET, limit=100, offset=page * 100)
        except Exception as e:
            db.log_event("leader_trades", "gap_fill_error", {"page": page, "error": str(e)})
            break
        pages += 1
        if not extra:
            break
        n_filled += _insert_page(extra, now)
        page_oldest = min(float(t["timestamp"]) for t in extra)
        if page_oldest <= hw:
            break  # cruzamos la marca de agua: el hueco quedó cerrado
    db.log_event("leader_trades", "gap_filled", {
        "hueco_desde": hw, "hueco_hasta": oldest_in_page,
        "segundos_de_hueco": oldest_in_page - hw,
        "trades_recuperados": n_filled, "paginas_extra": pages,
        "cerrado": pages < MAX_GAP_FILL_PAGES})
    return n_filled, pages


def poll_once():
    now = time.time()
    error = None
    n_returned = n_new = n_gap_filled = gap_pages = 0
    oldest = newest = None
    try:
        trades = pm.get_leader_trades(LEADER_WALLET, limit=100)
        n_returned = len(trades)
        n_new = _insert_page(trades, now)

        if trades:
            timestamps = [float(t["timestamp"]) for t in trades]
            oldest, newest = min(timestamps), max(timestamps)

            # Hueco real = el trade más viejo de esta página es más nuevo que la
            # marca de agua. Ojo: n_returned==100 por sí solo NO es señal de
            # pérdida -- con este líder 100 trades cubren ~7 minutos, muy por
            # encima del intervalo de 1s entre polls (medido, no asumido).
            if _high_water["ts"] is not None and oldest > _high_water["ts"]:
                n_gap_filled, gap_pages = _fill_gap(oldest, now)

            if _high_water["ts"] is None or newest > _high_water["ts"]:
                _high_water["ts"] = newest
    except Exception as e:
        error = str(e)

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO leader_poll_log (ts, n_returned, n_new, error, oldest_ts_in_page, "
            "newest_ts_in_page, n_gap_filled, gap_pages_fetched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, n_returned, n_new, error, oldest, newest, n_gap_filled, gap_pages))

    return n_returned, n_new, error


def run():
    hw = _load_high_water()
    db.log_event("leader_trades", "start", {"wallet": LEADER_WALLET, "high_water_ts": hw})
    # Cadencia por PERÍODO, no sleep fijo: se duerme solo lo que falta para
    # completar el intervalo. Con sleep fijo, el tiempo de trabajo (HTTP ~250ms
    # + inserts) se sumaba al intervalo y el ritmo real caía a ~0.43/s en vez
    # de 1/s. Tampoco acumula deriva.
    next_at = time.time()
    while True:
        next_at += LEADER_POLL_INTERVAL_SEC
        n_returned, n_new, error = poll_once()
        if error:
            print(f"[leader_trades] poll error: {error}", flush=True)
        time.sleep(max(0.0, next_at - time.time()))


if __name__ == "__main__":
    db.init_db()
    run()
