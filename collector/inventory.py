"""
leader_inventory_timeline: reconstruye, después de cada trade del líder en
un mercado, cuánto lleva acumulado en UP y DOWN, su VWAP por lado, cuántas
shares están "emparejadas" (cubiertas de verdad) y cuántas quedan como
exposición direccional.

Metodología de atribución de coste (documentada explícitamente, como pide
el spec): las shares emparejadas = min(up_shares, down_shares). Se les
atribuye coste usando el VWAP de cada lado (no un FIFO de fills
individuales) porque el líder no ata una compra UP a una compra DOWN
específica -- son dos posiciones independientes que se van promediando.
`matched_cost = matched_shares * (vwap_up + vwap_down)`.
`guaranteed_profit = matched_shares * 1 - matched_cost` -- válido de
inmediato, sin esperar a que resuelva el mercado, porque exactamente un
lado paga $1/share y las shares emparejadas están en ambos lados en igual
cantidad, así que el payout de esa porción es matched_shares*$1 pase lo
que pase.

Una posición con `surplus_shares > 0` NUNCA se describe como "arbitraje
garantizado" completo -- solo la porción `matched` lo es; el resto
(`directional_exposure_shares`) es apuesta direccional pura, con P&L que
sí depende de la resolución.
"""
import time

import db


def _recompute_market(conn, condition_id):
    up_shares = down_shares = up_cost = down_cost = 0.0
    first_trade_side = None

    trades_full = conn.execute(
        "SELECT id, outcome, price, shares, source_timestamp_utc FROM leader_trades "
        "WHERE condition_id=? ORDER BY source_timestamp_utc, id",
        (condition_id,),
    ).fetchall()

    already = {r["after_trade_id"] for r in conn.execute(
        "SELECT after_trade_id FROM leader_inventory_timeline WHERE condition_id=?", (condition_id,)
    ).fetchall()}

    first_ts = None
    first_opposite_ts = {"Up": None, "Down": None}  # first ts a trade of this outcome happened

    for t in trades_full:
        outcome, price, shares = t["outcome"], t["price"], t["shares"]

        # Un trade incompleto (price/shares nulos -- puede pasar con filas de
        # BACKFILL cuyo origen no guardaba el tamaño) NO debe tumbar el
        # recálculo de todo el mercado: sin este guard, una sola fila mala
        # dejaba `leader_inventory_timeline` vacía para siempre en ese
        # mercado, porque el recálculo entero crasheaba en cada pasada.
        if price is None or shares is None or outcome not in ("Up", "Down"):
            db.log_event("inventory", "skipped_incomplete_trade",
                         {"trade_id": t["id"], "condition_id": condition_id,
                          "has_price": price is not None, "has_shares": shares is not None,
                          "outcome": outcome})
            continue

        if first_ts is None:
            first_ts = t["source_timestamp_utc"]
            first_trade_side = outcome
        if first_opposite_ts[outcome] is None:
            first_opposite_ts[outcome] = t["source_timestamp_utc"]

        if outcome == "Up":
            up_shares += shares
            up_cost += price * shares
        else:
            down_shares += shares
            down_cost += price * shares

        if t["id"] in already:
            continue  # already computed for this trade; state above still needs updating for later trades

        vwap_up = (up_cost / up_shares) if up_shares else None
        vwap_down = (down_cost / down_shares) if down_shares else None
        matched = min(up_shares, down_shares)
        if up_shares > down_shares:
            surplus_side, surplus = "Up", up_shares - down_shares
        elif down_shares > up_shares:
            surplus_side, surplus = "Down", down_shares - up_shares
        else:
            surplus_side, surplus = None, 0.0
        max_side = max(up_shares, down_shares)
        coverage_ratio = (matched / max_side) if max_side else None
        matched_cost = matched * ((vwap_up or 0) + (vwap_down or 0))
        guaranteed_profit = matched * 1.0 - matched_cost

        other_side = "Down" if first_trade_side == "Up" else "Up"
        opposite_first_ts = first_opposite_ts.get(other_side)
        time_to_hedge = (opposite_first_ts - first_ts) if (opposite_first_ts and first_ts) else None

        conn.execute(
            """INSERT OR IGNORE INTO leader_inventory_timeline
               (condition_id, after_trade_id, computed_at, up_shares, down_shares, up_cost, down_cost,
                vwap_up, vwap_down, matched_shares, surplus_side, surplus_shares, coverage_ratio,
                matched_cost, guaranteed_profit, directional_exposure_shares, time_to_hedge_second_leg_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (condition_id, t["id"], time.time(), up_shares, down_shares, up_cost, down_cost,
             vwap_up, vwap_down, matched, surplus_side, surplus, coverage_ratio,
             matched_cost, guaranteed_profit, surplus, time_to_hedge),
        )


def recompute_pending():
    """Finds every condition_id with leader_trades missing an inventory row
    and recomputes that market's whole timeline (cheap at this volume)."""
    with db.connect() as conn:
        pending = conn.execute(
            """SELECT DISTINCT lt.condition_id FROM leader_trades lt
               LEFT JOIN leader_inventory_timeline lit ON lit.after_trade_id = lt.id
               WHERE lit.id IS NULL AND lt.condition_id IS NOT NULL"""
        ).fetchall()
        for row in pending:
            _recompute_market(conn, row["condition_id"])


def run():
    db.log_event("inventory", "start")
    while True:
        try:
            recompute_pending()
        except Exception as e:
            db.log_event("inventory", "error", {"error": str(e)})
        time.sleep(5)


if __name__ == "__main__":
    db.init_db()
    run()
