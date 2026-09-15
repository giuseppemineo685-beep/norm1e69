"""
Excel de la validacion forward en paper trading. Lee SOLO
collector/paper_validation.db (nunca data.db). Debe poder generarse aunque
todavia no haya ningun mercado resuelto -- las hojas quedan con encabezado
y cero filas, nunca fallan por falta de datos.

Uso: python3 export_paper_validation.py [salida.xlsx]
"""
import hashlib
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

import paper_validation_db as pdb


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _write_table(ws, headers, rows, start_row=1):
    for j, h in enumerate(headers, start=1):
        ws.cell(row=start_row, column=j, value=h)
    for i, row in enumerate(rows, start=start_row + 1):
        for j, h in enumerate(headers, start=1):
            ws.cell(row=i, column=j, value=row.get(h))
    return start_row + len(rows) + 1


def _drawdown(pnls_ordered):
    cum = peak = max_dd = 0.0
    for p in pnls_ordered:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def build(out_path):
    pdb.init_db()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    with pdb.connect() as conn:
        # ---------- Overview ----------
        ws = wb.create_sheet("Overview")
        ws["A1"] = ("Validacion forward en PAPER TRADING -- SOLO simulacion, datos completamente "
                    "nuevos (mercados posteriores al arranque del validador). No copy-trading, no "
                    "ejecucion real. Estimated gross P&L. Puede tener cero mercados resueltos todavia.")
        cutoff = pdb.get_meta("validator_started_at")
        n_markets = conn.execute("SELECT count(*) c FROM paper_markets").fetchone()["c"]
        n_decisions = conn.execute("SELECT count(*) c FROM paper_decisions").fetchone()["c"]
        n_hedge_fills = conn.execute("SELECT count(*) c FROM paper_hedge_fills").fetchone()["c"]
        n_resolutions = conn.execute("SELECT count(*) c FROM paper_resolutions").fetchone()["c"]
        overview = [
            ("generado_utc", fmt(time.time())),
            ("validador_arrancado_utc", fmt(float(cutoff)) if cutoff else None),
            ("mercados_descubiertos", n_markets),
            ("decisiones_tomadas", n_decisions),
            ("coberturas_ejecutadas", n_hedge_fills),
            ("resoluciones_registradas", n_resolutions),
        ]
        ws.append(["campo", "valor"])
        for k, v in overview:
            ws.append([k, v])
        ws.freeze_panes = "A2"

        # ---------- Signals (decisions) ----------
        ws = wb.create_sheet("Signals")
        rows = _rows(conn, """
            SELECT pd.strategy, pm.market_title, pd.asset_symbol, pd.condition_id, pd.token_id,
                   pd.decision_timestamp_utc, pd.underlying_open_price, pd.underlying_price_at_60s,
                   pd.distance_pct, pd.signal_present, pd.side_chosen, pd.snapshot_id, pd.best_ask,
                   pd.executable_price, pd.shares, pd.capital_usd, pd.fill_status, pd.skip_or_partial_reason
            FROM paper_decisions pd JOIN paper_markets pm ON pm.condition_id = pd.condition_id
            ORDER BY pd.decision_timestamp_utc
        """)
        for r in rows:
            r["decision_time_human"] = fmt(r["decision_timestamp_utc"])
        headers = ["strategy", "market_title", "asset_symbol", "condition_id", "token_id",
                   "decision_timestamp_utc", "decision_time_human", "underlying_open_price",
                   "underlying_price_at_60s", "distance_pct", "signal_present", "side_chosen",
                   "snapshot_id", "best_ask", "executable_price", "shares", "capital_usd",
                   "fill_status", "skip_or_partial_reason"]
        _write_table(ws, headers, rows, start_row=3)
        ws["A1"] = "Cada mercado x cada estrategia (las 3 evaluan exactamente el mismo universo)."
        ws.freeze_panes = "A4"

        # ---------- Hedges ----------
        ws = wb.create_sheet("Hedges")
        checks = _rows(conn, """
            SELECT hc.*, pd.condition_id AS cid2 FROM paper_hedge_checks hc
            JOIN paper_decisions pd ON pd.id = hc.decision_id
            ORDER BY hc.checked_at_utc
        """)
        for r in checks:
            r["checked_at_human"] = fmt(r["checked_at_utc"])
        headers_c = ["decision_id", "condition_id", "checked_at_human", "opposite_token_id",
                     "opposite_snapshot_id", "opposite_best_ask", "opposite_executable_price",
                     "combined_cost", "depth_sufficient", "seconds_to_close", "cond_price_le_010",
                     "cond_combined_le_090", "cond_depth_ok", "cond_time_ok", "all_conditions_met",
                     "hedge_executed"]
        next_row = _write_table(ws, headers_c, checks, start_row=3)
        ws["A1"] = "Cada chequeo de condiciones de cobertura de MOMENTUM_PARTIAL_HEDGE (se cubra o no)."

        fills = _rows(conn, """
            SELECT hf.*, pd.strategy FROM paper_hedge_fills hf
            JOIN paper_decisions pd ON pd.id = hf.decision_id ORDER BY hf.executed_at_utc
        """)
        for r in fills:
            r["executed_at_human"] = fmt(r["executed_at_utc"])
        ws.cell(row=next_row + 1, column=1, value="Coberturas EJECUTADAS:")
        headers_f = ["decision_id", "condition_id", "executed_at_human", "target_shares",
                     "filled_shares", "usd_spent", "vwap", "fill_status", "snapshot_id"]
        _write_table(ws, headers_f, fills, start_row=next_row + 3)
        ws.freeze_panes = "A4"

        # ---------- Resolutions (P&L, ROI, drawdown por estrategia) ----------
        ws = wb.create_sheet("Resolutions")
        res_rows = _rows(conn, """
            SELECT pr.*, pm.market_title, pm.asset_symbol FROM paper_resolutions pr
            JOIN paper_markets pm ON pm.condition_id = pr.condition_id
            ORDER BY pr.resolved_at_utc
        """)
        for r in res_rows:
            r["resolved_at_human"] = fmt(r["resolved_at_utc"])
        headers_r = ["strategy", "condition_id", "market_title", "asset_symbol", "winner",
                     "resolved_at_human", "capital_deployed_usd", "payout_usd", "pnl_usd", "roi"]
        _write_table(ws, headers_r, res_rows, start_row=3)
        ws["A1"] = "Resoluciones registradas (vacio hasta que cierren mercados y el collector conozca el winner)."
        ws.freeze_panes = "A4"

        # ---------- Strategy Summary ----------
        ws = wb.create_sheet("Strategy Summary")
        by_strategy = defaultdict(list)
        for r in res_rows:
            by_strategy[r["strategy"]].append(r)
        summary_rows = []
        for strategy in ("MOMENTUM_PURE", "MOMENTUM_PARTIAL_HEDGE", "POLYMARKET_FAVORITE_BASELINE"):
            rs = by_strategy.get(strategy, [])
            n = len(rs)
            capital = sum(r["capital_deployed_usd"] for r in rs)
            pnl = sum(r["pnl_usd"] for r in rs)
            wins = sum(1 for r in rs if r["pnl_usd"] > 0)
            by_asset = defaultdict(lambda: {"n": 0, "pnl": 0.0})
            for r in rs:
                a = by_asset[r["asset_symbol"]]
                a["n"] += 1; a["pnl"] += r["pnl_usd"]
            dd = _drawdown([r["pnl_usd"] for r in sorted(rs, key=lambda r: r["resolved_at_utc"])])
            row = {
                "strategy": strategy, "n_resolved": n, "capital_deployed_usd": round(capital, 2),
                "pnl_usd": round(pnl, 2), "roi_pct": round(pnl / capital * 100, 2) if capital else None,
                "win_rate_pct": round(wins / n * 100, 1) if n else None, "max_drawdown_usd": round(dd, 2),
            }
            for asset in ("BTC", "ETH", "SOL"):
                a = by_asset.get(asset, {"n": 0, "pnl": 0.0})
                row[f"{asset}_n"] = a["n"]; row[f"{asset}_pnl"] = round(a["pnl"], 2)
            summary_rows.append(row)
        headers_s = ["strategy", "n_resolved", "capital_deployed_usd", "pnl_usd", "roi_pct",
                     "win_rate_pct", "max_drawdown_usd", "BTC_n", "BTC_pnl", "ETH_n", "ETH_pnl",
                     "SOL_n", "SOL_pnl"]
        _write_table(ws, headers_s, summary_rows, start_row=3)
        ws["A1"] = "P&L/ROI/drawdown agregado por estrategia -- se llena a medida que cierran mercados."
        ws.column_dimensions["A"].width = 28
        ws.freeze_panes = "A4"

        # ---------- Methodology ----------
        ws = wb.create_sheet("Methodology")
        methodology = [
            "Datos completamente nuevos: solo mercados con open_time_utc posterior al arranque del "
            "validador (nunca los ya usados en el backtest V1/V2/V3).",
            "MOMENTUM_PURE: decision a los 60s de abierto. Opera solo si |distancia del subyacente al "
            "open| >= 0.02% (umbral congelado de V1). Compra UP si positiva, DOWN si negativa. Stake $10. "
            "Mantiene a resolucion. Nunca compra la pierna contraria.",
            "MOMENTUM_PARTIAL_HEDGE: misma entrada que MOMENTUM_PURE. Monitorea la pierna contraria y "
            "cubre EXACTAMENTE 25% de las shares iniciales, una sola vez, solo si TODAS estas condiciones "
            "se cumplen simultaneamente: (1) precio ejecutable de la pierna contraria <= $0.10; (2) coste "
            "medio de la 1ra pierna + precio ejecutable contrario <= $0.90; (3) profundidad suficiente para "
            "llenar el 25% completo (FULL, nunca PARTIAL); (4) quedan >= 60s para el cierre. Nunca vende.",
            "POLYMARKET_FAVORITE_BASELINE: decision a los 60s, compra el lado con MAYOR probabilidad "
            "implicita (precio/mid mas alto) -- nota: el enunciado original decia 'menor best ask / mayor "
            "probabilidad implicita', que son contradictorios bajo el mecanismo real de precios; se resolvio "
            "usando 'mayor probabilidad implicita', consistente con 'favorito' en toda la sesion previa "
            "(V1/V2/V3). Stake $10, mantiene a resolucion.",
            "Ejecucion: SIEMPRE recorriendo profundidad real (orderbook_levels), nunca asume fill al "
            "best_ask. FULL/PARTIAL/SKIPPED registrados explicitamente. Fail-closed: sin datos = SKIPPED, "
            "nunca se inventa un precio.",
            "Anti-lookahead: toda decision usa unicamente snapshots con received_at_utc_ms <= el instante "
            "de decision (misma regla conservadora que trade_context/V1/V2/V3). El monitoreo de cobertura "
            "usa el instante real de wall-clock en que corre el chequeo -- no puede ver el futuro porque "
            "corre en tiempo real hacia adelante.",
            "Ninguna estrategia observa ni usa las operaciones del lider como señal -- las 3 son "
            "autonomas, evaluadas sobre el mismo universo de mercados BTC/ETH/SOL de 5 minutos.",
            "SEGURIDAD: este validador nunca hace una request HTTP (0 dependencias de red), nunca importa "
            "el ejecutor real ni ningun cliente de ordenes/wallet, nunca lee credenciales, y abre "
            "collector/data.db exclusivamente en modo read-only (a nivel de driver sqlite, no solo por "
            "convencion). Escribe unicamente en collector/paper_validation.db.",
        ]
        ws.append(["nota_metodologica"])
        for m in methodology:
            ws.append([m])
        ws.column_dimensions["A"].width = 120

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()
    out_path.with_suffix(out_path.suffix + ".sha256").write_text(f"{sha256}  {out_path.name}\n")
    print(f"wrote {out_path}  sha256={sha256}")
    return out_path, sha256


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "export" / "paper_validation.xlsx"
    build(out)
