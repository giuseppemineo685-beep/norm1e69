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


def _nearest_snapshot(conn, condition_id, token_id, target_ts):
    row = conn.execute(
        """SELECT * FROM orderbook_snapshots
           WHERE condition_id=? AND token_id=?
           ORDER BY ABS(COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) - ?) ASC
           LIMIT 1""",
        (condition_id, token_id, target_ts),
    ).fetchone()
    if row is None:
        return None, None
    actual_ts = row["source_timestamp_utc"] or (row["received_at_utc_ms"] / 1000.0)
    age = abs(actual_ts - target_ts)
    if age > SNAPSHOT_TOLERANCE_S:
        return None, age
    return row, age


def _depth(conn, snapshot_id, side):
    if snapshot_id is None:
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
    row = conn.execute(
        """SELECT * FROM underlying_prices WHERE asset_symbol=?
           ORDER BY ABS(received_at_utc - ?) ASC LIMIT 1""",
        (asset_symbol, target_ts),
    ).fetchone()
    if row is None or abs(row["received_at_utc"] - target_ts) > SNAPSHOT_TOLERANCE_S:
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

        conn.execute(
            """INSERT OR IGNORE INTO trade_context
               (leader_trade_id, offset_seconds, usable_for_backtest, context_available, snapshot_age_s,
                best_bid_up, best_ask_up, depth_bid_up, depth_ask_up,
                best_bid_down, best_ask_down, depth_bid_down, depth_ask_down,
                spread_up, spread_down, underlying_price, underlying_distance_from_open_pct,
                executable_price_for_leader_size, opposite_leg_price, combined_cost_to_pair,
                leader_up_shares_snapshot, leader_down_shares_snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, offset, 1 if offset <= 0 else 0, 1 if context_available else 0, own_age,
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
