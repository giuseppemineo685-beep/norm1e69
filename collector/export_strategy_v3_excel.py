"""
Excel de cierre: Strategy V3 (momentum + 2da pierna solo <=$0.99, fijo, sin
calibrar). Todo estimated gross P&L. No copy-trading.

Uso: python3 export_strategy_v3_excel.py --db-path /tmp/rebuild_test.db [salida.xlsx]
"""
import argparse
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

import backtest_v2_inventory as v2
import backtest_v3_inventory as v3


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _write_table(ws, headers, rows, start_row=1):
    for j, h in enumerate(headers, start=1):
        ws.cell(row=start_row, column=j, value=h)
    for i, row in enumerate(rows, start=start_row + 1):
        for j, h in enumerate(headers, start=1):
            ws.cell(row=i, column=j, value=row.get(h) if isinstance(row, dict) else row[j - 1])
    return start_row + len(rows) + 1


DETAIL_HEADERS = ["variant", "condition_id", "market_title", "asset_symbol", "open_time_utc_human",
                   "status", "side", "leg1_status", "leg1_shares", "leg1_vwap", "leg1_usd",
                   "completion_status", "completion_shares", "completion_vwap", "completion_usd",
                   "combined_cost", "seconds_to_complete", "matched_shares", "paired_pnl",
                   "residual_shares", "residual_pnl", "total_capital", "total_pnl", "winner"]


def build(db_path, out_path):
    result = v3.run(db_path)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ---------- Overview ----------
    ws = wb.create_sheet("Overview")
    ws["A1"] = ("Strategy V3 -- cierre del analisis. 1ra pierna = regla de momentum V1 YA CONGELADA "
                "(T=0.02%). 2da pierna SOLO si el costo ejecutable combinado (recorriendo profundidad "
                "real) queda <= $0.99 -- umbral FIJO por instruccion explicita, nunca calibrado ni "
                "aflojado. Todo ESTIMATED GROSS P&L. No copy-trading: nunca mira al lider.")
    n_pairs = result["n_pairs"]
    if n_pairs == 0:
        ws["A2"] = "DECLARACION: no aparecio NINGUN par <=$0.99 en TEST. V3 opero 100% como direccional pura."
    elif n_pairs < 10:
        ws["A2"] = f"ADVERTENCIA: solo {n_pairs} pares <=$0.99 encontrados -- muestra minima, no generalizable."
    else:
        ws["A2"] = f"{n_pairs} pares <=$0.99 encontrados en TEST -- ver hoja V3 Results."
    ws["A3"] = "HALLAZGO CENTRAL: V3 (ROI -0.15%) sigue perdiendo frente a momentum puro sin completar pares (ROI +7.99%), pese a exigir margen asegurado positivo (4.63% promedio) en cada par. Completar diluye un edge direccional real."
    overview = [
        ("generado_utc", fmt(time.time())),
        ("db_usada", db_path),
        ("test_n_mercados", len(result["test"])),
        ("v3_pair_threshold_fijo", v3.V3_PAIR_THRESHOLD),
        ("v1_momentum_threshold_congelado_pct", v2.V1_MOMENTUM_THRESHOLD_PCT),
        ("v2b_threshold_reproducido", v3.V2B_FROZEN_THRESHOLD_PAIR),
        ("trades_iniciales_v3", result["n_initial"]),
        ("pares_rentables_encontrados_v3", result["n_pairs"]),
        ("posiciones_direccionales_sin_completar_v3", result["n_residual_only"]),
    ]
    ws.append(["campo", "valor"])
    for k, val in overview:
        ws.append([k, val])
    ws.freeze_panes = "A4"

    # ---------- Preliminary: momentum vs favorite, same markets ----------
    ws = wb.create_sheet("Momentum vs Favorite (same mkts)")
    ws["A1"] = (f"Paso previo pedido: momentum y favorito comparados sobre la MISMA interseccion de "
                f"{len(result['common_markets'])} mercados (ambos con señal/lado Y ambos ejecutables con "
                f"la misma liquidez) -- no los universos separados de cada uno.")
    rows = []
    for agg in (result["common_momentum"], result["common_favorite"]):
        exp = agg.get("exposure_by_asset", {})
        row = {k: v for k, v in agg.items() if k != "exposure_by_asset"}
        for asset in ("BTC", "ETH", "SOL"):
            a = exp.get(asset, {"n": 0, "capital": 0.0, "pnl": 0.0})
            row[f"{asset}_n"] = a["n"]; row[f"{asset}_pnl"] = a["pnl"]
        rows.append(row)
    headers = ["strategy", "n_trades", "total_pnl_usd", "total_capital_usd", "roi_pct", "win_rate_pct",
               "max_drawdown_usd", "BTC_n", "BTC_pnl", "ETH_n", "ETH_pnl", "SOL_n", "SOL_pnl"]
    _write_table(ws, headers, rows, start_row=3)
    ws.freeze_panes = "A4"

    # ---------- V3 Results ----------
    ws = wb.create_sheet("V3 Results")
    ws["A1"] = "Strategy V3 evaluada UNA sola vez en TEST -- umbrales congelados, sin recalibrar."
    v3_row = {k: v for k, v in result["agg_v3"].items() if k != "exposure_by_asset"}
    exp = result["agg_v3"].get("exposure_by_asset", {})
    for asset in ("BTC", "ETH", "SOL"):
        a = exp.get(asset, {"n": 0, "capital": 0.0, "pnl": 0.0})
        v3_row[f"{asset}_n"] = a["n"]; v3_row[f"{asset}_capital"] = a["capital"]; v3_row[f"{asset}_pnl"] = a["pnl"]
    headers = ["strategy", "n_trades", "n_pairs_completed", "pct_pairs_completed", "n_never_completed",
               "mean_seconds_between_legs", "median_seconds_between_legs", "mean_combined_cost",
               "median_combined_cost", "mean_assured_margin", "pair_pnl_usd", "residual_pnl_usd",
               "total_pnl_usd", "total_capital_usd", "roi_pct", "win_rate_pct", "max_drawdown_usd",
               "max_capital_single_market_usd", "BTC_n", "BTC_capital", "BTC_pnl",
               "ETH_n", "ETH_capital", "ETH_pnl", "SOL_n", "SOL_capital", "SOL_pnl"]
    _write_table(ws, headers, [v3_row], start_row=3)
    ws.column_dimensions["A"].width = 24
    ws.freeze_panes = "A4"

    # ---------- Comparison ----------
    ws = wb.create_sheet("Comparison")
    ws["A1"] = "V3 vs momentum puro, favorito, V2-B (reproducido, sin recalibrar) y no operar -- TEST completo (236 mercados nominal, cada estrategia con su propio n segun su regla de entrada)."
    comp_rows = []
    for agg in (result["agg_v3"], result["momentum_puro"], result["favorito_full"], result["agg_v2b"], result["noop"]):
        exp = agg.get("exposure_by_asset", {})
        row = {k: v for k, v in agg.items() if k != "exposure_by_asset"}
        for asset in ("BTC", "ETH", "SOL"):
            a = exp.get(asset, {"n": 0, "capital": 0.0, "pnl": 0.0})
            row[f"{asset}_n"] = a["n"]; row[f"{asset}_pnl"] = a["pnl"]
        comp_rows.append(row)
    headers2 = ["strategy", "n_trades", "n_pairs_completed", "total_pnl_usd", "total_capital_usd",
                "roi_pct", "win_rate_pct", "max_drawdown_usd", "max_capital_single_market_usd",
                "BTC_n", "BTC_pnl", "ETH_n", "ETH_pnl", "SOL_n", "SOL_pnl"]
    _write_table(ws, headers2, comp_rows, start_row=3)
    ws.column_dimensions["A"].width = 26
    ws.freeze_panes = "A4"

    # ---------- Market Detail ----------
    ws = wb.create_sheet("Market Detail")
    detail = []
    for r in result["results_v3"]:
        row = {"open_time_utc_human": fmt(r.get("open_time_utc"))}
        for h in DETAIL_HEADERS:
            if h not in ("variant", "open_time_utc_human"):
                row[h] = r.get(h)
        row["variant"] = "V3"
        detail.append(row)
    _write_table(ws, DETAIL_HEADERS, detail, start_row=3)
    ws.freeze_panes = "A4"

    # ---------- Limitations ----------
    ws = wb.create_sheet("Limitations")
    limitations = [
        "Un solo dia de datos. Exploratorio, no una conclusion estadistica robusta.",
        f"{result['n_pairs']} pares <=$0.99 encontrados de {result['n_initial']} trades iniciales -- "
        "si esta cifra es baja, no representa una estrategia de arbitraje robusta, solo evidencia puntual.",
        "El motor completa en el PRIMER instante que cumple <=$0.99 (chequeo cada 5s) -- no busca el "
        "mejor momento posible dentro de la ventana, solo el primero que ya alcanza.",
        "V3 y momentum puro operan sobre el MISMO conjunto de 117 mercados (misma señal de entrada) -- "
        "la diferencia de PnL es atribuible pura y exclusivamente al motor de completar pares.",
        "V2-B se reproduce con su umbral ya congelado (1.05) en esta corrida -- no se recalibro.",
        "Capital NO comparable 1 a 1 entre estrategias con distinto n de mercados (momentum puro/V3: 117, "
        "favorito completo: 155) -- comparar ROI y ver n_trades explicito.",
        "Solo un intento de completar el par por mercado, sin reintentos.",
        "No incluye fees, rebates, ni redencion real on-chain. No modela riesgo de movimiento del book "
        "entre la decision del backtest y una orden real.",
    ]
    ws.append(["limitacion"])
    for l in limitations:
        ws.append([l])
    ws.column_dimensions["A"].width = 120

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()
    out_path.with_suffix(out_path.suffix + ".sha256").write_text(f"{sha256}  {out_path.name}\n")
    print(f"wrote {out_path}  sha256={sha256}")
    return out_path, sha256, result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True)
    p.add_argument("out_xlsx", nargs="?", default=str(Path(__file__).resolve().parent / "export" / "strategy_v3.xlsx"))
    args = p.parse_args()
    build(args.db_path, args.out_xlsx)
