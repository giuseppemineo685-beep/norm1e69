"""
Informe de verificación del run: cobertura, conteos, tamaño y consistencia.
Responde exactamente las preguntas que hay que contestar antes de desplegar.

Uso:  python3 verify_run.py
"""
import os
import time
from datetime import datetime, timezone

import db
from config import DB_PATH


def _fmt_ts(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main():
    with db.connect() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()

        # ---------- duración del run ----------
        span = q("""SELECT min(ts) a, max(ts) b FROM leader_poll_log""")
        hours = ((span["b"] - span["a"]) / 3600) if (span and span["a"]) else 0

        print("=" * 72)
        print(f"INFORME DE VERIFICACIÓN — {_fmt_ts(time.time())}")
        print(f"Duración del run: {hours:.2f} h  ({_fmt_ts(span['a'])} → {_fmt_ts(span['b'])})")
        print("=" * 72)

        # ---------- 1. tamaño y filas ----------
        print("\n[1] TAMAÑO Y FILAS")
        total_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                size = os.path.getsize(p)
                total_bytes += size
                print(f"  {os.path.basename(p):24s} {size/1e6:8.1f} MB")
        print(f"  {'TOTAL':24s} {total_bytes/1e6:8.1f} MB"
              + (f"   → ~{total_bytes/1e6/hours*24:.0f} MB/día proyectado" if hours > 0.1 else ""))

        tables = ["markets", "leader_trades", "market_trades", "orderbook_snapshots",
                  "orderbook_levels", "underlying_prices", "leader_inventory_timeline",
                  "trade_context", "leader_poll_log", "collector_events"]
        print()
        total_rows = 0
        for t in tables:
            n = q(f"SELECT count(*) c FROM {t}")["c"]
            total_rows += n
            rate = f"{n/hours/1000:.1f}k/h" if hours > 0.1 else ""
            print(f"  {t:28s} {n:9,d}  {rate}")
        print(f"  {'TOTAL FILAS':28s} {total_rows:9,d}")

        # ---------- 2. conteos de trades ----------
        print("\n[2] TRADES DEL LÍDER: DETECTADOS / DEDUPLICADOS / RECUPERADOS / SIN RESOLVER / SIN CONTEXTO")
        polls = q("""SELECT count(*) n_polls, COALESCE(sum(n_returned),0) returned,
                            COALESCE(sum(n_new),0) new_rows,
                            COALESCE(sum(n_gap_filled),0) recovered,
                            COALESCE(sum(CASE WHEN n_gap_filled>0 THEN 1 ELSE 0 END),0) polls_with_gap,
                            COALESCE(sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END),0) errors
                     FROM leader_poll_log""")
        live = q("SELECT count(*) c FROM leader_trades WHERE collection_method='LIVE'")["c"]
        backfill = q("SELECT count(*) c FROM leader_trades WHERE collection_method='BACKFILL'")["c"]
        dup = max(polls["returned"] - polls["new_rows"], 0)

        print(f"  polls realizados                     {polls['n_polls']:9,d}"
              f"   ({polls['n_polls']/max(span['b']-span['a'],1):.2f}/s)")
        print(f"  trades devueltos por la API (bruto)  {polls['returned']:9,d}")
        print(f"  insertados nuevos (LIVE)             {polls['new_rows']:9,d}")
        print(f"  DEDUPLICADOS (ya vistos, ignorados)  {dup:9,d}   <- solapamiento esperado del poll")
        print(f"  RECUPERADOS por relleno de hueco     {polls['recovered']:9,d}   ({polls['polls_with_gap']} polls con hueco)")
        print(f"  errores de poll                      {polls['errors']:9,d}")
        print(f"  total en tabla: LIVE={live:,}  BACKFILL={backfill:,}")

        unresolved = q("""SELECT count(*) c FROM markets
                          WHERE winner IS NULL AND close_time_utc IS NOT NULL AND close_time_utc < ?""",
                       time.time())["c"]
        resolved = q("SELECT count(*) c FROM markets WHERE winner IS NOT NULL")["c"]
        n_markets = q("SELECT count(*) c FROM markets")["c"]
        print(f"  mercados: {n_markets} descubiertos, {resolved} resueltos, "
              f"{unresolved} cerrados SIN RESOLVER todavía")

        no_market = q("""SELECT count(*) c FROM leader_trades
                         WHERE condition_id IS NULL OR condition_id NOT IN
                               (SELECT condition_id FROM markets)""")["c"]
        no_times = q("SELECT count(*) c FROM leader_trades WHERE seconds_to_market_close IS NULL")["c"]
        print(f"  trades sin mercado asociado          {no_market:9,d}")
        print(f"  trades sin seconds_to_market_close   {no_times:9,d}")

        # ---------- 3. cobertura de contexto ----------
        print("\n[3] COBERTURA DE CONTEXTO (% de trades con order book y subyacente válidos)")
        for method in ("LIVE", "BACKFILL"):
            tot = q("SELECT count(*) c FROM leader_trades WHERE collection_method=?", method)["c"]
            if not tot:
                continue
            processed = q("""SELECT count(DISTINCT tc.leader_trade_id) c FROM trade_context tc
                             JOIN leader_trades lt ON lt.id=tc.leader_trade_id
                             WHERE lt.collection_method=?""", method)["c"]
            ob_at0 = q("""SELECT count(*) c FROM trade_context tc
                          JOIN leader_trades lt ON lt.id=tc.leader_trade_id
                          WHERE lt.collection_method=? AND tc.offset_seconds=0
                            AND tc.context_available=1""", method)["c"]
            und_at0 = q("""SELECT count(*) c FROM trade_context tc
                           JOIN leader_trades lt ON lt.id=tc.leader_trade_id
                           WHERE lt.collection_method=? AND tc.offset_seconds=0
                             AND tc.underlying_available=1""", method)["c"]
            both = q("""SELECT count(*) c FROM trade_context tc
                        JOIN leader_trades lt ON lt.id=tc.leader_trade_id
                        WHERE lt.collection_method=? AND tc.offset_seconds=0
                          AND tc.context_available=1 AND tc.underlying_available=1""", method)["c"]
            full9 = q("""SELECT count(*) c FROM (
                           SELECT tc.leader_trade_id FROM trade_context tc
                           JOIN leader_trades lt ON lt.id=tc.leader_trade_id
                           WHERE lt.collection_method=?
                           GROUP BY tc.leader_trade_id
                           HAVING count(*)=9 AND sum(tc.context_available)=9)""", method)["c"]
            pct = lambda n: f"{n/tot*100:5.1f}%"
            print(f"  {method}: {tot:,} trades, {processed:,} procesados")
            print(f"    con order book en t=0            {ob_at0:7,d}  {pct(ob_at0)}")
            print(f"    con subyacente en t=0            {und_at0:7,d}  {pct(und_at0)}")
            print(f"    con AMBOS en t=0                 {both:7,d}  {pct(both)}")
            print(f"    con los 9 offsets completos      {full9:7,d}  {pct(full9)}")

        pending_ctx = q("""SELECT count(*) c FROM leader_trades lt
                           LEFT JOIN trade_context tc ON tc.leader_trade_id=lt.id AND tc.offset_seconds=0
                           WHERE tc.id IS NULL""")["c"]
        print(f"  SIN CONTEXTO todavía (cola pendiente)  {pending_ctx:7,d}")

        # ---------- 4. anti-look-ahead ----------
        print("\n[4] GARANTÍA ANTI-LOOK-AHEAD")
        neg = q("SELECT count(*) c FROM trade_context WHERE snapshot_age_s < 0")["c"]
        print(f"  filas con snapshot POSTERIOR al instante: {neg}   "
              f"{'OK (ninguna)' if neg == 0 else '<<< FALLO'}")

        # ---------- 5. densidad del order book ----------
        print("\n[5] DENSIDAD DEL ORDER BOOK")
        for src in ("REST", "WS", "WS_price_change"):
            r = q("""SELECT count(*) c, count(DISTINCT token_id) tk,
                            min(received_at_utc_ms)/1000.0 a, max(received_at_utc_ms)/1000.0 b
                     FROM orderbook_snapshots WHERE source=?""", src)
            if r["c"] and r["b"] and r["b"] > r["a"]:
                print(f"  {src:16s} {r['c']:8,d} filas, {r['tk']:2d} tokens, "
                      f"{r['c']/r['tk']/(r['b']-r['a']):.2f}/s por token")

        # ---------- 6. eventos ----------
        print("\n[6] EVENTOS DEL COLLECTOR")
        for r in conn.execute("""SELECT component, event_type, count(*) c FROM collector_events
                                 GROUP BY component, event_type ORDER BY c DESC LIMIT 15"""):
            print(f"  {r['component']:18s} {r['event_type']:30s} {r['c']:6,d}")

        # ---------- 7. integridad ----------
        print("\n[7] INTEGRIDAD / DUPLICADOS")
        dups = q("""SELECT count(*) c FROM (
                      SELECT leader_wallet, transaction_hash, source_timestamp_utc, token_id, shares
                      FROM leader_trades
                      GROUP BY leader_wallet, transaction_hash, source_timestamp_utc, token_id, shares
                      HAVING count(*) > 1)""")["c"]
        inv_dups = q("""SELECT count(*) c FROM (SELECT after_trade_id FROM leader_inventory_timeline
                        GROUP BY after_trade_id HAVING count(*)>1)""")["c"]
        ctx_dups = q("""SELECT count(*) c FROM (SELECT leader_trade_id, offset_seconds FROM trade_context
                        GROUP BY leader_trade_id, offset_seconds HAVING count(*)>1)""")["c"]
        print(f"  leader_trades duplicados             {dups}   {'OK' if dups==0 else '<<< FALLO'}")
        print(f"  inventory duplicados                 {inv_dups}   {'OK' if inv_dups==0 else '<<< FALLO'}")
        print(f"  trade_context duplicados             {ctx_dups}   {'OK' if ctx_dups==0 else '<<< FALLO'}")

        gaps_in_trades = q("""SELECT count(*) c FROM (
              SELECT source_timestamp_utc,
                     LAG(source_timestamp_utc) OVER (ORDER BY source_timestamp_utc) prev
              FROM leader_trades WHERE collection_method='LIVE')
              WHERE prev IS NOT NULL AND source_timestamp_utc - prev > 120""")["c"]
        print(f"  huecos >120s en la serie de trades   {gaps_in_trades}")

        print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
