"""
Métricas y alertas de calidad de datos. No corrige nada -- solo mide y
reporta, para que el usuario sepa cuánto confiar en cada tramo de datos.
"""
import time

import db


def compute_report() -> dict:
    with db.connect() as conn:
        def scalar(sql, *args):
            row = conn.execute(sql, args).fetchone()
            return row[0] if row else None

        n_leader_trades = scalar("SELECT count(*) FROM leader_trades")
        n_market_trades = scalar("SELECT count(*) FROM market_trades")
        n_orderbook_snapshots = scalar("SELECT count(*) FROM orderbook_snapshots")
        n_underlying_prices = scalar("SELECT count(*) FROM underlying_prices")

        n_markets = scalar("SELECT count(*) FROM markets")
        n_markets_resolved = scalar("SELECT count(*) FROM markets WHERE winner IS NOT NULL")
        n_markets_closed_unresolved = scalar(
            "SELECT count(*) FROM markets WHERE winner IS NULL AND close_time_utc < ?", time.time()
        )
        n_leader_trades_no_market = scalar(
            "SELECT count(*) FROM leader_trades WHERE condition_id IS NULL OR condition_id NOT IN "
            "(SELECT condition_id FROM markets)"
        )

        n_leader_trades_null_required = scalar(
            "SELECT count(*) FROM leader_trades WHERE price IS NULL OR shares IS NULL OR outcome IS NULL"
        )

        # context coverage: a trade has "full context" if all 9 offsets exist and are context_available
        n_trades_full_context = scalar(
            """SELECT count(*) FROM (
                 SELECT leader_trade_id FROM trade_context
                 GROUP BY leader_trade_id
                 HAVING count(*) = 9 AND sum(context_available) = 9
               )"""
        )
        n_trades_any_context_row = scalar(
            "SELECT count(DISTINCT leader_trade_id) FROM trade_context"
        )

        poll_agg = conn.execute(
            "SELECT count(*) n_polls, sum(n_returned) tot_returned, sum(n_new) tot_new, "
            "sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) n_poll_errors, "
            "sum(CASE WHEN n_gap_filled > 0 THEN 1 ELSE 0 END) n_polls_with_gap, "
            "COALESCE(sum(n_gap_filled), 0) n_trades_recovered_from_gaps, "
            "COALESCE(sum(gap_pages_fetched), 0) n_extra_pages_fetched FROM leader_poll_log"
        ).fetchone()

        # Margen real de la ventana de 100 trades: cuántos segundos de historia
        # cubre cada página. Si esto cae cerca del intervalo de poll, el riesgo
        # de perder trades es real; mientras sea holgado, no lo es.
        page_span = conn.execute(
            "SELECT avg(newest_ts_in_page - oldest_ts_in_page) avg_span, "
            "min(newest_ts_in_page - oldest_ts_in_page) min_span FROM leader_poll_log "
            "WHERE oldest_ts_in_page IS NOT NULL AND newest_ts_in_page IS NOT NULL"
        ).fetchone()
        n_polls = poll_agg["n_polls"] or 0
        tot_returned = poll_agg["tot_returned"] or 0
        tot_new = poll_agg["tot_new"] or 0
        n_duplicate_detections = max(tot_returned - tot_new, 0)  # expected overlap across 100-limit polls

        events = conn.execute(
            "SELECT component, event_type, count(*) c FROM collector_events "
            "GROUP BY component, event_type ORDER BY component, event_type"
        ).fetchall()

        coverage_range = conn.execute(
            "SELECT min(received_at_utc_ms)/1000.0 a, max(received_at_utc_ms)/1000.0 b FROM orderbook_snapshots"
        ).fetchone()

    coverage_hours = None
    if coverage_range and coverage_range["a"] and coverage_range["b"]:
        coverage_hours = (coverage_range["b"] - coverage_range["a"]) / 3600.0

    pct_full_context = (n_trades_full_context / n_leader_trades * 100) if n_leader_trades else None

    return {
        "generated_at": time.time(),
        "counts": {
            "leader_trades": n_leader_trades,
            "market_trades": n_market_trades,
            "orderbook_snapshots": n_orderbook_snapshots,
            "underlying_prices": n_underlying_prices,
            "markets": n_markets,
            "markets_resolved": n_markets_resolved,
            "markets_closed_unresolved": n_markets_closed_unresolved,
        },
        "coverage": {
            "orderbook_span_hours": coverage_hours,
            "orderbook_span_start": coverage_range["a"] if coverage_range else None,
            "orderbook_span_end": coverage_range["b"] if coverage_range else None,
        },
        "leader_trade_quality": {
            "n_no_matching_market": n_leader_trades_no_market,
            "n_null_required_fields": n_leader_trades_null_required,
            "n_with_full_trade_context": n_trades_full_context,
            "n_with_any_trade_context": n_trades_any_context_row,
            "pct_with_full_trade_context": pct_full_context,
        },
        "polling": {
            "n_polls": n_polls,
            "total_returned": tot_returned,
            "total_new": tot_new,
            "n_duplicate_detections_expected_overlap": n_duplicate_detections,
            "n_poll_errors": poll_agg["n_poll_errors"] or 0,
        },
        "gaps": {
            # Un "gap" = el trade más viejo de una página era más nuevo que lo
            # último guardado, o sea que entre dos polls hubo trades que no
            # entraron en los 100. Cuando pasa, se paginan hacia atrás y se
            # recuperan -- estos números dicen cuántas veces pasó y cuánto se
            # recuperó, no cuánto se perdió.
            "n_polls_with_gap": poll_agg["n_polls_with_gap"] or 0,
            "n_trades_recovered_from_gaps": poll_agg["n_trades_recovered_from_gaps"] or 0,
            "n_extra_pages_fetched": poll_agg["n_extra_pages_fetched"] or 0,
            # Margen de seguridad: segundos de historia que cubre una página de
            # 100 trades. Mientras sea >> el intervalo de poll (1s), no se pierde nada.
            "avg_page_span_seconds": page_span["avg_span"] if page_span else None,
            "min_page_span_seconds": page_span["min_span"] if page_span else None,
        },
        "events_by_type": [dict(r) for r in events],
    }


def print_report():
    import json
    print(json.dumps(compute_report(), indent=2, default=str))


if __name__ == "__main__":
    print_report()
