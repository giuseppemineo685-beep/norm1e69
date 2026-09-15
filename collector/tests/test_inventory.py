"""
Regresion contra datos reales ya analizados a mano en esta investigacion:
ventana BTC de norm1e69, Sept 14 2026 4:45-4:50PM ET, 19 fills.
up_shares=567.67 (coste $139.71), down_shares=432.78 (coste $227.67) --
verificado por conteo manual antes de escribir este collector. Este test
NO verifica el P&L realizado al resolver (eso no esta en el alcance de
este build); verifica que inventory.py reproduce la aritmetica de
inventario/cobertura/beneficio-de-la-porcion-emparejada tal como la define
su propio docstring.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# (side, price, shares) -- orden real de la ventana
FILLS = [
    ("Up", 0.29, 100.00), ("Down", 0.48, 47.00), ("Down", 0.34, 100.00), ("Up", 0.45, 100.00),
    ("Up", 0.40, 73.06), ("Up", 0.37, 11.33), ("Down", 0.48, 10.00), ("Down", 0.53, 21.00),
    ("Down", 0.56, 100.00), ("Up", 0.32, 10.00), ("Up", 0.34, 15.00), ("Down", 0.59, 100.00),
    ("Up", 0.40, 0.76), ("Up", 0.38, 11.00), ("Down", 0.61, 15.00), ("Down", 0.78, 39.78),
    ("Up", 0.14, 46.52), ("Up", 0.06, 100.00), ("Up", 0.07, 100.00),
]
CONDITION_ID = "0xtestwindow"


def _fresh_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp.name)
    db.reset_connection()
    db.init_db()
    return db


def _seed(db):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO markets (condition_id, market_slug, market_title, asset_symbol, discovered_at) "
            "VALUES (?, 'test-slug', 'test market', 'BTC', 0)", (CONDITION_ID,)
        )
        ts = 1000.0
        for side, price, shares in FILLS:
            conn.execute(
                """INSERT INTO leader_trades
                   (leader_wallet, transaction_hash, source_timestamp_utc, received_at_utc,
                    condition_id, outcome, side, price, shares, usdc_amount, raw_payload,
                    collection_method)
                   VALUES ('0xleader', ?, ?, ?, ?, ?, 'BUY', ?, ?, ?, '{}', 'BACKFILL')""",
                (f"0xhash{ts}", ts, ts, CONDITION_ID, side, price, shares, price * shares),
            )
            ts += 1


def test_inventory_matches_hand_computed_real_window(monkeypatch):
    db = _fresh_db(monkeypatch)
    _seed(db)

    import inventory
    inventory.recompute_pending()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM leader_inventory_timeline WHERE condition_id=? "
            "ORDER BY after_trade_id DESC LIMIT 1", (CONDITION_ID,)
        ).fetchone()

    assert row is not None
    assert row["up_shares"] == pytest_approx(567.67)
    assert row["down_shares"] == pytest_approx(432.78)
    assert row["up_cost"] == pytest_approx(139.7129, abs=0.01)
    assert row["down_cost"] == pytest_approx(227.6684, abs=0.01)
    assert row["matched_shares"] == pytest_approx(432.78)
    assert row["surplus_side"] == "Up"
    assert row["surplus_shares"] == pytest_approx(134.89, abs=0.01)
    assert row["coverage_ratio"] == pytest_approx(0.76243, abs=0.001)
    # guaranteed_profit = matched*1 - matched*(vwap_up+vwap_down), see inventory.py docstring
    assert row["guaranteed_profit"] == pytest_approx(98.6, abs=0.5)


def pytest_approx(expected, abs=1e-2):
    import pytest
    return pytest.approx(expected, abs=abs)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
