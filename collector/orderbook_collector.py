"""
Order book: WebSocket oficial de Polymarket para cada cambio (baja
latencia) + snapshot REST completo de respaldo cada 1s (no depender 100%
del WS si se cae o si un mensaje se pierde). Ambas fuentes escriben a la
misma tabla `orderbook_snapshots` con `source` distinguiendo 'WS'/'REST'.
"""
import asyncio
import json
import time

import websockets

import db
import polymarket_api as pm
from config import ORDERBOOK_SNAPSHOT_INTERVAL_SEC, WS_MARKET_URL
from markets_collector import active_markets


# ---------------------------------------------------------------- REST ----

def _insert_snapshot(conn, source_ts, received_ms, condition_id, token_id, outcome,
                      seconds_since_open, seconds_to_close, book, source, message_seq=None,
                      write_levels=True):
    """write_levels=False para eventos que solo traen top-of-book (price_change
    del WS): se guarda el mejor bid/ask real, pero NO se escriben filas en
    orderbook_levels, porque no conocemos la profundidad en ese instante --
    fabricar un libro de 1 nivel sería inventar datos que no existen."""
    bids = pm.top_levels(book.get("bids") or [], reverse=True)   # best bid = highest price
    asks = pm.top_levels(book.get("asks") or [], reverse=False)  # best ask = lowest price
    best_bid = float(bids[0]["price"]) if bids else None
    best_bid_size = float(bids[0]["size"]) if bids and bids[0].get("size") is not None else None
    best_ask = float(asks[0]["price"]) if asks else None
    best_ask_size = float(asks[0]["size"]) if asks and asks[0].get("size") is not None else None
    mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    last_trade = book.get("last_trade_price")
    last_trade = float(last_trade) if last_trade not in (None, "") else None

    cur = conn.execute(
        """INSERT INTO orderbook_snapshots
           (source_timestamp_utc, received_at_utc_ms, condition_id, token_id, outcome,
            seconds_since_open, seconds_to_close, best_bid, best_bid_size, best_ask, best_ask_size,
            mid_price, spread, last_trade_price, message_seq, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_ts, received_ms, condition_id, token_id, outcome, seconds_since_open,
         seconds_to_close, best_bid, best_bid_size, best_ask, best_ask_size, mid, spread,
         last_trade, message_seq, source),
    )
    snap_id = cur.lastrowid
    if not write_levels:
        return
    level_rows = [(snap_id, "bid", i + 1, float(l["price"]), float(l["size"])) for i, l in enumerate(bids)]
    level_rows += [(snap_id, "ask", i + 1, float(l["price"]), float(l["size"])) for i, l in enumerate(asks)]
    if level_rows:
        conn.executemany(
            "INSERT INTO orderbook_levels (snapshot_id, side, level, price, size) VALUES (?, ?, ?, ?, ?)",
            level_rows,
        )


def rest_snapshot_once(markets: dict):
    now = time.time()
    with db.connect() as conn:
        for asset, m in markets.items():
            for token_id, outcome in ((m.token_up, "Up"), (m.token_down, "Down")):
                try:
                    book = pm.get_book(token_id)
                except Exception as e:
                    db.log_event("orderbook", "error", {"token_id": token_id, "error": str(e)})
                    continue
                since_open = (now - m.start_ts) if m.start_ts else None
                to_close = (m.end_ts - now) if m.end_ts else None
                _insert_snapshot(conn, now, now * 1000, m.condition_id, token_id, outcome,
                                  since_open, to_close, book, source="REST")


def run_rest():
    db.log_event("orderbook_rest", "start")
    while True:
        try:
            markets = active_markets()
            rest_snapshot_once(markets)
        except Exception as e:
            db.log_event("orderbook_rest", "error", {"error": str(e)})
        time.sleep(ORDERBOOK_SNAPSHOT_INTERVAL_SEC)


# ----------------------------------------------------------------- WS -----

class TokenRegistry:
    """token_id -> (condition_id, outcome, start_ts, end_ts), refreshed from
    active_markets() so the WS client knows what to (un)subscribe to and how
    to compute seconds_since_open/to_close for incoming events."""

    def __init__(self):
        self.map = {}

    def refresh(self):
        markets = active_markets()
        new_map = {}
        for asset, m in markets.items():
            new_map[m.token_up] = (m.condition_id, "Up", m.start_ts, m.end_ts)
            new_map[m.token_down] = (m.condition_id, "Down", m.start_ts, m.end_ts)
        added = set(new_map) - set(self.map)
        removed = set(self.map) - set(new_map)
        self.map = new_map
        return added, removed


async def _heartbeat(ws):
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except Exception:
            return


async def _resubscribe_loop(ws, registry: TokenRegistry):
    while True:
        await asyncio.sleep(5)
        added, removed = registry.refresh()
        try:
            if added:
                await ws.send(json.dumps({"assets_ids": list(added), "operation": "subscribe"}))
            if removed:
                await ws.send(json.dumps({"assets_ids": list(removed), "operation": "unsubscribe"}))
        except Exception:
            return


# Último top-of-book guardado por token, para deduplicar `price_change`.
# Medido sobre datos reales: el 97.9% de los eventos price_change llegan con
# el MISMO best_bid/best_ask que el anterior (cambió algo más profundo del
# libro, que en esas filas no guardamos igual). Sin dedupe eran ~29M filas y
# ~21GB por día; con dedupe baja ~48x sin perder ni un cambio real de
# top-of-book. La profundidad completa sigue viniendo del snapshot REST.
_last_top = {}


def _handle_message(conn, registry: TokenRegistry, msg: dict):
    now_ms = time.time() * 1000
    event_type = msg.get("event_type")
    ts = msg.get("timestamp")
    source_ts = float(ts) / 1000.0 if ts and float(ts) > 1e12 else (float(ts) if ts else None)

    if event_type == "book":
        token_id = msg.get("asset_id")
        info = registry.map.get(token_id)
        condition_id, outcome, start_ts, end_ts = info if info else (msg.get("market"), None, None, None)
        since_open = (source_ts - start_ts) if (source_ts and start_ts) else None
        to_close = (end_ts - source_ts) if (source_ts and end_ts) else None
        book = {"bids": msg.get("bids") or [], "asks": msg.get("asks") or []}
        _insert_snapshot(conn, source_ts, now_ms, condition_id, token_id, outcome,
                          since_open, to_close, book, source="WS", message_seq=msg.get("hash"))

    elif event_type == "price_change":
        for change in msg.get("price_changes", []):
            token_id = change.get("asset_id")
            info = registry.map.get(token_id)
            condition_id, outcome, start_ts, end_ts = info if info else (msg.get("market"), None, None, None)
            since_open = (source_ts - start_ts) if (source_ts and start_ts) else None
            to_close = (end_ts - source_ts) if (source_ts and end_ts) else None
            bb = change.get("best_bid")
            ba = change.get("best_ask")
            if _last_top.get(token_id) == (bb, ba):
                continue  # nada nuevo en el top-of-book: no se guarda fila repetida
            _last_top[token_id] = (bb, ba)
            # Solo top-of-book: el evento no dice cuánta profundidad hay detrás,
            # así que `size` NO se rellena (era el tamaño del cambio, no el del
            # nivel) y no se escriben niveles.
            book = {
                "bids": [{"price": bb, "size": None}] if bb else [],
                "asks": [{"price": ba, "size": None}] if ba else [],
            }
            _insert_snapshot(conn, source_ts, now_ms, condition_id, token_id, outcome,
                              since_open, to_close, book, source="WS_price_change",
                              write_levels=False)


async def run_ws_once(registry: TokenRegistry):
    async with websockets.connect(WS_MARKET_URL, ping_interval=None, open_timeout=10) as ws:
        registry.refresh()
        if registry.map:
            await ws.send(json.dumps({"assets_ids": list(registry.map), "type": "market"}))
        db.log_event("orderbook_ws", "connect", {"n_tokens": len(registry.map)})

        heartbeat_task = asyncio.create_task(_heartbeat(ws))
        resub_task = asyncio.create_task(_resubscribe_loop(ws, registry))
        try:
            async for raw in ws:
                if raw == "PONG":
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                messages = msg if isinstance(msg, list) else [msg]
                with db.connect() as conn:
                    for m in messages:
                        try:
                            _handle_message(conn, registry, m)
                        except Exception as e:
                            db.log_event("orderbook_ws", "error", {"error": str(e)})
        finally:
            heartbeat_task.cancel()
            resub_task.cancel()


async def run_ws():
    registry = TokenRegistry()
    backoff = 1
    while True:
        try:
            await run_ws_once(registry)
            backoff = 1  # clean disconnect (server closed) -- reset backoff
        except Exception as e:
            db.log_event("orderbook_ws", "reconnect", {"error": str(e), "backoff_s": backoff})
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def run_ws_blocking():
    db.log_event("orderbook_ws", "start")
    asyncio.run(run_ws())


if __name__ == "__main__":
    db.init_db()
    import threading
    threading.Thread(target=run_rest, daemon=True).start()
    run_ws_blocking()
