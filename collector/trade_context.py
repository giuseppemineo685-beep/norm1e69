"""
Une leader_trades + orderbook_snapshots + underlying_prices en
-10/-5/-3/-1/0/+1/+3/+5/+10s alrededor de cada trade del líder, en una
tabla materializada (`trade_context`) -- es la sección "Market Context"
del export.

Los offsets positivos sirven para evaluar la CONSECUENCIA de una decisión
ya tomada -- nunca deben usarse como input de un backtest (look-ahead
bias). Por eso quedan marcados `usable_for_backtest=0` en el propio
esquema, no solo en la documentación.
"""
import time

import db

OFFSETS = (-10, -5, -3, -1, 0, 1, 3, 5, 10)
SNAPSHOT_TOLERANCE_S = 2.5  # if no real snapshot is this close to the target instant,
                             # context_available=False for that offset -- never invented

# Hueco de order book confirmado (auditoria Codex + verificacion propia, 2026-09-15):
# 2026-09-15 14:58:30 -> 16:35:18 UTC, 96.8 min sin captura real de order book (caida de
# VPN: ambos hosts de Polymarket inalcanzables, Binance siguio funcionando normal). Los
# trades del lider en esa ventana SI se recuperaron (poll de trades con paginacion hacia
# atras), pero su contexto de mercado es irrecuperable -- no existe order book historico.
# Se usan epochs fijos (no se recalculan en cada corrida) porque es un evento puntual ya
# cerrado, documentado, y verificado independientemente dos veces.
ORDERBOOK_GAP_START_TS = 1789484310.0  # 2026-09-15 14:58:30 UTC
ORDERBOOK_GAP_END_TS = 1789490118.0    # 2026-09-15 16:35:18 UTC


def _in_known_gap(trade_ts):
    return ORDERBOOK_GAP_START_TS <= trade_ts <= ORDERBOOK_GAP_END_TS


def _nearest_snapshot(conn, condition_id, token_id, target_ts):
    """El ÚLTIMO snapshot con timestamp <= target_ts, exigiendo AMBAS cosas a la vez:

      1. event_timestamp <= target_ts (el timestamp que declara el propio evento/mensaje)
      2. received_timestamp <= target_ts (el instante en que el collector REALMENTE lo recibió)

    Antes solo se exigía (1). Auditoria (propia + Codex, 2026-09-15) encontró que ~22% de
    los trades con contexto elegían al menos un snapshot cuyo evento declaraba un timestamp
    anterior al trade pero que en los HECHOS llegó al collector despues de que el trade ya
    había ocurrido (típico de WS: el mensaje trae su propio timestamp, que puede quedar algo
    atrás de cuándo realmente se procesó). Ese snapshot nunca pudo haber informado una
    decisión tomada en tiempo real -- es look-ahead de disponibilidad real, aunque el
    timestamp declarado por sí solo pareciera correcto. Exigir también (2) lo elimina.

    Con ambas condiciones activas, se ordena por received_at_utc_ms DESC (no por el
    timestamp declarado): entre los candidatos ya validos, el más recientemente RECIBIDO es
    el más informativo."""
    row = conn.execute(
        """SELECT *, COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) AS ts
           FROM orderbook_snapshots
           WHERE condition_id=? AND token_id=?
             AND COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) <= ?
             AND received_at_utc_ms/1000.0 <= ?
           ORDER BY received_at_utc_ms DESC
           LIMIT 1""",
        (condition_id, token_id, target_ts, target_ts),
    ).fetchone()
    if row is None:
        return None, None
    age = target_ts - row["ts"]          # siempre >= 0 por construcción
    if age > SNAPSHOT_TOLERANCE_S:
        return None, age                  # hay dato, pero demasiado viejo: no se usa
    return row, age


def _has_depth(conn, snapshot_id):
    """¿Ese snapshot trae niveles de profundidad reales?

    Los snapshots REST y los eventos WS `book` sí. Los eventos WS
    `price_change` NO: solo traen top-of-book, así que de ellos no sabemos
    qué hay detrás del mejor precio."""
    if snapshot_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM orderbook_levels WHERE snapshot_id=? LIMIT 1", (snapshot_id,)
    ).fetchone()
    return row is not None


def _depth(conn, snapshot_id, side):
    """Profundidad acumulada de un lado, o None si NO LA CONOCEMOS.

    Devolver 0 cuando no hay niveles registrados sería afirmar "no hay
    liquidez", que es una afirmación distinta y mucho más fuerte que "no
    registramos la profundidad en ese instante". Solo se devuelve 0 cuando el
    snapshot SÍ trae niveles y ese lado está genuinamente vacío."""
    if snapshot_id is None or not _has_depth(conn, snapshot_id):
        return None
    row = conn.execute(
        "SELECT COALESCE(SUM(size), 0) s FROM orderbook_levels WHERE snapshot_id=? AND side=?",
        (snapshot_id, side),
    ).fetchone()
    return row["s"]


def _executable_price(conn, snapshot_id, target_shares):
    """Walk ask levels of one snapshot to fill target_shares, VWAP-style.
    Returns None if there isn't enough stored depth to fully price it (still
    returns the partial VWAP -- better than nothing, but callers should treat
    a value from a thin book with care; that's what depth_* columns are for)."""
    if snapshot_id is None or not target_shares:
        return None
    levels = conn.execute(
        "SELECT price, size FROM orderbook_levels WHERE snapshot_id=? AND side='ask' ORDER BY level",
        (snapshot_id,),
    ).fetchall()
    remaining = target_shares
    cost = 0.0
    filled = 0.0
    for lvl in levels:
        take = min(remaining, lvl["size"])
        cost += take * lvl["price"]
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled == 0:
        return None
    return cost / filled


def _nearest_underlying(conn, asset_symbol, target_ts):
    """Misma regla que el order book: la última lectura EN O ANTES del
    instante pedido, nunca una posterior.

    NOTA: a diferencia del order book, acá NO hay una distinción real entre
    'event timestamp' y 'received timestamp' que corregir -- confirmado en la
    auditoría: underlying_price_collector.py escribe source_timestamp_utc y
    received_at_utc con el MISMO valor (el instante local en que se lanzó el
    request, no cuando llegó la respuesta). Filtrar por received_at_utc<=target_ts
    (como ya hace esta query) es correcto pero no aporta nada adicional hasta
    que el collector capture el timestamp real de recepción de la respuesta
    HTTP -- eso es un cambio de instrumentación en vivo, fuera de alcance de
    esta capa de derivación (ver corrección propuesta #1 del audit)."""
    row = conn.execute(
        """SELECT * FROM underlying_prices
           WHERE asset_symbol=? AND received_at_utc <= ?
           ORDER BY received_at_utc DESC LIMIT 1""",
        (asset_symbol, target_ts),
    ).fetchone()
    if row is None or (target_ts - row["received_at_utc"]) > SNAPSHOT_TOLERANCE_S:
        return None
    return row


def _build_one(conn, trade):
    trade_id = trade["id"]
    condition_id = trade["condition_id"]
    asset_symbol = trade["asset_symbol"]
    trade_ts = trade["source_timestamp_utc"]
    outcome = trade["outcome"]
    shares = trade["shares"]

    market = conn.execute(
        "SELECT token_up_id, token_down_id FROM markets WHERE condition_id=?", (condition_id,)
    ).fetchone()
    if market is None:
        # Sin metadata del mercado no hay contexto posible (pasa con los trades
        # de BACKFILL, cuyos mercados ya estaban cerrados antes de que este
        # collector existiera). Se marcan igual como procesados con
        # context_available=0 -- si no, quedaban para siempre al frente de la
        # cola y BLOQUEABAN el contexto de todos los trades nuevos.
        for offset in OFFSETS:
            conn.execute(
                """INSERT OR IGNORE INTO trade_context
                   (leader_trade_id, offset_seconds, usable_for_backtest, context_available)
                   VALUES (?, ?, ?, 0)""",
                (trade_id, offset, 1 if offset <= 0 else 0),
            )
        return
    token_own = market["token_up_id"] if outcome == "Up" else market["token_down_id"]
    token_other = market["token_down_id"] if outcome == "Up" else market["token_up_id"]
    trade_in_known_gap = _in_known_gap(trade_ts)
    is_live_non_startup = trade["collection_method"] == "LIVE" and not trade["is_startup_batch"]

    inv = conn.execute(
        "SELECT * FROM leader_inventory_timeline WHERE after_trade_id=?", (trade_id,)
    ).fetchone()
    if inv:
        up_after, down_after = inv["up_shares"], inv["down_shares"]
        up_before = up_after - (shares if outcome == "Up" else 0)
        down_before = down_after - (shares if outcome == "Down" else 0)
    else:
        up_before = down_before = up_after = down_after = None

    for offset in OFFSETS:
        target_ts = trade_ts + offset
        own_snap, own_age = _nearest_snapshot(conn, condition_id, token_own, target_ts)
        other_snap, _ = _nearest_snapshot(conn, condition_id, token_other, target_ts)
        underlying = _nearest_underlying(conn, asset_symbol, target_ts) if asset_symbol else None

        context_available = own_snap is not None and other_snap is not None
        own_id = own_snap["id"] if own_snap else None
        other_id = other_snap["id"] if other_snap else None

        # Calidad del contexto: 'executable' solo si AMBOS lados traen niveles de
        # profundidad reales (si no, el coste de armar el par no es calculable).
        if not context_available:
            context_quality = None
        elif _has_depth(conn, own_id) and _has_depth(conn, other_id):
            context_quality = "executable"
        else:
            context_quality = "indicative"

        best_bid_own = own_snap["best_bid"] if own_snap else None
        best_ask_own = own_snap["best_ask"] if own_snap else None
        best_bid_other = other_snap["best_bid"] if other_snap else None
        best_ask_other = other_snap["best_ask"] if other_snap else None

        executable = _executable_price(conn, own_id, shares)
        opposite_leg_price = best_ask_other
        combined = (executable + opposite_leg_price) if (executable is not None and opposite_leg_price is not None) else None

        up_bid, up_ask, up_dbid, up_dask = (best_bid_own, best_ask_own, _depth(conn, own_id, "bid"), _depth(conn, own_id, "ask")) \
            if outcome == "Up" else (best_bid_other, best_ask_other, _depth(conn, other_snap["id"] if other_snap else None, "bid"),
                                      _depth(conn, other_snap["id"] if other_snap else None, "ask"))
        down_bid, down_ask, down_dbid, down_dask = (best_bid_other, best_ask_other, _depth(conn, other_snap["id"] if other_snap else None, "bid"),
                                                      _depth(conn, other_snap["id"] if other_snap else None, "ask")) \
            if outcome == "Up" else (best_bid_own, best_ask_own, _depth(conn, own_id, "bid"), _depth(conn, own_id, "ask"))

        snapshot_up_id = own_id if outcome == "Up" else other_id
        snapshot_down_id = other_id if outcome == "Up" else own_id
        underlying_id = underlying["id"] if underlying else None

        # Usable para aprendizaje/backtest de una estrategia AUTONOMA (no copy-trading):
        # exige TODO lo anterior (offset<=0, ambos libros bajo la regla conservadora,
        # subyacente disponible) MAS que el trade sea LIVE genuino (no startup, no
        # BACKFILL importado -- esos nunca tienen order book real) Y que no caiga dentro
        # del hueco de 96.8min sin order book, donde el contexto es irrecuperable aunque
        # el trade en si se haya recuperado.
        usable_for_strategy_learning = (
            offset <= 0 and context_available and underlying is not None
            and is_live_non_startup and not trade_in_known_gap
        )

        conn.execute(
            """INSERT OR IGNORE INTO trade_context
               (leader_trade_id, offset_seconds, usable_for_backtest, context_available,
                underlying_available, context_quality, snapshot_age_s,
                snapshot_up_id, snapshot_down_id, underlying_price_id, usable_for_strategy_learning,
                best_bid_up, best_ask_up, depth_bid_up, depth_ask_up,
                best_bid_down, best_ask_down, depth_bid_down, depth_ask_down,
                spread_up, spread_down, underlying_price, underlying_distance_from_open_pct,
                executable_price_for_leader_size, opposite_leg_price, combined_cost_to_pair,
                leader_up_shares_snapshot, leader_down_shares_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, offset, 1 if offset <= 0 else 0, 1 if context_available else 0,
             1 if underlying is not None else 0, context_quality, own_age,
             snapshot_up_id, snapshot_down_id, underlying_id, 1 if usable_for_strategy_learning else 0,
             up_bid, up_ask, up_dbid, up_dask, down_bid, down_ask, down_dbid, down_dask,
             (up_ask - up_bid) if (up_ask is not None and up_bid is not None) else None,
             (down_ask - down_bid) if (down_ask is not None and down_bid is not None) else None,
             underlying["price"] if underlying else None,
             underlying["distance_from_open_pct"] if underlying else None,
             executable, opposite_leg_price, combined,
             up_before if offset < 0 else up_after,
             down_before if offset < 0 else down_after),
        )


def recompute_pending(batch_size=200):
    with db.connect() as conn:
        pending = conn.execute(
            """SELECT lt.* FROM leader_trades lt
               LEFT JOIN trade_context tc ON tc.leader_trade_id = lt.id AND tc.offset_seconds = 0
               WHERE tc.id IS NULL
               ORDER BY lt.source_timestamp_utc DESC LIMIT ?""",
            (batch_size,),
        ).fetchall()
        for trade in pending:
            # only build context for trades old enough that the +10s snapshot could exist
            if time.time() - trade["source_timestamp_utc"] < 12:
                continue
            _build_one(conn, trade)


def run():
    db.log_event("trade_context", "start")
    while True:
        try:
            recompute_pending()
        except Exception as e:
            db.log_event("trade_context", "error", {"error": str(e)})
        time.sleep(5)


if __name__ == "__main__":
    db.init_db()
    run()


def rebuild_all():
    """Recalcula TODA la tabla derivada trade_context desde cero.

    trade_context no contiene ningún dato capturado: se deriva enteramente de
    leader_trades + orderbook_snapshots + underlying_prices, que no se tocan.
    Recalcular es la forma correcta de aplicar una corrección de lógica (por
    ejemplo, pasar de profundidad 0 a NULL cuando no se conoce) a las filas ya
    construidas -- no es pérdida de datos.
    """
    with db.connect() as conn:
        before = conn.execute("SELECT count(*) c FROM trade_context").fetchone()["c"]
        conn.execute("DELETE FROM trade_context")
    print(f"trade_context: {before} filas derivadas a recalcular "
          f"(fuentes intactas: leader_trades, orderbook_snapshots, underlying_prices)")
    total = 0
    while True:
        with db.connect() as conn:
            pending = conn.execute(
                """SELECT lt.* FROM leader_trades lt
                   LEFT JOIN trade_context tc ON tc.leader_trade_id = lt.id AND tc.offset_seconds = 0
                   WHERE tc.id IS NULL ORDER BY lt.source_timestamp_utc DESC LIMIT 500""").fetchall()
            if not pending:
                break
            for trade in pending:
                _build_one(conn, trade)
                total += 1
        print(f"  recalculados {total} trades...", flush=True)
    with db.connect() as conn:
        after = conn.execute("SELECT count(*) c FROM trade_context").fetchone()["c"]
    print(f"listo: {after} filas")
