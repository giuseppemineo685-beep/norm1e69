"""
Informe COMPACTO de forward paper validation, ventana movil de 48 horas.

Forward paper validation, NO backtest historico: lee unicamente
collector/data.db (solo lectura, mode=ro) y collector/paper_validation.db
(una unica transaccion BEGIN DEFERRED, igual que export_paper_validation.py)
-- nunca escribe en ninguna de las dos, nunca reinicia el collector ni el
validador, nunca lee .env ni credenciales, nunca envia ordenes.

Reusa la formula de fee YA VERIFICADA (export_paper_validation.py,
TAKER_FEE_RATE_CRYPTO/estimated_taker_fee_usd) y el calculo de drawdown
(_drawdown) -- import directo, no reimplementados, para no divergir del
informe principal.

REPORT_CUTOFF_TS se fija UNA sola vez al arrancar (time.time()) y TODAS las
consultas de todas las hojas usan ese mismo instante como limite superior --
igual criterio de snapshot unico que export_paper_validation.py. La ventana
analizada es [REPORT_CUTOFF_TS - 48h, REPORT_CUTOFF_TS].

Cohorte: SOLO 'OFFICIAL' (paper_markets.validation_cohort) -- PRE_VPN_RECOVERY
queda excluido de TODAS las metricas, y el Overview reporta cuantos mercados
PRE_VPN_RECOVERY cayeron (si alguno) dentro de la ventana, para que la
exclusion sea verificable, no solo afirmada.

paper_hedge_checks (potencialmente >1M filas: un registro por cada chequeo
periodico de condiciones de cobertura, se cumplan o no) NUNCA se vuelca fila
por fila -- solo se reporta su COUNT(*) total y el COUNT(*) con
all_conditions_met=1, en la hoja Data Quality. Las coberturas REALMENTE
EJECUTADAS (paper_hedge_fills, un orden de magnitud menor: a lo sumo una
por decision) si se listan individualmente en la hoja 'Executed Hedges'.

Uso: python3 export_paper_last_48h_compact.py [salida.xlsx]
"""
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openpyxl

import paper_validation_db as pdb
from export_paper_validation import (
    TAKER_FEE_RATE_CRYPTO,
    TAKER_FEE_SOURCE,
    estimated_taker_fee_usd,
    _drawdown,
)

DATA_DB_PATH = str(Path(__file__).resolve().parent / "data.db")
WINDOW_HOURS = 48.0
STRATEGIES = ("MOMENTUM_PURE", "MOMENTUM_PARTIAL_HEDGE", "POLYMARKET_FAVORITE_BASELINE")
ASSETS = ("BTC", "ETH", "SOL")


def data_conn():
    """SOLO LECTURA -- misma URI mode=ro que run_paper_validation.py/
    export_paper_validation.py, rechazada a nivel de driver sqlite."""
    conn = sqlite3.connect(f"file:{DATA_DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


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


def _fee_for_decision(dconn_rows_hedge_by_decision, d):
    """fee = fee(entrada) + fee(cobertura si existe). Nunca toca
    paper_decisions/paper_resolutions -- calculo derivado y separado."""
    fee = estimated_taker_fee_usd(d.get("shares"), d.get("executable_price")) or 0.0
    hedge = dconn_rows_hedge_by_decision.get(d["id"])
    if hedge:
        fee += estimated_taker_fee_usd(hedge.get("filled_shares"), hedge.get("vwap")) or 0.0
    return fee


def build(out_path):
    pdb.init_db()
    REPORT_CUTOFF_TS = time.time()
    WINDOW_START_TS = REPORT_CUTOFF_TS - WINDOW_HOURS * 3600.0

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    with pdb.connect() as conn:
        conn.execute("BEGIN DEFERRED")  # una sola foto consistente de paper_validation.db para todo el informe

        # ---------------------------------------------------------- universo base ---
        # "Evaluado en la ventana" = decision_time_utc (el instante en que la
        # estrategia decide) cae dentro de [WINDOW_START_TS, REPORT_CUTOFF_TS].
        # No open_time_utc -- un mercado que abrio justo antes de la ventana
        # pero decide dentro de ella SI cuenta; uno que decide antes de la
        # ventana NO, aunque siga abierto.
        markets = _rows(conn, """
            SELECT * FROM paper_markets
            WHERE decision_time_utc BETWEEN ? AND ?
              AND discovered_at <= ?
        """, WINDOW_START_TS, REPORT_CUTOFF_TS, REPORT_CUTOFF_TS)
        markets_by_cid = {m["condition_id"]: m for m in markets}
        official_cids = {cid for cid, m in markets_by_cid.items() if m["validation_cohort"] == "OFFICIAL"}
        n_pre_vpn_in_window = sum(1 for m in markets_by_cid.values() if m["validation_cohort"] != "OFFICIAL")

        cids_placeholder = ",".join("?" * len(official_cids)) if official_cids else "''"

        decisions = []
        if official_cids:
            decisions = _rows(conn, f"""
                SELECT * FROM paper_decisions
                WHERE condition_id IN ({cids_placeholder}) AND created_at <= ?
            """, *official_cids, REPORT_CUTOFF_TS)
        decisions_by_strategy = defaultdict(list)
        decisions_by_market = defaultdict(list)
        for d in decisions:
            decisions_by_strategy[d["strategy"]].append(d)
            decisions_by_market[d["condition_id"]].append(d)

        resolutions = []
        if official_cids:
            resolutions = _rows(conn, f"""
                SELECT * FROM paper_resolutions
                WHERE condition_id IN ({cids_placeholder}) AND resolved_at_utc <= ?
            """, *official_cids, REPORT_CUTOFF_TS)
        res_by_strategy = defaultdict(list)
        for r in resolutions:
            res_by_strategy[r["strategy"]].append(r)

        decision_ids = [d["id"] for d in decisions]
        hedge_fills = []
        if decision_ids:
            ph = ",".join("?" * len(decision_ids))
            hedge_fills = _rows(conn, f"""
                SELECT * FROM paper_hedge_fills WHERE decision_id IN ({ph}) AND executed_at_utc <= ?
            """, *decision_ids, REPORT_CUTOFF_TS)
        hedge_by_decision = {h["decision_id"]: h for h in hedge_fills}

        # paper_hedge_checks: SOLO COUNT -- nunca se leen filas individuales.
        n_hedge_checks_total = n_hedge_checks_met = 0
        if decision_ids:
            ph = ",".join("?" * len(decision_ids))
            row = conn.execute(f"""
                SELECT count(*) n, COALESCE(sum(all_conditions_met),0) n_met
                FROM paper_hedge_checks WHERE decision_id IN ({ph}) AND checked_at_utc <= ?
            """, (*decision_ids, REPORT_CUTOFF_TS)).fetchone()
            n_hedge_checks_total, n_hedge_checks_met = row["n"], row["n_met"]

        decisions_by_id = {d["id"]: d for d in decisions}

        # ---------------------------------------------------------- clasificacion de mercado ---
        def _classify_market(cid):
            decs = decisions_by_market.get(cid, [])
            if len(decs) < len(STRATEGIES):
                return "PENDIENTE"
            if any(d["fill_status"] in ("FULL", "PARTIAL") for d in decs):
                return "RESUELTO_O_EJECUTADO"
            return "OMITIDO"

        n_pending = sum(1 for cid in official_cids if _classify_market(cid) == "PENDIENTE")
        n_executed_mkt = sum(1 for cid in official_cids if _classify_market(cid) == "RESUELTO_O_EJECUTADO")
        n_omitted_mkt = sum(1 for cid in official_cids if _classify_market(cid) == "OMITIDO")

        fill_status_counts = defaultdict(int)
        for d in decisions:
            fill_status_counts[d["fill_status"]] += 1

        # ---------------------------------------------------------- Data Quality: huecos ---
        # collector_events (data.db): fuente estructurada de gaps/reconnects/errores,
        # ya poblada por el collector -- se usa tal cual, no se re-deriva a mano.
        with data_conn() as dconn:
            gap_events = _rows(dconn, """
                SELECT ts, component, event_type, detail FROM collector_events
                WHERE ts BETWEEN ? AND ? AND event_type IN ('gap','reconnect','error')
                ORDER BY ts
            """, WINDOW_START_TS, REPORT_CUTOFF_TS)
            ob_coverage = dconn.execute("""
                SELECT min(received_at_utc_ms)/1000.0 a, max(received_at_utc_ms)/1000.0 b
                FROM orderbook_snapshots WHERE received_at_utc_ms/1000.0 BETWEEN ? AND ?
            """, (WINDOW_START_TS, REPORT_CUTOFF_TS)).fetchone()

        # gap de ingesta por delta entre snapshots consecutivos (deteccion generica,
        # independiente de collector_events, por si un hueco no genero evento explicito)
        with data_conn() as dconn:
            snap_ts = [r[0] / 1000.0 for r in dconn.execute("""
                SELECT received_at_utc_ms FROM orderbook_snapshots
                WHERE received_at_utc_ms/1000.0 BETWEEN ? AND ?
                ORDER BY received_at_utc_ms
            """, (WINDOW_START_TS, REPORT_CUTOFF_TS)).fetchall()]
        GAP_THRESHOLD_S = 30.0
        detected_gaps = []
        for i in range(1, len(snap_ts)):
            delta = snap_ts[i] - snap_ts[i - 1]
            if delta > GAP_THRESHOLD_S:
                detected_gaps.append({"gap_start_utc": fmt(snap_ts[i - 1]), "gap_end_utc": fmt(snap_ts[i]),
                                       "duration_s": round(delta, 1)})
        total_gap_hours = sum(g["duration_s"] for g in detected_gaps) / 3600.0
        effective_hours = WINDOW_HOURS - total_gap_hours
        pct_coverage = round(effective_hours / WINDOW_HOURS * 100, 2)

        n_gap_affected_markets = 0
        for g in detected_gaps:
            gs = datetime.strptime(g["gap_start_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
            ge = datetime.strptime(g["gap_end_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
            for cid in official_cids:
                if gs <= markets_by_cid[cid]["decision_time_utc"] <= ge:
                    n_gap_affected_markets += 1
        pct_markets_gap_affected = round(n_gap_affected_markets / len(official_cids) * 100, 2) if official_cids else 0.0

        # ============================================================ Overview ===
        ws = wb.create_sheet("Overview")
        ws["A1"] = "FORWARD PAPER VALIDATION -- ventana movil de 48h (NO backtest historico)"
        ws["A2"] = f"REPORT_CUTOFF_TS (unico, todas las hojas): {fmt(REPORT_CUTOFF_TS)}"
        ws["A3"] = f"Ventana analizada: {fmt(WINDOW_START_TS)} -> {fmt(REPORT_CUTOFF_TS)} ({WINDOW_HOURS:.0f}h)"
        ws["A4"] = (f"Cohorte: SOLO OFFICIAL. {n_pre_vpn_in_window} mercado(s) PRE_VPN_RECOVERY cayeron en la "
                    f"ventana y quedaron EXCLUIDOS de toda metrica de esta hoja en adelante.")
        ws["A5"] = f"Mercados OFFICIAL evaluados en la ventana (decision_time_utc dentro de ella): {len(official_cids)}"
        ws["A6"] = f"  Resueltos o con posicion abierta: {n_executed_mkt} | Pendientes (sin las 3 decisiones aun): {n_pending} | Omitidos (3 SKIPPED/NO_TRADE): {n_omitted_mkt}"
        ws["A7"] = f"Decisiones (mercado x estrategia) por fill_status: FULL={fill_status_counts.get('FULL',0)}  PARTIAL={fill_status_counts.get('PARTIAL',0)}  SKIPPED={fill_status_counts.get('SKIPPED',0)}  NO_TRADE={fill_status_counts.get('NO_TRADE',0)}"
        ws["A8"] = (f"paper_hedge_checks en la ventana: {n_hedge_checks_total} chequeos totales (COUNT unicamente, "
                    f"nunca volcados fila por fila), {n_hedge_checks_met} con all_conditions_met=1.")
        ws["A9"] = f"Coberturas realmente EJECUTADAS (paper_hedge_fills) en la ventana: {len(hedge_fills)}."
        ws["A10"] = f"Horas cubiertas por datos vs. huecos detectados: {effective_hours:.2f}h efectivas de {WINDOW_HOURS:.0f}h ({pct_coverage}%) -- detalle en 'Data Quality'."
        ws["A11"] = "Bases NO modificadas: data.db abierta mode=ro, paper_validation.db leida en una unica transaccion BEGIN DEFERRED. .env no leido. Ningun proceso reiniciado. Ninguna orden LIVE enviada."
        ws.column_dimensions["A"].width = 120

        # ============================================================ Strategy Summary ===
        ws = wb.create_sheet("Strategy Summary")
        ws["A1"] = "Cada estrategia es un experimento INDEPENDIENTE -- nunca sumar filas como portfolio combinado."
        strat_rows = []
        for strat in STRATEGIES:
            decs = decisions_by_strategy.get(strat, [])
            res = res_by_strategy.get(strat, [])
            n_mkts = len({d["condition_id"] for d in decs})
            n_exec = sum(1 for d in decs if d["fill_status"] in ("FULL", "PARTIAL"))
            wins = sum(1 for r in res if r["pnl_usd"] > 0)
            losses = sum(1 for r in res if r["pnl_usd"] <= 0)
            capital = sum(r["capital_deployed_usd"] for r in res)
            gross_pnl = sum(r["pnl_usd"] for r in res)
            fees = sum(_fee_for_decision(hedge_by_decision, decisions_by_id[r["decision_id"]]) for r in res
                       if r["decision_id"] in decisions_by_id)
            dd = _drawdown([r["pnl_usd"] for r in sorted(res, key=lambda r: r["resolved_at_utc"])])
            strat_rows.append({
                "strategy": strat, "mercados_unicos_evaluados": n_mkts, "operaciones_ejecutadas": n_exec,
                "resueltas": len(res), "ganadas": wins, "perdidas": losses,
                "win_rate_pct": round(wins / len(res) * 100, 1) if res else None,
                "capital_desplegado_usd": round(capital, 2),
                "gross_pnl_usd": round(gross_pnl, 2),
                "gross_roi_pct": round(gross_pnl / capital * 100, 2) if capital else None,
                "estimated_fees_usd": round(fees, 4),
                "estimated_pnl_after_fee_usd": round(gross_pnl - fees, 2),
                "estimated_roi_after_fee_pct": round((gross_pnl - fees) / capital * 100, 2) if capital else None,
                "max_drawdown_usd": round(dd, 2),
            })
        _write_table(ws, ["strategy", "mercados_unicos_evaluados", "operaciones_ejecutadas", "resueltas",
                          "ganadas", "perdidas", "win_rate_pct", "capital_desplegado_usd", "gross_pnl_usd",
                          "gross_roi_pct", "estimated_fees_usd", "estimated_pnl_after_fee_usd",
                          "estimated_roi_after_fee_pct", "max_drawdown_usd"], strat_rows, start_row=3)
        ws.column_dimensions["A"].width = 28

        # ============================================================ Same Markets Comparison ===
        ws = wb.create_sheet("Same Markets Comparison")
        ws["A1"] = "Comparaciones restringidas a la INTERSECCION exacta de condition_id con resolucion OFFICIAL en ambas estrategias -- nunca universos completos por separado."

        def _common_cmp(strat_a, strat_b):
            ra = {r["condition_id"]: r for r in res_by_strategy.get(strat_a, [])}
            rb = {r["condition_id"]: r for r in res_by_strategy.get(strat_b, [])}
            common = sorted(set(ra) & set(rb))
            out = []
            for tag, d in ((strat_a, ra), (strat_b, rb)):
                rs = [d[c] for c in common]
                capital = sum(r["capital_deployed_usd"] for r in rs)
                pnl = sum(r["pnl_usd"] for r in rs)
                fees = sum(_fee_for_decision(hedge_by_decision, decisions_by_id[r["decision_id"]]) for r in rs
                           if r["decision_id"] in decisions_by_id)
                out.append({
                    "comparacion": f"{strat_a} vs {strat_b}", "n_common_mercados": len(common), "strategy": tag,
                    "capital_usd": round(capital, 2), "gross_pnl_usd": round(pnl, 2),
                    "gross_roi_pct": round(pnl / capital * 100, 2) if capital else None,
                    "estimated_fees_usd": round(fees, 4),
                    "estimated_pnl_after_fee_usd": round(pnl - fees, 2),
                    "estimated_roi_after_fee_pct": round((pnl - fees) / capital * 100, 2) if capital else None,
                })
            return out

        cmp_rows = (_common_cmp("MOMENTUM_PURE", "POLYMARKET_FAVORITE_BASELINE")
                    + _common_cmp("MOMENTUM_PARTIAL_HEDGE", "MOMENTUM_PURE"))
        _write_table(ws, ["comparacion", "n_common_mercados", "strategy", "capital_usd", "gross_pnl_usd",
                          "gross_roi_pct", "estimated_fees_usd", "estimated_pnl_after_fee_usd",
                          "estimated_roi_after_fee_pct"], cmp_rows, start_row=3)
        ws.column_dimensions["A"].width = 34

        # ============================================================ Daily Results ===
        ws = wb.create_sheet("Daily Results")
        ws["A1"] = "P&L por dia calendario UTC de resolucion (resolved_at_utc), por estrategia."
        daily = defaultdict(lambda: defaultdict(list))
        for strat in STRATEGIES:
            for r in res_by_strategy.get(strat, []):
                day = datetime.fromtimestamp(r["resolved_at_utc"], timezone.utc).date().isoformat()
                daily[day][strat].append(r)
        daily_rows = []
        for day in sorted(daily):
            for strat in STRATEGIES:
                rs = daily[day].get(strat, [])
                if not rs:
                    continue
                capital = sum(r["capital_deployed_usd"] for r in rs)
                pnl = sum(r["pnl_usd"] for r in rs)
                fees = sum(_fee_for_decision(hedge_by_decision, decisions_by_id[r["decision_id"]]) for r in rs
                           if r["decision_id"] in decisions_by_id)
                wins = sum(1 for r in rs if r["pnl_usd"] > 0)
                daily_rows.append({
                    "dia_utc": day, "strategy": strat, "n_resolved": len(rs), "wins": wins,
                    "win_rate_pct": round(wins / len(rs) * 100, 1),
                    "capital_usd": round(capital, 2), "gross_pnl_usd": round(pnl, 2),
                    "gross_roi_pct": round(pnl / capital * 100, 2) if capital else None,
                    "estimated_pnl_after_fee_usd": round(pnl - fees, 2),
                })
        _write_table(ws, ["dia_utc", "strategy", "n_resolved", "wins", "win_rate_pct", "capital_usd",
                          "gross_pnl_usd", "gross_roi_pct", "estimated_pnl_after_fee_usd"], daily_rows, start_row=3)
        ws.column_dimensions["A"].width = 14

        # ============================================================ By Asset ===
        ws = wb.create_sheet("By Asset")
        ws["A1"] = "Mismo resumen de Strategy Summary, desglosado por activo subyacente."
        asset_rows = []
        for strat in STRATEGIES:
            for asset in ASSETS:
                rs = [r for r in res_by_strategy.get(strat, []) if markets_by_cid.get(r["condition_id"], {}).get("asset_symbol") == asset]
                capital = sum(r["capital_deployed_usd"] for r in rs)
                pnl = sum(r["pnl_usd"] for r in rs)
                fees = sum(_fee_for_decision(hedge_by_decision, decisions_by_id[r["decision_id"]]) for r in rs
                           if r["decision_id"] in decisions_by_id)
                wins = sum(1 for r in rs if r["pnl_usd"] > 0)
                asset_rows.append({
                    "strategy": strat, "asset": asset, "n_resolved": len(rs), "wins": wins,
                    "win_rate_pct": round(wins / len(rs) * 100, 1) if rs else None,
                    "capital_usd": round(capital, 2), "gross_pnl_usd": round(pnl, 2),
                    "gross_roi_pct": round(pnl / capital * 100, 2) if capital else None,
                    "estimated_pnl_after_fee_usd": round(pnl - fees, 2),
                    "estimated_roi_after_fee_pct": round((pnl - fees) / capital * 100, 2) if capital else None,
                })
        _write_table(ws, ["strategy", "asset", "n_resolved", "wins", "win_rate_pct", "capital_usd",
                          "gross_pnl_usd", "gross_roi_pct", "estimated_pnl_after_fee_usd",
                          "estimated_roi_after_fee_pct"], asset_rows, start_row=3)
        ws.column_dimensions["A"].width = 28

        # ============================================================ Executed Signals ===
        ws = wb.create_sheet("Executed Signals")
        ws["A1"] = "Una fila por decision EJECUTADA (FULL/PARTIAL) -- no incluye SKIPPED/NO_TRADE (ver 'Skipped Reasons')."
        exec_rows = []
        for d in decisions:
            if d["fill_status"] not in ("FULL", "PARTIAL"):
                continue
            m = markets_by_cid.get(d["condition_id"], {})
            r = next((rr for rr in res_by_strategy.get(d["strategy"], []) if rr["decision_id"] == d["id"]), None)
            exec_rows.append({
                "condition_id": d["condition_id"], "asset": m.get("asset_symbol"), "strategy": d["strategy"],
                "decision_time_utc": fmt(d["decision_timestamp_utc"]), "side_chosen": d["side_chosen"],
                "executable_price": d["executable_price"], "shares": d["shares"], "capital_usd": d["capital_usd"],
                "fill_status": d["fill_status"], "resuelto": r is not None,
                "winner": r["winner"] if r else None, "pnl_usd": round(r["pnl_usd"], 4) if r else None,
            })
        _write_table(ws, ["condition_id", "asset", "strategy", "decision_time_utc", "side_chosen",
                          "executable_price", "shares", "capital_usd", "fill_status", "resuelto", "winner",
                          "pnl_usd"], exec_rows, start_row=3)
        ws.column_dimensions["A"].width = 26

        # ============================================================ Executed Hedges ===
        ws = wb.create_sheet("Executed Hedges")
        ws["A1"] = ("Coberturas REALMENTE EJECUTADAS (paper_hedge_fills), no chequeos -- ver Overview/Data "
                    "Quality para el COUNT total de chequeos. 'efecto_en_pnl_usd' = pnl CON cobertura menos "
                    "pnl hipotetico SIN ella (solo la pierna primaria), aislando el aporte de cada cobertura.")
        hedge_rows = []
        total_hedge_effect = 0.0
        for h in hedge_fills:
            d = decisions_by_id.get(h["decision_id"])
            if d is None:
                continue
            r = next((rr for rr in res_by_strategy.get(d["strategy"], []) if rr["decision_id"] == d["id"]), None)
            if r is None:
                continue
            payout_without_hedge = (d["shares"] or 0.0) if d["side_chosen"] == r["winner"] else 0.0
            capital_without_hedge = d["capital_usd"] or 0.0
            pnl_without_hedge = payout_without_hedge - capital_without_hedge
            effect = r["pnl_usd"] - pnl_without_hedge
            total_hedge_effect += effect
            hedge_rows.append({
                "condition_id": h["condition_id"], "asset": markets_by_cid.get(h["condition_id"], {}).get("asset_symbol"),
                "executed_at_utc": fmt(h["executed_at_utc"]), "target_shares": h["target_shares"],
                "filled_shares": h["filled_shares"], "usd_spent": h["usd_spent"], "vwap": h["vwap"],
                "fill_status": h["fill_status"], "pnl_con_cobertura_usd": round(r["pnl_usd"], 4),
                "pnl_sin_cobertura_usd": round(pnl_without_hedge, 4), "efecto_en_pnl_usd": round(effect, 4),
            })
        ws["A2"] = f"Total coberturas ejecutadas: {len(hedge_rows)}. Efecto agregado sobre P&L: ${round(total_hedge_effect, 2)}."
        _write_table(ws, ["condition_id", "asset", "executed_at_utc", "target_shares", "filled_shares",
                          "usd_spent", "vwap", "fill_status", "pnl_con_cobertura_usd", "pnl_sin_cobertura_usd",
                          "efecto_en_pnl_usd"], hedge_rows, start_row=4)
        ws.column_dimensions["A"].width = 26

        # ============================================================ Skipped Reasons ===
        ws = wb.create_sheet("Skipped Reasons")
        ws["A1"] = "SKIPPED/NO_TRADE agrupados por motivo (skip_or_partial_reason), por estrategia."
        reason_counts = defaultdict(int)
        for d in decisions:
            if d["fill_status"] in ("SKIPPED", "NO_TRADE"):
                reason_counts[(d["strategy"], d["fill_status"], d["skip_or_partial_reason"] or "(sin motivo registrado)")] += 1
        reason_rows = []
        for (strat, status, reason), n in sorted(reason_counts.items(), key=lambda kv: (-kv[1])):
            n_strat_total = len(decisions_by_strategy.get(strat, [])) or 1
            reason_rows.append({"strategy": strat, "fill_status": status, "motivo": reason, "n": n,
                                "pct_del_total_evaluado_por_estrategia": round(n / n_strat_total * 100, 2)})
        _write_table(ws, ["strategy", "fill_status", "motivo", "n", "pct_del_total_evaluado_por_estrategia"],
                     reason_rows, start_row=3)
        ws.column_dimensions["C"].width = 60

        # ============================================================ Data Quality ===
        ws = wb.create_sheet("Data Quality")
        ws["A1"] = f"Ventana: {fmt(WINDOW_START_TS)} -> {fmt(REPORT_CUTOFF_TS)} ({WINDOW_HOURS:.0f}h nominal)."
        ws["A2"] = f"Horas efectivamente cubiertas por datos (nominal menos huecos detectados): {effective_hours:.2f}h ({pct_coverage}%)."
        ws["A3"] = (f"Cobertura real de orderbook_snapshots en la ventana: {fmt(ob_coverage['a']) if ob_coverage and ob_coverage['a'] else 'sin datos'} "
                    f"-> {fmt(ob_coverage['b']) if ob_coverage and ob_coverage['b'] else 'sin datos'}.")
        ws["A4"] = (f"Huecos detectados por delta entre snapshots consecutivos > {GAP_THRESHOLD_S:.0f}s: "
                    f"{len(detected_gaps)}, total {total_gap_hours:.3f}h.")
        ws["A5"] = f"Mercados OFFICIAL con decision_time_utc dentro de algun hueco detectado: {n_gap_affected_markets}/{len(official_cids)} ({pct_markets_gap_affected}%)."
        ws["A6"] = f"Eventos collector_events (gap/reconnect/error) en la ventana: {len(gap_events)}."
        ws["A7"] = f"paper_hedge_checks en la ventana: {n_hedge_checks_total} (COUNT unicamente -- nunca exportados fila por fila)."
        ws["A8"] = f"Fee estimado: fee = shares * {TAKER_FEE_RATE_CRYPTO} * VWAP * (1-VWAP), fuente: {TAKER_FEE_SOURCE}. Fee de redencion: DESCONOCIDO, no aplicado."
        ws.cell(row=10, column=1, value="Huecos detectados (delta entre snapshots consecutivos):")
        next_row_dq = _write_table(ws, ["gap_start_utc", "gap_end_utc", "duration_s"], detected_gaps, start_row=12)
        ws.cell(row=next_row_dq + 1, column=1, value="Eventos collector_events (gap/reconnect/error):")
        gap_event_rows = [{"ts_utc": fmt(e["ts"]), "component": e["component"], "event_type": e["event_type"],
                           "detail": e["detail"]} for e in gap_events]
        _write_table(ws, ["ts_utc", "component", "event_type", "detail"], gap_event_rows, start_row=next_row_dq + 3)
        ws.column_dimensions["A"].width = 100

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"wrote {out_path}")
    return out_path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "export" / "paper_last_48h_compact.xlsx"
    build(out)
