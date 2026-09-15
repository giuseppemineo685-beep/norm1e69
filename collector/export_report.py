"""
Exporta las 7 secciones a CSV (siempre) y, si `openpyxl` está instalado, a
un único .xlsx con una pestaña por sección. Nunca se escribe durante la
captura -- se corre a mano cuando el usuario quiere revisar datos.

Uso:
    python3 export_report.py [out_dir] [--xlsx]
"""
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import db
from data_quality import compute_report

ET = ZoneInfo("America/New_York")

SECTIONS = {
    "Leader Trades": """
        SELECT id, leader_wallet, trade_id, transaction_hash, source_timestamp_utc,
               received_at_utc, detection_latency_ms, condition_id, market_slug, market_title,
               asset_symbol, token_id, outcome, side, price, shares, usdc_amount,
               seconds_since_market_open, seconds_to_market_close, maker_taker, collection_method
        FROM leader_trades ORDER BY source_timestamp_utc""",
    "Market Context": """
        SELECT tc.leader_trade_id, lt.market_title, lt.outcome as leader_side, tc.offset_seconds,
               tc.usable_for_backtest, tc.context_available, tc.snapshot_age_s,
               tc.best_bid_up, tc.best_ask_up, tc.depth_bid_up, tc.depth_ask_up,
               tc.best_bid_down, tc.best_ask_down, tc.depth_bid_down, tc.depth_ask_down,
               tc.spread_up, tc.spread_down, tc.underlying_price, tc.underlying_distance_from_open_pct,
               tc.executable_price_for_leader_size, tc.opposite_leg_price, tc.combined_cost_to_pair,
               tc.leader_up_shares_snapshot, tc.leader_down_shares_snapshot
        FROM trade_context tc JOIN leader_trades lt ON lt.id = tc.leader_trade_id
        ORDER BY tc.leader_trade_id, tc.offset_seconds""",
    "Order Book": """
        SELECT source_timestamp_utc, received_at_utc_ms, condition_id, token_id, outcome,
               seconds_since_open, seconds_to_close, best_bid, best_bid_size, best_ask,
               best_ask_size, mid_price, spread, last_trade_price, source
        FROM orderbook_snapshots ORDER BY received_at_utc_ms""",
    "Underlying Prices": """
        SELECT source_timestamp_utc, received_at_utc, asset_symbol, source, price,
               market_open_reference_price, distance_from_open_abs, distance_from_open_pct,
               seconds_to_close, condition_id
        FROM underlying_prices ORDER BY received_at_utc""",
    "Inventory Timeline": """
        SELECT condition_id, after_trade_id, computed_at, up_shares, down_shares, up_cost, down_cost,
               vwap_up, vwap_down, matched_shares, surplus_side, surplus_shares, coverage_ratio,
               matched_cost, guaranteed_profit, directional_exposure_shares, time_to_hedge_second_leg_s
        FROM leader_inventory_timeline ORDER BY condition_id, after_trade_id""",
    "Market Summary": """
        SELECT condition_id, market_slug, market_title, asset_symbol, open_time_utc, close_time_utc,
               resolution_time_utc, winner, resolution_source, open_reference_price
        FROM markets ORDER BY open_time_utc""",
}


def _utc_et(ts):
    if ts is None:
        return "", ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC"), dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")


_TS_COLUMNS = {
    "source_timestamp_utc", "received_at_utc", "open_time_utc", "close_time_utc",
    "resolution_time_utc", "computed_at",
}


def _rows_for(name, sql):
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    out = []
    for r in rows:
        extra = {}
        for col in list(r.keys()):
            if col in _TS_COLUMNS and r[col]:
                u, e = _utc_et(r[col])
                extra[col + "_utc_str"] = u
                extra[col + "_et_str"] = e
        r.update(extra)
        out.append(r)
    if name == "Order Book":
        for r in out:
            if r.get("received_at_utc_ms"):
                u, e = _utc_et(r["received_at_utc_ms"] / 1000.0)
                r["received_at_ms_utc_str"] = u
                r["received_at_ms_et_str"] = e
    return out


def _flatten_quality(report):
    flat = []
    for group, values in report.items():
        if isinstance(values, dict):
            for k, v in values.items():
                flat.append({"section": group, "metric": k, "value": v})
        elif isinstance(values, list):
            for row in values:
                flat.append({"section": group, **row})
        else:
            flat.append({"section": group, "metric": group, "value": values})
    return flat


def write_csv(rows, path):
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def export(out_dir: Path, want_xlsx: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {}
    for name, sql in SECTIONS.items():
        data[name] = _rows_for(name, sql)
    data["Data Quality"] = _flatten_quality(compute_report())

    for name, rows in data.items():
        fname = name.lower().replace(" ", "_") + ".csv"
        write_csv(rows, out_dir / fname)
        print(f"wrote {out_dir / fname} ({len(rows)} rows)")

    if want_xlsx:
        try:
            import openpyxl
        except ImportError:
            print("openpyxl not installed -- skipping .xlsx (CSVs above are still complete)")
            return
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in data.items():
            ws = wb.create_sheet(title=name[:31])
            if rows:
                headers = list(rows[0].keys())
                ws.append(headers)
                ws.freeze_panes = "A2"
                for r in rows:
                    ws.append([r.get(h) for h in headers])
        xlsx_path = out_dir / "trade_strategy_analysis.xlsx"
        wb.save(xlsx_path)
        print(f"wrote {xlsx_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    want_xlsx = "--xlsx" in args
    args = [a for a in args if a != "--xlsx"]
    out_dir = Path(args[0]) if args else Path(__file__).resolve().parent / "export"
    export(out_dir, want_xlsx)
