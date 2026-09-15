"""
Excel de entrega de Strategy V2 (gestion dinamica de inventario, 3 variantes
de 1ra pierna + motor compartido de completar pares). Todo estimated gross
P&L. No copy-trading: nunca mira al lider ni sus trades.

Uso: python3 export_strategy_v2_excel.py --db-path /tmp/rebuild_test.db [salida.xlsx]
"""
import argparse
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

import backtest_v2_inventory as v2


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


def _detail_rows(results, variant_label):
    out = []
    for r in results:
        row = {"variant": variant_label, "open_time_utc_human": fmt(r.get("open_time_utc"))}
        for h in DETAIL_HEADERS:
            if h not in ("variant", "open_time_utc_human"):
                row[h] = r.get(h)
        out.append(row)
    return out


def build(db_path, out_path):
    result = v2.run(db_path)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ---------- Overview ----------
    ws = wb.create_sheet("Overview")
    ws["A1"] = ("Strategy V2 -- gestion dinamica de inventario UP/DOWN, autonoma. NUNCA mira al lider ni usa "
                "sus operaciones como señal. Todo ESTIMATED GROSS P&L: sin fees, sin rebates, sin redencion "
                "real, sin riesgo de ejecucion mas alla del best_ask/profundidad observados.")
    ws["A2"] = ("ADVERTENCIA DE MUESTRA: el order book del collector solo tiene cobertura real desde "
                "~10:43 UTC. El periodo TRAIN nominal (02:20-12:05 UTC) por eso solo aporta 31 mercados "
                "utilizables para calibrar threshold_pair (de 352 nominales) -- la calibracion es "
                "estadisticamente fragil. threshold_open de V2-C se calibro con un unico candidato viable "
                "(n>=15), no por preferencia sobre alternativas reales.")
    overview = [
        ("generado_utc", fmt(time.time())),
        ("db_usada", db_path),
        ("universo_total_mercados", len(result["universe"])),
        ("train_n_mercados_nominal", len(result["train"])),
        ("test_n_mercados_nominal", len(result["test"])),
        ("threshold_pair_congelado", result["threshold_pair"]),
        ("threshold_open_V2C_congelado", result["threshold_open"]),
        ("decision_offset_s", v2.DECISION_OFFSET_S),
        ("stake_usd", v2.STAKE_USD),
        ("check_interval_s", v2.CHECK_INTERVAL_S),
        ("close_buffer_s", v2.CLOSE_BUFFER_S),
    ]
    ws.append(["campo", "valor"])
    for k, val in overview:
        ws.append([k, val])
    ws.freeze_panes = "A2"

    # ---------- Calibration ----------
    ws = wb.create_sheet("Calibration")
    ws["A1"] = "Barrido de umbrales SOLO sobre TRAIN -- usado para elegir y congelar, nunca para reportar performance final."
    ws["A2"] = "threshold_pair (compartido por V2-A/B/C), calibrado con el selector de V2-A:"
    pair_rows = [{"threshold_pair": t, **{k: v for k, v in agg.items() if k != "strategy"}}
                 for t, agg in result["sweep_pair"]]
    next_row = _write_table(ws, ["threshold_pair", "n_trades", "n_pairs_completed", "total_pnl_usd", "roi_pct"],
                             pair_rows, start_row=4)
    ws.cell(row=next_row + 1, column=1, value="threshold_open de V2-C (con threshold_pair ya congelado):")
    open_rows = [{"threshold_open": t, **{k: v for k, v in agg.items() if k != "strategy"}}
                 for t, agg in result["sweep_open"]]
    _write_table(ws, ["threshold_open", "n_trades", "n_pairs_completed", "total_pnl_usd", "roi_pct"],
                 open_rows, start_row=next_row + 3)
    ws.freeze_panes = "A5"

    # ---------- Results Summary (V2-A/B/C + baselines) ----------
    ws = wb.create_sheet("Results Summary")
    ws["A1"] = "Evaluacion UNICA sobre TEST -- umbrales ya congelados desde TRAIN, nunca retocados."
    all_aggs = [result["agg_a"], result["agg_b"], result["agg_c"],
                result["bl_favorite"], result["bl_underdog"], result["bl_momentum"], result["bl_noop"]]
    summary_rows = []
    for agg in all_aggs:
        exp = agg.get("exposure_by_asset", {})
        row = {k: v for k, v in agg.items() if k != "exposure_by_asset"}
        for asset in ("BTC", "ETH", "SOL"):
            a = exp.get(asset, {"n": 0, "capital": 0.0, "pnl": 0.0})
            row[f"{asset}_n"] = a["n"]; row[f"{asset}_capital"] = a["capital"]; row[f"{asset}_pnl"] = a["pnl"]
        summary_rows.append(row)
    headers = ["strategy", "n_trades", "n_pairs_completed", "pct_pairs_completed", "n_never_completed",
               "mean_seconds_between_legs", "median_seconds_between_legs", "mean_combined_cost",
               "median_combined_cost", "mean_assured_margin", "pair_pnl_usd", "residual_pnl_usd",
               "total_pnl_usd", "total_capital_usd", "roi_pct", "win_rate_pct", "max_drawdown_usd",
               "max_capital_single_market_usd", "BTC_n", "BTC_capital", "BTC_pnl",
               "ETH_n", "ETH_capital", "ETH_pnl", "SOL_n", "SOL_capital", "SOL_pnl"]
    _write_table(ws, headers, summary_rows, start_row=3)
    ws.column_dimensions["A"].width = 28
    ws.freeze_panes = "A4"

    # ---------- Per-market detail ----------
    ws = wb.create_sheet("Market Detail")
    ws["A1"] = "Detalle por mercado, las 3 variantes V2 (incluye NO_SIGNAL / LEG1_SKIPPED_NO_LIQUIDITY / WINDOW_TOO_SHORT)."
    detail = (_detail_rows(result["results_a"], "V2-A") + _detail_rows(result["results_b"], "V2-B") +
              _detail_rows(result["results_c"], "V2-C"))
    _write_table(ws, DETAIL_HEADERS, detail, start_row=3)
    ws.freeze_panes = "A4"

    # ---------- Limitations ----------
    ws = wb.create_sheet("Limitations")
    limitations = [
        "Un solo dia de datos. Todo lo de este archivo es exploratorio, no una conclusion estadistica robusta.",
        "TRAIN nominal es 352 mercados pero el order book solo cubre desde ~10:43 UTC -- solo 31 mercados "
        "TRAIN tuvieron datos utilizables para calibrar threshold_pair. La calibracion es fragil.",
        "threshold_open de V2-C tuvo UN SOLO candidato con muestra suficiente (n>=15) en TRAIN -- se eligio "
        "por necesidad, no por comparacion real entre alternativas. En TEST, V2-C termino operando casi los "
        "mismos mercados que V2-A (153 vs 155) -- el filtro de 'oportunidad' resulto poco restrictivo.",
        "V2-A y V2-C (ambos entran por lado barato) pierden dinero en TEST, pero MENOS que la version pura "
        "sin intentar completar el par (baseline_underdog_puro): completar el par redujo la perdida de "
        "ROI -23.61% a aprox -3.6%, a costa de ~4x mas capital desplegado.",
        "V2-B (momentum) empeora respecto de su version pura sin completar pares: momentum solo "
        "(baseline_momentum_V1_puro) da +7.99% ROI; agregar el motor de completar pares lo baja a -0.34%. "
        "El motor de pares parece diluir un edge direccional real cuando ya existe, no solo limitar perdidas.",
        "NINGUNA de las 3 variantes V2 (con pares) supera a los baselines de una sola pierna en este TEST.",
        "Umbral de completar el par (threshold_pair) elegido maximizando P&L en dolares en TRAIN, no ROI -- "
        "otra metrica de seleccion daria un umbral distinto.",
        "Capital NO es directamente comparable entre variantes: V2-A/C despliegan ~$6.2-6.3k, V2-B solo "
        "~$1.65k (abre en muchos menos mercados). Comparar ROI, no solo P&L en dolares.",
        "Solo un intento de completar el par por mercado (sin reintentos tras un PARTIAL) -- decision de "
        "diseno documentada, no la unica posible.",
        "Chequeo de oportunidad de completar cada 5s, no tick-a-tick -- puede perderse una ventana de "
        "oportunidad mas corta que eso.",
        "No incluye fees, rebates, ni redencion real on-chain. No modela riesgo de que el book se mueva "
        "entre la decision del backtest y una orden real.",
        "121 trades del hueco de order book y toda la logica de startup/backfill quedan fuera por "
        "construccion (el universo es 100% mercados, no trades del lider).",
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
    p.add_argument("out_xlsx", nargs="?", default=str(Path(__file__).resolve().parent / "export" / "strategy_v2.xlsx"))
    args = p.parse_args()
    build(args.db_path, args.out_xlsx)
