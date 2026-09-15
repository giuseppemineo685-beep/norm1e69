"""
Clasificacion A-F de inventory_pair_analysis.py: debe usar SOLO el estado
'antes' de cada trade (y su propio combined_cost_to_pair), nunca el
resultado del mercado ni trades posteriores.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FIELDS = ["leader_trade_id", "condition_id", "market_title", "asset_symbol",
          "source_timestamp_utc", "trade_outcome", "side", "price", "shares",
          "usdc_amount", "seconds_to_market_close", "underlying_distance_from_open_pct",
          "combined_cost_to_pair", "market_winner"]


def _row(tid, cid, ts, outcome, price, shares, combined_cost, winner="Up"):
    return {
        "leader_trade_id": tid, "condition_id": cid, "market_title": "test",
        "asset_symbol": "BTC", "source_timestamp_utc": ts, "trade_outcome": outcome,
        "side": "BUY", "price": price, "shares": shares, "usdc_amount": price * shares,
        "seconds_to_market_close": 200, "underlying_distance_from_open_pct": 0.05,
        "combined_cost_to_pair": combined_cost, "market_winner": winner,
    }


def _write_csv(rows):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    w = csv.DictWriter(tmp, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    tmp.close()
    return tmp.name


def test_first_trade_is_first_leg():
    import inventory_pair_analysis as ipa
    rows = [_row(1, "0xc1", 1000, "Up", 0.4, 10, None)]
    path = _write_csv(rows)
    by_market = ipa.load_trades(path)
    out = ipa.classify_and_reconstruct(by_market)
    assert out[0]["function_tag"] == "A_FIRST_LEG"


def test_closing_gap_under_dollar_is_imbalance_reducing_with_cheap_bucket():
    import inventory_pair_analysis as ipa
    rows = [
        _row(1, "0xc2", 1000, "Up", 0.4, 10, None),
        _row(2, "0xc2", 1010, "Down", 0.55, 10, 0.95),  # combined_cost_to_pair < 1
    ]
    path = _write_csv(rows)
    out = ipa.classify_and_reconstruct(ipa.load_trades(path))
    tags = {r["leader_trade_id"]: r for r in out}
    assert tags["1"]["function_tag"] == "A_FIRST_LEG"
    assert tags["2"]["function_tag"] == "IMBALANCE_REDUCING_BUY"
    assert tags["2"]["cost_bucket_at_imbalance_reducing_buy"] == "<0.99"


def test_closing_gap_over_dollar_is_imbalance_reducing_with_expensive_bucket():
    import inventory_pair_analysis as ipa
    rows = [
        _row(1, "0xc3", 1000, "Up", 0.6, 10, None),
        _row(2, "0xc3", 1010, "Down", 0.55, 10, 1.05),  # combined >= 1.05
    ]
    path = _write_csv(rows)
    out = ipa.classify_and_reconstruct(ipa.load_trades(path))
    tags = {r["leader_trade_id"]: r for r in out}
    assert tags["2"]["function_tag"] == "IMBALANCE_REDUCING_BUY"
    assert tags["2"]["cost_bucket_at_imbalance_reducing_buy"] == ">1.05"


def test_adding_to_already_larger_side_with_low_coverage_is_directional():
    import inventory_pair_analysis as ipa
    rows = [
        _row(1, "0xc4", 1000, "Up", 0.4, 50, None),   # up=50, down=0
        _row(2, "0xc4", 1010, "Up", 0.42, 50, None),   # sigue comprando UP -> ensancha el hueco (down sigue en 0)
    ]
    path = _write_csv(rows)
    out = ipa.classify_and_reconstruct(ipa.load_trades(path))
    tags = {r["leader_trade_id"]: r["function_tag"] for r in out}
    assert tags["2"] == "D_DIRECTIONAL_SURPLUS"


def test_classification_never_uses_market_winner():
    """Mismo escenario, dos 'winners' distintos -- la clasificacion debe ser
    identica en ambos casos (no debe depender del resultado)."""
    import inventory_pair_analysis as ipa
    for winner in ("Up", "Down"):
        rows = [
            _row(1, "0xc5", 1000, "Up", 0.4, 10, None, winner=winner),
            _row(2, "0xc5", 1010, "Down", 0.55, 10, 0.95, winner=winner),
        ]
        path = _write_csv(rows)
        out = ipa.classify_and_reconstruct(ipa.load_trades(path))
        tags = [r["function_tag"] for r in out]
        assert tags == ["A_FIRST_LEG", "IMBALANCE_REDUCING_BUY"], (winner, tags)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
