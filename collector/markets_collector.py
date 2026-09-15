"""
Flujo de mercado (1/2): descubre automáticamente los mercados BTC/ETH/SOL
Up/Down de 5 minutos (por slug + metadata de Gamma, no parseo de texto) y
mantiene su estado de resolución al día.
"""
import threading
import time

import db
import polymarket_api as pm
from config import ASSETS, RESOLVE_CHECK_INTERVAL_SEC


def upsert_market(m: pm.Market, conn=None):
    def _do(c):
        c.execute(
            """INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol,
                                     token_up_id, token_down_id, open_time_utc, close_time_utc,
                                     window_minutes, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(condition_id) DO NOTHING""",
            (m.condition_id, m.market_slug, m.market_title, m.asset_symbol,
             m.token_up, m.token_down, m.start_ts, m.end_ts, m.window_minutes, time.time()),
        )
    if conn is not None:
        _do(conn)
    else:
        with db.connect() as c:
            _do(c)


# Cache compartida del descubrimiento de mercados. Sin esto, los 4 flujos que
# necesitan saber cuál es la ventana activa llamaban cada uno a
# find_active_window() por asset en cada ciclo -- hasta 12 requests HTTP por
# vuelta, por flujo. Medido: eso hacía que el poll del líder cayera a 1 cada
# 5s en vez de 1/s. Las ventanas son de 5 minutos, así que cachear unos
# segundos no pierde nada.
_CACHE_TTL_S = 10
_cache = {"at": 0.0, "markets": {}}
_cache_lock = threading.Lock()


def active_markets(force: bool = False) -> dict:
    """Returns {condition_id: pm.Market} de TODAS las ventanas Up/Down vivas
    ahora mismo: 3 assets x 2 duraciones (5m y 15m). Antes solo devolvía la de
    5m por asset, y así los mercados de 15m que el líder también opera
    quedaban sin order book. También las inserta en `markets`."""
    now = time.time()
    with _cache_lock:
        if not force and now - _cache["at"] < _CACHE_TTL_S and _cache["markets"]:
            return _cache["markets"]

    out = {}
    with db.connect() as conn:
        for asset in ASSETS:
            for m in pm.find_active_windows(asset):
                upsert_market(m, conn)
                out[m.condition_id] = m

    with _cache_lock:
        # Si una vuelta falló entera (red caída), se conserva lo anterior en vez
        # de devolver un dict vacío que dejaría a los otros flujos sin mercados.
        if out:
            _cache["markets"] = out
            _cache["at"] = now
        return _cache["markets"]


def primary_markets_by_asset() -> dict:
    """{asset_lower: Market} quedándose con la ventana de 5m (la dominante:
    97% de los trades del líder) -- para los flujos que necesitan UNA ventana
    de referencia por asset, como el precio del subyacente."""
    out = {}
    for m in active_markets().values():
        asset = m.asset_symbol.lower()
        if m.window_minutes == 5 or asset not in out:
            out[asset] = m
    return out


def refresh_resolutions():
    """Poll for winner/resolution_source of any market still marked unresolved
    whose window has closed."""
    now = time.time()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT condition_id, close_time_utc FROM markets "
            "WHERE winner IS NULL AND close_time_utc IS NOT NULL AND close_time_utc < ?",
            (now,),
        ).fetchall()
    for row in rows:
        winner, source = pm.get_resolution(row["condition_id"])
        if winner is None:
            continue
        with db.connect() as conn:
            conn.execute(
                "UPDATE markets SET winner=?, resolution_source=?, resolution_time_utc=? "
                "WHERE condition_id=?",
                (winner, source, time.time(), row["condition_id"]),
            )
        db.log_event("markets", "resolved", {"condition_id": row["condition_id"], "winner": winner})


def run():
    db.log_event("markets", "start")
    last_resolve_check = 0
    while True:
        try:
            active_markets()
            if time.time() - last_resolve_check > RESOLVE_CHECK_INTERVAL_SEC:
                refresh_resolutions()
                last_resolve_check = time.time()
        except Exception as e:
            db.log_event("markets", "error", {"error": str(e)})
        time.sleep(5)


if __name__ == "__main__":
    db.init_db()
    run()
