"""
trader_strategy_research.py -- analizador LOCAL, solo lectura, para (a) inferir
el comportamiento del trader lider y (b) evaluar candidatas de estrategia
sobre el universo COMPLETO de mercados, con anti-look-ahead estricto.

Se ejecuta en la Mac del usuario contra las bases reales:

    python3 trader_strategy_research.py \
        --collector-db /ruta/data.db \
        --paper-db /ruta/paper_validation.db \
        --output-dir /ruta/export

GARANTIAS (verificadas por tests/test_trader_strategy_research.py):
  - SQLite exclusivamente en `mode=ro` (URI). Sin init_db, sin migraciones,
    sin ALTER/INSERT/UPDATE/DELETE. Un intento de escritura falla a nivel de
    driver.
  - Sin red: no importa requests/websockets/urllib/polymarket_api.
  - Sin trading: no importa polymarket, scripts.live_trader, live_micro*.
  - No lee .env ni ninguna variable de credenciales.
  - Corte temporal unico (REPORT_CUTOFF_TS): se fija UNA vez al arrancar
    (o con --cutoff-ts para reproducibilidad exacta) y TODAS las consultas
    lo usan como limite superior.
  - Union canonica historico (leader_trades_v2, origin DATA_API_HISTORY) +
    LIVE (leader_trades, collection_method='LIVE', is_startup_batch=0) con
    reconciliacion multiset por clave natural -- nunca doble-cuenta el
    solape, nunca colapsa dos fills legitimos distintos. Cada fill canonico
    conserva su origen y su id de fila fuente.
  - Anti-look-ahead: cualquier snapshot/lectura usada para el instante t
    exige event_ts <= t Y received_ts <= t (misma regla conservadora que
    trade_context.py post-c8813ee), con tolerancia de antiguedad acotada.
  - Inventario reconstruido DESDE CERO por mercado, BUY y SELL (SELL reduce
    shares a coste medio y realiza P&L), sobre la union canonica completa --
    NO sobre leader_inventory_timeline (que ignora SELL y usa la tabla vieja
    con duplicados) ni sobre el subconjunto "limpio" de trade_context.
  - Resultados pendientes (mercados sin winner al corte) separados.

Los archivos de salida son compactos: nunca se vuelcan chequeos repetitivos
(paper_hedge_checks solo se COUNT-ea). El unico archivo "grande" es
trader_fills_canonical.csv (una fila por fill del lider, ~1e4), necesario
para la trazabilidad fill -> origen; se desactiva con --no-fills.
"""
import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:  # openpyxl es opcional: sin el, se omite el .xlsx y se avisa
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

VERSION = "0.1.0"
HISTORY_ORIGIN = "DATA_API_HISTORY"
DEFAULT_LEADER_WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"  # publico (proxy wallet del lider)
ASSETS = ("BTC", "ETH", "SOL")
TICK = 0.01

# Fee: formula documentada en export_paper_validation.py (taker, categoria
# crypto). Es una ASUNCION: el maker/taker del lider es desconocido (NULL en
# todos sus fills auditados) -- por eso cada P&L se reporta bruto y con fee
# estimada por separado, nunca solo neto.
DEFAULT_TAKER_FEE_RATE = 0.07


def taker_fee(shares, price, rate):
    if shares is None or price is None or rate is None:
        return 0.0
    return shares * rate * price * (1.0 - price)


def fmt(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def day_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def wilson_ci(k, n, z=1.96):
    if not n:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(centre - half, 4), round(centre + half, 4))


def binom_two_sided_p(k, n, p0=0.5):
    """p-valor exacto bilateral (suma de masas <= masa observada). n chico
    (<~2000) -- stdlib, sin scipy."""
    if not n:
        return None
    if n > 3000:  # aproximacion normal para no iterar millones de veces
        mu, sd = n * p0, math.sqrt(n * p0 * (1 - p0))
        z = (k - mu) / sd if sd else 0.0
        return round(math.erfc(abs(z) / math.sqrt(2)), 6)
    pk = math.comb(n, k) * p0 ** k * (1 - p0) ** (n - k)
    total = 0.0
    for i in range(n + 1):
        pi = math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i)
        if pi <= pk + 1e-15:
            total += pi
    return round(min(1.0, total), 6)


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(cov / (vx * vy), 4) if vx and vy else None


def quantiles(values, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    v = sorted(x for x in values if x is not None)
    if not v:
        return {q: None for q in qs}
    out = {}
    for q in qs:
        idx = min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))
        out[q] = v[idx]
    return out


def drawdown(pnls):
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


# ============================================================ acceso RO ===
def open_ro(path):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def columns_of(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def rows(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def scalar(conn, sql, *params):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


# ============================================================ contexto ===
class Ctx:
    def __init__(self, args):
        self.args = args
        self.cutoff = float(args.cutoff_ts) if args.cutoff_ts else time.time()
        self.leader = args.leader_wallet
        self.tol = args.snapshot_tolerance_s
        self.upad = args.underlying_pad_s
        self.fee_rate = args.fee_rate
        self.stake = args.stake_usd
        self.decision_offset = args.decision_offset_s
        self.rng = random.Random(args.seed)
        self.warnings = []
        self.data = open_ro(args.collector_db)
        self.paper = open_ro(args.paper_db) if args.paper_db else None
        self.notes = {}

    def warn(self, msg):
        self.warnings.append(msg)
        print(f"[warn] {msg}", file=sys.stderr)


# ================================================== fills canonicos ===
def natural_key(tx, ts, cid, outcome, side, price, shares):
    return (tx or "", int(round(ts or 0)), cid or "", outcome or "", side or "",
            round(float(price), 6) if price is not None else None,
            round(float(shares), 6) if shares is not None else None)


def load_canonical_fills(ctx):
    """Union historico (leader_trades_v2) + LIVE (leader_trades). Reconciliacion
    multiset: por cada clave natural, el historico fija cuantas instancias hay;
    las filas LIVE con la misma clave se consideran la MISMA observacion hasta
    agotar ese conteo (matched) y a partir de ahi son fills nuevos (live_only)."""
    c = ctx.data
    stats = Counter()
    fills = []
    hist = []
    if table_exists(c, "leader_trades_v2"):
        hist = rows(c, """
            SELECT id, transaction_hash, condition_id, market_slug, market_title, asset_symbol,
                   token_id, outcome, side, price, shares, usdc_amount, source_timestamp_utc,
                   dedup_ambiguous, multiplicity_total
            FROM leader_trades_v2
            WHERE origin=? AND leader_wallet=? AND source_timestamp_utc <= ?
            ORDER BY source_timestamp_utc, id
        """, HISTORY_ORIGIN, ctx.leader, ctx.cutoff)
        stats["hist_rows"] = len(hist)
        stats["hist_dedup_ambiguous"] = sum(1 for r in hist if r["dedup_ambiguous"])
    else:
        ctx.warn("leader_trades_v2 no existe: solo se usa LIVE (sin historico canonico)")

    live = rows(c, """
        SELECT id, transaction_hash, condition_id, market_slug, market_title, asset_symbol,
               token_id, outcome, side, price, shares, usdc_amount, source_timestamp_utc,
               api_received_at, received_at_utc
        FROM leader_trades
        WHERE leader_wallet=? AND collection_method='LIVE' AND COALESCE(is_startup_batch,0)=0
          AND source_timestamp_utc <= ?
        ORDER BY source_timestamp_utc, id
    """, ctx.leader, ctx.cutoff)
    stats["live_rows"] = len(live)
    stats["excluded_startup_batch"] = scalar(c, """SELECT count(*) FROM leader_trades
        WHERE leader_wallet=? AND collection_method='LIVE' AND COALESCE(is_startup_batch,0)=1""", ctx.leader) or 0
    stats["excluded_old_backfill_rows"] = scalar(c, """SELECT count(*) FROM leader_trades
        WHERE leader_wallet=? AND collection_method='BACKFILL'""", ctx.leader) or 0

    hist_counts = Counter()
    for r in hist:
        k = natural_key(r["transaction_hash"], r["source_timestamp_utc"], r["condition_id"], r["outcome"],
                        r["side"], r["price"], r["shares"])
        hist_counts[k] += 1
        fills.append(_fill_from(r, origin="HISTORY_V2", source_table="leader_trades_v2"))
    seen = Counter()
    for r in live:
        k = natural_key(r["transaction_hash"], r["source_timestamp_utc"], r["condition_id"], r["outcome"],
                        r["side"], r["price"], r["shares"])
        if seen[k] < hist_counts.get(k, 0):
            seen[k] += 1
            stats["live_matched_to_history"] += 1
            continue
        seen[k] += 1
        stats["live_only"] += 1
        fills.append(_fill_from(r, origin="LIVE", source_table="leader_trades"))
    stats["hist_only"] = stats["hist_rows"] - stats["live_matched_to_history"]
    stats["canonical_fills"] = len(fills)

    # filas viejas BACKFILL que NO estan cubiertas por la union canonica (huerfanas)
    if stats["excluded_old_backfill_rows"]:
        keys = {natural_key(f["tx"], f["ts"], f["condition_id"], f["outcome"], f["side"], f["price"], f["shares"])
                for f in fills}
        old = rows(c, """SELECT transaction_hash, source_timestamp_utc, condition_id, outcome, side, price, shares
                         FROM leader_trades WHERE leader_wallet=? AND collection_method='BACKFILL'""", ctx.leader)
        stats["old_backfill_not_in_canonical"] = sum(
            1 for r in old if natural_key(r["transaction_hash"], r["source_timestamp_utc"], r["condition_id"],
                                          r["outcome"], r["side"], r["price"], r["shares"]) not in keys)

    fills.sort(key=lambda f: (f["ts"], 0 if f["origin"] == "HISTORY_V2" else 1, f["source_row_id"]))
    for i, f in enumerate(fills, start=1):
        f["fill_id"] = i
    return fills, dict(stats)


def _fill_from(r, origin, source_table):
    price = float(r["price"]) if r["price"] is not None else None
    shares = float(r["shares"]) if r["shares"] is not None else None
    outcome = (r["outcome"] or "").capitalize() or None   # 'UP'/'up' -> 'Up'
    side = (r["side"] or "").upper() or None
    return {
        "origin": origin, "source_table": source_table, "source_row_id": r["id"],
        "tx": r["transaction_hash"], "ts": float(r["source_timestamp_utc"]),
        "condition_id": r["condition_id"], "market_slug": r["market_slug"], "market_title": r["market_title"],
        "asset": (r["asset_symbol"] or "").upper() or None, "token_id": r["token_id"], "outcome": outcome,
        "side": side, "price": price, "shares": shares,
        "usd": (price * shares) if (price is not None and shares is not None) else None,
    }


# ======================================================== mercados ===
def load_markets(ctx):
    cols = columns_of(ctx.data, "markets")
    wm = "window_minutes" if "window_minutes" in cols else "NULL AS window_minutes"
    ms = rows(ctx.data, f"""
        SELECT condition_id, market_slug, market_title, asset_symbol, token_up_id, token_down_id,
               open_time_utc, close_time_utc, resolution_time_utc, winner, open_reference_price, {wm}
        FROM markets
    """)
    out = {}
    for m in ms:
        m["asset_symbol"] = (m["asset_symbol"] or "").upper() or None
        w = (m["winner"] or "").capitalize()
        m["winner"] = w if w in ("Up", "Down") else None
        # winner conocido al corte: si hay resolution_time_utc posterior al corte, se trata como pendiente
        if m["winner"] and m["resolution_time_utc"] and m["resolution_time_utc"] > ctx.cutoff:
            m["winner_at_cutoff"] = None
        else:
            m["winner_at_cutoff"] = m["winner"]
        out[m["condition_id"]] = m
    return out


# ============================================ order book / subyacente ===
def nearest_snapshot(conn, condition_id, token_id, t, tol):
    """Ultimo snapshot con event_ts <= t Y received_ts <= t (regla conservadora).
    None si no hay o si el mas reciente valido es mas viejo que tol."""
    if token_id is None:
        return None
    # received_at_utc_ms <= t*1000 se escribe columna-vs-constante (no
    # received_at_utc_ms/1000.0 <= t) para que SQLite use el indice
    # (token_id, received_at_utc_ms) como rango: con millones de filas WS la
    # forma con expresion obligaba a recorrer todo lo posterior a t.
    row = conn.execute("""
        SELECT id, best_bid, best_ask, best_bid_size, best_ask_size, source, received_at_utc_ms,
               COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) AS ts
        FROM orderbook_snapshots
        WHERE token_id=? AND received_at_utc_ms <= ? AND condition_id=?
          AND COALESCE(source_timestamp_utc, received_at_utc_ms/1000.0) <= ?
        ORDER BY received_at_utc_ms DESC LIMIT 1""", (token_id, t * 1000.0, condition_id, t)).fetchone()
    if row is None or (t - row["ts"]) > tol:
        return None
    d = dict(row)
    d["age_s"] = t - row["ts"]
    return d


def walk_asks(conn, snapshot_id, target_usd=None, target_shares=None, slippage_ticks=0):
    """Camina los niveles ask reales. Devuelve dict(status FULL/PARTIAL/NO_DEPTH,
    shares, usd, vwap, n_levels). NO_DEPTH = el snapshot no trae niveles (WS
    price_change): no se sabe nada de profundidad, no se inventa."""
    levels = conn.execute(
        "SELECT price, size FROM orderbook_levels WHERE snapshot_id=? AND side='ask' ORDER BY level",
        (snapshot_id,)).fetchall()
    if not levels:
        return {"status": "NO_DEPTH", "shares": 0.0, "usd": 0.0, "vwap": None, "n_levels": 0}
    rem_usd = target_usd
    rem_sh = target_shares
    shares = usd = 0.0
    for lv in levels:
        price = float(lv["price"]) + slippage_ticks * TICK
        size = float(lv["size"])
        if price <= 0 or size <= 0:
            continue
        if rem_usd is not None:
            take_usd = min(rem_usd, price * size)
            take_sh = take_usd / price
            rem_usd -= take_usd
        else:
            take_sh = min(rem_sh, size)
            take_usd = take_sh * price
            rem_sh -= take_sh
        shares += take_sh
        usd += take_usd
        if (rem_usd is not None and rem_usd <= 1e-9) or (rem_sh is not None and rem_sh <= 1e-9):
            break
    full = (rem_usd is not None and rem_usd <= 1e-9) or (rem_sh is not None and rem_sh <= 1e-9)
    return {"status": "FULL" if full else "PARTIAL", "shares": shares, "usd": usd,
            "vwap": (usd / shares) if shares else None, "n_levels": len(levels)}


def total_depth(conn, snapshot_id, side="ask"):
    return scalar(conn, "SELECT COALESCE(SUM(size),0) FROM orderbook_levels WHERE snapshot_id=? AND side=?",
                  snapshot_id, side)


def nearest_underlying(conn, asset, t, tol, pad):
    """Lectura del subyacente disponible en t. `pad` compensa que el collector
    guarda received_at_utc como el instante en que LANZO la request (no cuando
    llego la respuesta, ver market_data_diagnostic.md): se exige
    received_at_utc <= t - pad."""
    row = conn.execute("""
        SELECT id, price, market_open_reference_price, distance_from_open_pct, received_at_utc, condition_id
        FROM underlying_prices WHERE asset_symbol=? AND received_at_utc <= ?
        ORDER BY received_at_utc DESC LIMIT 1""", (asset, t - pad)).fetchone()
    if row is None or (t - row["received_at_utc"]) > tol + pad:
        return None
    return dict(row)


# ==================================================== inventario ===
def reconstruct_market(ctx, market, fills):
    """Reconstruye el inventario del lider en UN mercado, fill a fill, BUY y
    SELL. Devuelve (sequence_row, classified_fills). Nunca usa el winner para
    clasificar; solo para el P&L final."""
    c = ctx.data
    fills = sorted(fills, key=lambda f: (f["ts"], 0 if f["origin"] == "HISTORY_V2" else 1, f["source_row_id"]))
    open_t, close_t = market.get("open_time_utc"), market.get("close_time_utc")
    tok = {"Up": market.get("token_up_id"), "Down": market.get("token_down_id")}
    winner = market.get("winner_at_cutoff")

    sh = {"Up": 0.0, "Down": 0.0}
    cost = {"Up": 0.0, "Down": 0.0}          # coste de las shares que se TIENEN (coste medio)
    buy_cost_total = {"Up": 0.0, "Down": 0.0}
    sold_sh = {"Up": 0.0, "Down": 0.0}
    proceeds = {"Up": 0.0, "Down": 0.0}
    realized_sell_pnl = 0.0
    oversell = 0
    tags = Counter()
    out_fills = []
    first = None
    first_opposite = None
    side_switches = 0
    prev_outcome = None
    same_second_ambiguous = 0
    prev_ts = None
    fees_est = 0.0
    n_ctx_exec = 0
    sim_pair_costs = []

    for f in fills:
        o, side, price, q = f["outcome"], f["side"], f["price"], f["shares"]
        if o not in ("Up", "Down") or price is None or q is None:
            tags["SKIPPED_INCOMPLETE"] += 1
            continue
        other = "Down" if o == "Up" else "Up"
        up_b, dn_b = sh["Up"], sh["Down"]
        matched_b = min(up_b, dn_b)
        surplus_side_b = "Up" if up_b > dn_b else ("Down" if dn_b > up_b else None)
        vwap_b = {s: (cost[s] / sh[s]) if sh[s] > 0 else None for s in ("Up", "Down")}

        if prev_ts is not None and f["ts"] == prev_ts and prev_outcome is not None and prev_outcome != o:
            same_second_ambiguous += 1
        prev_ts = f["ts"]

        # --- clasificacion causal (solo estado 'before' + el propio fill) ---
        if side == "SELL":
            tag = "SELL_REDUCE" if sh[o] > 0 else "SELL_WITHOUT_INVENTORY"
        elif up_b == 0 and dn_b == 0:
            tag = "FIRST_LEG"
        elif surplus_side_b is None:
            tag = "BALANCED_BASE_ADD"          # base empatada (>0): rompe el empate
        elif o == surplus_side_b:
            tag = "SURPLUS_ADD"                # compra del lado que ya tenia MAS
        else:
            tag = "IMBALANCE_REDUCING_BUY"     # compra del lado que tenia MENOS
        tags[tag] += 1

        # --- contexto de mercado en el instante del fill (anti-look-ahead) ---
        ctx_row = {}
        snap_own = nearest_snapshot(c, market["condition_id"], tok.get(o), f["ts"], ctx.tol)
        snap_oth = nearest_snapshot(c, market["condition_id"], tok.get(other), f["ts"], ctx.tol)
        under = nearest_underlying(c, market.get("asset_symbol"), f["ts"], ctx.tol, ctx.upad) if market.get("asset_symbol") else None
        if snap_own:
            w_own = walk_asks(c, snap_own["id"], target_shares=q)
            ctx_row.update(own_best_ask=snap_own["best_ask"], own_best_bid=snap_own["best_bid"],
                           own_exec_vwap_same_size=w_own["vwap"] if w_own["status"] == "FULL" else None,
                           own_exec_status=w_own["status"], own_snapshot_age_s=round(snap_own["age_s"], 3),
                           own_ask_depth_shares=total_depth(c, snap_own["id"], "ask") if w_own["status"] != "NO_DEPTH" else None)
        if snap_oth:
            w_oth = walk_asks(c, snap_oth["id"], target_shares=q)
            ctx_row.update(opp_best_ask=snap_oth["best_ask"], opp_exec_vwap_same_size=w_oth["vwap"] if w_oth["status"] == "FULL" else None,
                           opp_exec_status=w_oth["status"])
        if snap_own and snap_oth and ctx_row.get("own_exec_vwap_same_size") is not None and ctx_row.get("opp_exec_vwap_same_size") is not None:
            ctx_row["simultaneous_pair_cost_exec"] = ctx_row["own_exec_vwap_same_size"] + ctx_row["opp_exec_vwap_same_size"]
            n_ctx_exec += 1
            sim_pair_costs.append(ctx_row["simultaneous_pair_cost_exec"])
        if under:
            ctx_row.update(underlying_price=under["price"], underlying_dist_pct=under["distance_from_open_pct"])
        price_vs_ask = None
        if ctx_row.get("own_best_ask") is not None and side == "BUY":
            price_vs_ask = round(price - ctx_row["own_best_ask"], 4)

        # --- aplicar el fill ---
        if side == "BUY":
            sh[o] += q
            cost[o] += price * q
            buy_cost_total[o] += price * q
        elif side == "SELL":
            avail = sh[o]
            q_eff = min(q, avail)
            if q > avail + 1e-9:
                oversell += 1
            avg = (cost[o] / sh[o]) if sh[o] > 0 else 0.0
            realized_sell_pnl += (price - avg) * q_eff
            cost[o] -= avg * q_eff
            sh[o] -= q_eff
            sold_sh[o] += q
            proceeds[o] += price * q
        fees_est += taker_fee(q, price, ctx.fee_rate)

        if first is None:
            first = f
        elif first_opposite is None and o != first["outcome"] and side == "BUY":
            first_opposite = f
        if prev_outcome is not None and o != prev_outcome:
            side_switches += 1
        prev_outcome = o

        matched_a = min(sh["Up"], sh["Down"])
        out_fills.append({
            "fill_id": f["fill_id"], "origin": f["origin"], "source_table": f["source_table"],
            "source_row_id": f["source_row_id"], "tx": f["tx"], "ts": f["ts"], "ts_human": fmt(f["ts"]),
            "condition_id": market["condition_id"], "asset": market.get("asset_symbol"),
            "window_minutes": market.get("window_minutes"),
            "seconds_since_open": round(f["ts"] - open_t, 1) if open_t else None,
            "seconds_to_close": round(close_t - f["ts"], 1) if close_t else None,
            "outcome": o, "side": side, "price": price, "shares": q, "usd": round(price * q, 4),
            "tag": tag, "up_before": round(up_b, 4), "down_before": round(dn_b, 4),
            "matched_before": round(matched_b, 4), "surplus_side_before": surplus_side_b,
            "vwap_up_before": vwap_b["Up"], "vwap_down_before": vwap_b["Down"],
            "matched_after": round(matched_a, 4), "price_minus_best_ask": price_vs_ask,
            **ctx_row,
        })

    # --- estado final y P&L ---
    net = {"Up": sh["Up"], "Down": sh["Down"]}
    vwap = {s: (cost[s] / sh[s]) if sh[s] > 0 else None for s in ("Up", "Down")}
    matched = min(net["Up"], net["Down"])
    surplus_side = "Up" if net["Up"] > net["Down"] else ("Down" if net["Down"] > net["Up"] else None)
    surplus = abs(net["Up"] - net["Down"])
    coverage = (matched / max(net["Up"], net["Down"])) if max(net["Up"], net["Down"]) > 0 else None
    paired_pnl = matched * 1.0 - matched * ((vwap["Up"] or 0) + (vwap["Down"] or 0))
    total_buy_cost = buy_cost_total["Up"] + buy_cost_total["Down"]
    net_cost_held = cost["Up"] + cost["Down"]
    surplus_cost = surplus * (vwap[surplus_side] or 0) if surplus_side else 0.0
    portfolio_floor = matched * 1.0 - net_cost_held   # peor caso de la posicion que queda abierta
    if winner:
        payout = net[winner] * 1.0
        residual_pnl = (surplus * 1.0 if surplus_side == winner else 0.0) - surplus_cost
        gross_pnl = payout + proceeds["Up"] + proceeds["Down"] - total_buy_cost
        resolved = 1
    else:
        payout = residual_pnl = None
        gross_pnl = None
        resolved = 0
    identity_check = None
    if winner:
        identity_check = round(gross_pnl - (paired_pnl + residual_pnl + realized_sell_pnl), 6)

    seq = {
        "condition_id": market["condition_id"], "market_title": market.get("market_title"),
        "asset": market.get("asset_symbol"), "window_minutes": market.get("window_minutes"),
        "open_time_utc": fmt(open_t), "close_time_utc": fmt(close_t), "open_ts": open_t,
        "n_fills": len(out_fills), "n_buy": sum(1 for x in out_fills if x["side"] == "BUY"),
        "n_sell": sum(1 for x in out_fills if x["side"] == "SELL"),
        "n_fills_from_history": sum(1 for x in out_fills if x["origin"] == "HISTORY_V2"),
        "n_fills_from_live": sum(1 for x in out_fills if x["origin"] == "LIVE"),
        "same_second_opposite_side_ambiguities": same_second_ambiguous,
        "first_leg_side": first["outcome"] if first else None,
        "first_leg_ts": fmt(first["ts"]) if first else None,
        "first_leg_seconds_since_open": round(first["ts"] - open_t, 1) if (first and open_t) else None,
        "first_leg_price": first["price"] if first else None,
        "first_leg_shares": first["shares"] if first else None,
        "second_leg_ts": fmt(first_opposite["ts"]) if first_opposite else None,
        "seconds_between_legs": round(first_opposite["ts"] - first["ts"], 1) if (first and first_opposite) else None,
        "second_leg_price": first_opposite["price"] if first_opposite else None,
        "second_leg_shares": first_opposite["shares"] if first_opposite else None,
        "n_side_switches": side_switches,
        "net_up_shares": round(net["Up"], 4), "net_down_shares": round(net["Down"], 4),
        "buy_cost_up": round(buy_cost_total["Up"], 4), "buy_cost_down": round(buy_cost_total["Down"], 4),
        "sold_up_shares": round(sold_sh["Up"], 4), "sold_down_shares": round(sold_sh["Down"], 4),
        "sell_proceeds": round(proceeds["Up"] + proceeds["Down"], 4),
        "oversell_events": oversell,
        "vwap_up": round(vwap["Up"], 4) if vwap["Up"] is not None else None,
        "vwap_down": round(vwap["Down"], 4) if vwap["Down"] is not None else None,
        "combined_inventory_vwap": round((vwap["Up"] or 0) + (vwap["Down"] or 0), 4) if (vwap["Up"] and vwap["Down"]) else None,
        "matched_shares": round(matched, 4), "surplus_side": surplus_side, "surplus_shares": round(surplus, 4),
        "coverage_ratio": round(coverage, 4) if coverage is not None else None,
        "n_fills_with_exec_pair_context": n_ctx_exec,
        "median_simultaneous_pair_cost_exec": round(quantiles(sim_pair_costs)[0.5], 4) if sim_pair_costs else None,
        "min_simultaneous_pair_cost_exec": round(min(sim_pair_costs), 4) if sim_pair_costs else None,
        "underlying_dist_pct_at_first_leg": out_fills[0].get("underlying_dist_pct") if out_fills else None,
        "spread_own_at_first_leg": (round(out_fills[0]["own_best_ask"] - out_fills[0]["own_best_bid"], 4)
                                    if out_fills and out_fills[0].get("own_best_ask") is not None and out_fills[0].get("own_best_bid") is not None else None),
        "resolved_at_cutoff": resolved, "winner": winner,
        "paired_pnl_vwap_attrib": round(paired_pnl, 4),
        "portfolio_floor_pnl": round(portfolio_floor, 4),
        "residual_pnl": round(residual_pnl, 4) if residual_pnl is not None else None,
        "realized_sell_pnl": round(realized_sell_pnl, 4),
        "resolution_payout": round(payout, 4) if payout is not None else None,
        "gross_pnl": round(gross_pnl, 4) if gross_pnl is not None else None,
        "pending_surplus_exposure_usd": round(surplus_cost, 4) if not winner else None,
        "est_taker_fees": round(fees_est, 4),
        "net_pnl_if_taker": round(gross_pnl - fees_est, 4) if gross_pnl is not None else None,
        "pnl_identity_residual": identity_check,
        "tags_json": json.dumps(dict(tags), sort_keys=True),
    }
    return seq, out_fills


# ================================================ hipotesis / reglas ===
def hypothesis_tests(seqs, fills_by_market, ctx):
    """Cada fila: hipotesis, n, estadistico observado, baseline, IC 95%, p-valor,
    veredicto. Nunca se afirma intencion: se describe frecuencia vs azar."""
    H = []

    def add(name, k, n, group="ALL", detail="", null_p=0.5):
        """null_p=0.5 solo cuando 50/50 es un nulo NATURAL (barato vs caro, con
        vs contra momentum, reduce vs ensancha). Para el resto (null_p=None)
        se reporta la tasa con IC de Wilson y se etiqueta DESCRIPTIVE: no hay
        un azar de referencia contra el que 'ganar'."""
        if n == 0:
            H.append({"hypothesis": name, "group": group, "n": 0, "observed_rate": None, "baseline": null_p,
                       "ci95_low": None, "ci95_high": None, "p_value_vs_baseline": None,
                       "verdict": "NO DATA", "detail": detail})
            return
        p = k / n
        lo, hi = wilson_ci(k, n)
        if null_p is None:
            H.append({"hypothesis": name, "group": group, "n": n, "observed_rate": round(p, 4), "baseline": None,
                       "ci95_low": lo, "ci95_high": hi, "p_value_vs_baseline": None,
                       "verdict": "DESCRIPTIVE (sin nulo 50/50 natural)", "detail": detail})
            return
        pv = binom_two_sided_p(k, n, null_p)
        verdict = ("DISTINGUISHABLE FROM 50/50" if (pv is not None and pv < 0.01 and n >= 30)
                   else "WEAK EVIDENCE" if (pv is not None and pv < 0.05) else "NOT DISTINGUISHABLE FROM 50/50")
        H.append({"hypothesis": name, "group": group, "n": n, "observed_rate": round(p, 4), "baseline": null_p,
                   "ci95_low": lo, "ci95_high": hi, "p_value_vs_baseline": pv, "verdict": verdict, "detail": detail})

    first_fills = []
    for s in seqs:
        fl = fills_by_market.get(s["condition_id"], [])
        if fl and fl[0]["tag"] == "FIRST_LEG":
            first_fills.append((s, fl[0]))

    groups = {"ALL": first_fills}
    for a in ASSETS:
        groups[a] = [(s, f) for s, f in first_fills if s["asset"] == a]

    for g, items in groups.items():
        # 1) primera pierna = lado mas barato (por best ask propio vs contrario)
        k = n = 0
        for s, f in items:
            oa, xa = f.get("own_best_ask"), f.get("opp_best_ask")
            if oa is not None and xa is not None and oa != xa:
                n += 1
                k += 1 if oa < xa else 0
        add("H1_first_leg_is_cheaper_side", k, n, g, "own best_ask < opposite best_ask en el instante del fill")
        # 2) primera pierna = favorito (lado mas caro)
        add("H2_first_leg_is_favorite", n - k, n, g, "complemento de H1")
        # 3) momentum: signo de la distancia del subyacente alineado con el lado
        for thr in (0.0, 0.02, 0.05):
            k = n = 0
            for s, f in items:
                d = f.get("underlying_dist_pct")
                if d is None or abs(d) < thr or d == 0:
                    continue
                n += 1
                k += 1 if ((d > 0 and f["outcome"] == "Up") or (d < 0 and f["outcome"] == "Down")) else 0
            add(f"H3_first_leg_with_momentum_absdist_ge_{thr}", k, n, g, "lado == signo(distancia subyacente vs open)")
        # 4) contra-momentum es el complemento de H3 (no se duplica)
        # 5) acumula pares: mercados con ambos lados > 0 al final
        n = len([s for s, _ in items])
        k = sum(1 for s, _ in items if s["net_up_shares"] > 0 and s["net_down_shares"] > 0)
        add("H5_builds_both_sides", k, n, g, "mercados con net_up>0 y net_down>0 al ultimo fill", null_p=None)
        # 6) coverage >= 0.9 al final
        k = sum(1 for s, _ in items if (s["coverage_ratio"] or 0) >= 0.9)
        add("H6_ends_balanced_cov_ge_0.9", k, n, g, "coverage_ratio final >= 0.9", null_p=None)
        # 7) exposicion direccional relevante al final (coverage < 0.5 o un solo lado)
        k = sum(1 for s, _ in items if (s["coverage_ratio"] is None) or s["coverage_ratio"] < 0.5)
        add("H7_ends_directional_cov_lt_0.5", k, n, g, "coverage_ratio final < 0.5 o un solo lado", null_p=None)
        # 8) entre fills posteriores: reduce desbalance vs lo ensancha
        k = n = 0
        for s, _ in items:
            for f in fills_by_market.get(s["condition_id"], [])[1:]:
                if f["tag"] in ("IMBALANCE_REDUCING_BUY", "SURPLUS_ADD"):
                    n += 1
                    k += 1 if f["tag"] == "IMBALANCE_REDUCING_BUY" else 0
        add("H8_subsequent_buys_reduce_imbalance", k, n, g, "IMBALANCE_REDUCING_BUY / (IMBALANCE_REDUCING_BUY+SURPLUS_ADD)")
        # 9) compra la primera pierna barata en precio absoluto (< 0.5)
        k = sum(1 for s, f in items if f["price"] is not None and f["price"] < 0.5)
        n = sum(1 for s, f in items if f["price"] is not None)
        add("H9_first_leg_price_below_0.5", k, n, g, "precio de la primera pierna < 0.50", null_p=None)
        # 10) ¿usa SELL?
        k = sum(1 for s, _ in items if s["n_sell"] > 0)
        add("H10_markets_with_any_sell", k, len(items), g, "mercados con >=1 SELL", null_p=None)
        # 11) coste simultaneo ejecutable < 1 al armar par (cuando hay contexto ejecutable)
        k = n = 0
        for s, _ in items:
            for f in fills_by_market.get(s["condition_id"], []):
                v = f.get("simultaneous_pair_cost_exec")
                if v is not None and f["tag"] in ("IMBALANCE_REDUCING_BUY", "BALANCED_BASE_ADD"):
                    n += 1
                    k += 1 if v < 1.0 else 0
        add("H11_pair_completion_at_exec_cost_lt_1", k, n, g, "coste simultaneo ejecutable (ambas piernas, mismo tamano) < 1.0 en compras que reducen desbalance", null_p=None)

    # timing / sizing (descriptivos, sin baseline binomial)
    def desc(name, values, detail):
        q = quantiles(values)
        H.append({"hypothesis": name, "group": "ALL", "n": len([v for v in values if v is not None]),
                   "observed_rate": None, "baseline": None, "ci95_low": q[0.25], "ci95_high": q[0.75],
                   "p_value_vs_baseline": None, "verdict": f"median={q[0.5]} p10={q[0.1]} p90={q[0.9]}", "detail": detail})
    desc("T1_first_leg_seconds_since_open", [s["first_leg_seconds_since_open"] for s in seqs], "cuantiles (ci95 cols = p25/p75)")
    desc("T2_seconds_between_legs", [s["seconds_between_legs"] for s in seqs], "primera compra del lado contrario")
    desc("T3_first_leg_shares", [s["first_leg_shares"] for s in seqs], "sizing primera pierna")
    desc("T4_fills_per_market", [s["n_fills"] for s in seqs], "")
    desc("T5_final_coverage_ratio", [s["coverage_ratio"] for s in seqs], "")
    # sizing correlations
    all_f = [f for fl in fills_by_market.values() for f in fl if f["side"] == "BUY"]
    xs = [abs(f["up_before"] - f["down_before"]) for f in all_f]
    H.append({"hypothesis": "S1_spearman_shares_vs_imbalance_before", "group": "ALL", "n": len(all_f), "observed_rate": spearman(xs, [f["shares"] for f in all_f]),
               "baseline": 0.0, "ci95_low": None, "ci95_high": None, "p_value_vs_baseline": None, "verdict": "rho", "detail": "tamano del fill vs |up-down| antes"})
    xs2 = [f["seconds_to_close"] for f in all_f if f["seconds_to_close"] is not None]
    ys2 = [f["shares"] for f in all_f if f["seconds_to_close"] is not None]
    H.append({"hypothesis": "S2_spearman_shares_vs_seconds_to_close", "group": "ALL", "n": len(xs2), "observed_rate": spearman(xs2, ys2),
               "baseline": 0.0, "ci95_low": None, "ci95_high": None, "p_value_vs_baseline": None, "verdict": "rho", "detail": ""})
    H.append({"hypothesis": "S3_spearman_shares_vs_price", "group": "ALL", "n": len(all_f), "observed_rate": spearman([f["price"] for f in all_f], [f["shares"] for f in all_f]),
               "baseline": 0.0, "ci95_low": None, "ci95_high": None, "p_value_vs_baseline": None, "verdict": "rho", "detail": ""})
    # regimen de volatilidad: |dist| terciles en la primera pierna -> alineacion momentum
    dists = sorted(abs(f["underlying_dist_pct"]) for s, f in first_fills if f.get("underlying_dist_pct") is not None)
    if len(dists) >= 30:
        t1, t2 = dists[len(dists) // 3], dists[2 * len(dists) // 3]
        for label, lo, hi in (("low", 0, t1), ("mid", t1, t2), ("high", t2, float("inf"))):
            k = n = 0
            for s, f in first_fills:
                d = f.get("underlying_dist_pct")
                if d is None or d == 0 or not (lo <= abs(d) < hi):
                    continue
                n += 1
                k += 1 if ((d > 0 and f["outcome"] == "Up") or (d < 0 and f["outcome"] == "Down")) else 0
            add(f"R1_momentum_alignment_vol_regime_{label}", k, n, "ALL", f"|dist| en [{lo:.4f},{hi if hi != float('inf') else 'inf'})")
    return H


def fit_tree(rows_, features, target, max_depth=2, min_leaf=20):
    """Arbol de decision minusculo (Gini, umbrales numericos) en stdlib, para
    reglas interpretables. rows_: list[dict]; target: nombre de columna 0/1."""
    def gini(ys):
        n = len(ys)
        if not n:
            return 0.0
        p = sum(ys) / n
        return 2 * p * (1 - p)

    def best_split(sub):
        base = gini([r[target] for r in sub])
        best = None
        for feat in features:
            vals = sorted({r[feat] for r in sub if r[feat] is not None})
            if len(vals) < 2:
                continue
            cands = vals[:: max(1, len(vals) // 20)]
            for i in range(len(cands) - 1):
                thr = (cands[i] + cands[i + 1]) / 2
                left = [r for r in sub if r[feat] is not None and r[feat] <= thr]
                right = [r for r in sub if r[feat] is not None and r[feat] > thr]
                if len(left) < min_leaf or len(right) < min_leaf:
                    continue
                g = (len(left) * gini([r[target] for r in left]) + len(right) * gini([r[target] for r in right])) / (len(left) + len(right))
                gain = base - g
                if best is None or gain > best[0]:
                    best = (gain, feat, thr)
        return best

    def build(sub, depth):
        ys = [r[target] for r in sub]
        node = {"n": len(sub), "p1": round(sum(ys) / len(ys), 4) if ys else None}
        if depth >= max_depth or len(sub) < 2 * min_leaf:
            return node
        sp = best_split(sub)
        if sp is None or sp[0] <= 1e-6:
            return node
        gain, feat, thr = sp
        node.update(feature=feat, threshold=round(thr, 6), gain=round(gain, 6))
        node["left"] = build([r for r in sub if r[feat] is not None and r[feat] <= thr], depth + 1)
        node["right"] = build([r for r in sub if r[feat] is not None and r[feat] > thr], depth + 1)
        return node

    return build([r for r in rows_ if r[target] is not None], 0)


def tree_predict(node, r):
    while "feature" in node:
        v = r.get(node["feature"])
        if v is None:
            break
        node = node["left"] if v <= node["threshold"] else node["right"]
    return 1 if (node["p1"] or 0) >= 0.5 else 0


def tree_rules(node, prefix=""):
    if "feature" not in node:
        return [f"{prefix or 'ROOT'} -> P(target=1)={node['p1']} (n={node['n']})"]
    out = []
    out += tree_rules(node["left"], f"{prefix}{' AND ' if prefix else ''}{node['feature']}<={node['threshold']}")
    out += tree_rules(node["right"], f"{prefix}{' AND ' if prefix else ''}{node['feature']}>{node['threshold']}")
    return out


def interpretable_rules(seqs, fills_by_market):
    """Arbol depth-2 sobre la eleccion de la PRIMERA pierna (Up=1) con
    features disponibles en ese instante. Split cronologico 70/30 por mercado;
    se reporta accuracy en TEST vs mayoria vs 50%."""
    data = []
    for s in sorted(seqs, key=lambda s: s["open_ts"] or 0):
        fl = fills_by_market.get(s["condition_id"], [])
        if not fl or fl[0]["tag"] != "FIRST_LEG":
            continue
        f = fl[0]
        oa, xa = f.get("own_best_ask"), f.get("opp_best_ask")
        up_ask = oa if f["outcome"] == "Up" else xa
        dn_ask = xa if f["outcome"] == "Up" else oa
        data.append({
            "target_up": 1 if f["outcome"] == "Up" else 0,
            "underlying_dist_pct": f.get("underlying_dist_pct"),
            "up_ask_minus_down_ask": (up_ask - dn_ask) if (up_ask is not None and dn_ask is not None) else None,
            "seconds_since_open": f.get("seconds_since_open"),
            "spread_own": (f["own_best_ask"] - f["own_best_bid"]) if (f.get("own_best_ask") is not None and f.get("own_best_bid") is not None) else None,
        })
    n = len(data)
    if n < 60:
        return {"n": n, "note": "muestra insuficiente para arbol (<60 primeras piernas con contexto)", "rules": [], "acc_test": None}
    cut = int(n * 0.7)
    train, test = data[:cut], data[cut:]
    feats = ["underlying_dist_pct", "up_ask_minus_down_ask", "seconds_since_open", "spread_own"]
    tree = fit_tree(train, feats, "target_up", max_depth=2, min_leaf=max(15, n // 20))
    maj = 1 if sum(r["target_up"] for r in train) * 2 >= len(train) else 0
    acc_test = sum(1 for r in test if tree_predict(tree, r) == r["target_up"]) / len(test) if test else None
    acc_maj = sum(1 for r in test if maj == r["target_up"]) / len(test) if test else None
    return {"n": n, "n_train": len(train), "n_test": len(test), "tree": tree, "rules": tree_rules(tree),
            "acc_test": round(acc_test, 4) if acc_test is not None else None,
            "acc_majority_test": round(acc_maj, 4) if acc_maj is not None else None,
            "note": "features observables en el instante de la 1ra pierna; target = lado Up"}


# ===================================================== candidatas ===
def load_universe(ctx):
    """Universo COMPLETO: BTC/ETH/SOL 5min con winner conocido al corte y
    ventana cerrada antes del corte. NO filtra por 'mercados que opero el lider'."""
    ms = rows(ctx.data, """
        SELECT condition_id, market_title, asset_symbol, token_up_id, token_down_id, open_time_utc,
               close_time_utc, winner, resolution_time_utc, window_minutes
        FROM markets
        WHERE upper(asset_symbol) IN ('BTC','ETH','SOL') AND window_minutes=5
          AND upper(winner) IN ('UP','DOWN') AND open_time_utc IS NOT NULL AND close_time_utc IS NOT NULL
          AND close_time_utc <= ? AND (resolution_time_utc IS NULL OR resolution_time_utc <= ?)
        ORDER BY open_time_utc""", ctx.cutoff, ctx.cutoff)
    for m in ms:
        m["asset_symbol"] = m["asset_symbol"].upper()
        m["winner"] = m["winner"].capitalize()
    return ms


def split_chrono(ms, fr=(0.5, 0.25, 0.25)):
    n = len(ms)
    a = int(n * fr[0])
    b = int(n * (fr[0] + fr[1]))
    return ms[:a], ms[a:b], ms[b:]


def _decision_state(ctx, m):
    c = ctx.data
    t = m["open_time_utc"] + ctx.decision_offset
    su = nearest_snapshot(c, m["condition_id"], m["token_up_id"], t, ctx.tol)
    sd = nearest_snapshot(c, m["condition_id"], m["token_down_id"], t, ctx.tol)
    un = nearest_underlying(c, m["asset_symbol"], t, ctx.tol, ctx.upad)
    return {"t": t, "snap_up": su, "snap_down": sd, "under": un}


def _mid(s):
    if s is None or s["best_ask"] is None:
        return None
    return (s["best_bid"] + s["best_ask"]) / 2 if s["best_bid"] is not None else s["best_ask"]


CANDIDATE_SELECTORS = {}


def selector(name):
    def deco(fn):
        CANDIDATE_SELECTORS[name] = fn
        return fn
    return deco


@selector("NO_TRADE")
def sel_none(st, params):
    return None


@selector("FAVORITE")
def sel_fav(st, params):
    mu, md = _mid(st["snap_up"]), _mid(st["snap_down"])
    if mu is None or md is None or mu == md:
        return None
    return "Up" if mu > md else "Down"


@selector("UNDERDOG")
def sel_dog(st, params):
    mu, md = _mid(st["snap_up"]), _mid(st["snap_down"])
    if mu is None or md is None or mu == md:
        return None
    return "Down" if mu > md else "Up"


@selector("MOMENTUM")
def sel_mom(st, params):
    u = st["under"]
    if u is None or u["distance_from_open_pct"] is None:
        return None
    d = u["distance_from_open_pct"]
    if abs(d) < params.get("threshold_pct", 0.02) or d == 0:
        return None
    return "Up" if d > 0 else "Down"


@selector("CONTRA_MOMENTUM")
def sel_contra(st, params):
    s = sel_mom(st, params)
    return None if s is None else ("Down" if s == "Up" else "Up")


def simulate_candidate(ctx, markets, name, params, pair_threshold=None, partial_hedge=False, slippage_ticks=0):
    """Motor unico para todas las candidatas: 1ra pierna a open+offset con
    $stake caminando profundidad; opcional: completar par (pair_threshold) o
    cobertura parcial (reglas congeladas del paper validator). Fees por fill.
    Devuelve una lista de resultados por mercado (todos los mercados, incl.
    NO_SIGNAL/NO_LIQUIDITY, para el embudo)."""
    c = ctx.data
    sel = CANDIDATE_SELECTORS[name]
    out = []
    for m in markets:
        st = _decision_state(ctx, m)
        base = {"condition_id": m["condition_id"], "asset": m["asset_symbol"], "open_ts": m["open_time_utc"],
                "day": day_utc(m["open_time_utc"]), "winner": m["winner"], "candidate": None}
        if st["t"] >= m["close_time_utc"] - 60:
            out.append({**base, "status": "WINDOW_TOO_SHORT"}); continue
        side = sel(st, params)
        if side is None:
            out.append({**base, "status": "NO_SIGNAL"}); continue
        snap = st["snap_up"] if side == "Up" else st["snap_down"]
        if snap is None:
            out.append({**base, "status": "NO_BOOK", "side": side}); continue
        w = walk_asks(c, snap["id"], target_usd=ctx.stake, slippage_ticks=slippage_ticks)
        if w["status"] == "NO_DEPTH" or w["shares"] <= 0:
            out.append({**base, "status": "NO_LIQUIDITY", "side": side}); continue
        leg1 = w
        fees = taker_fee(leg1["shares"], leg1["vwap"], ctx.fee_rate)
        capital = leg1["usd"]
        other = "Down" if side == "Up" else "Up"
        other_tok = m["token_down_id"] if side == "Up" else m["token_up_id"]
        matched = 0.0
        leg2 = None
        completed_at = None
        if pair_threshold is not None or partial_hedge:
            t = st["t"] + 5
            close_limit = m["close_time_utc"] - 60
            target_sh = leg1["shares"] * (0.25 if partial_hedge else 1.0)
            while t <= close_limit:
                so = nearest_snapshot(c, m["condition_id"], other_tok, t, ctx.tol)
                if so is not None and so["best_ask"] is not None:
                    quick = leg1["vwap"] + so["best_ask"]
                    thr = 0.90 if partial_hedge else pair_threshold
                    if quick <= thr:
                        w2 = walk_asks(c, so["id"], target_shares=target_sh, slippage_ticks=slippage_ticks)
                        if w2["status"] == "FULL" and (leg1["vwap"] + w2["vwap"]) <= thr and (not partial_hedge or w2["vwap"] <= 0.10):
                            leg2 = w2
                            completed_at = t
                            break
                t += 5
        if leg2:
            matched = min(leg1["shares"], leg2["shares"])
            capital += leg2["usd"]
            fees += taker_fee(leg2["shares"], leg2["vwap"], ctx.fee_rate)
        payout = (leg1["shares"] if side == m["winner"] else 0.0) + ((leg2["shares"] if other == m["winner"] else 0.0) if leg2 else 0.0)
        gross = payout - capital
        out.append({**base, "status": "TRADED", "side": side, "leg1_shares": leg1["shares"], "leg1_vwap": leg1["vwap"],
                    "leg1_status": leg1["status"], "capital": capital, "gross_pnl": gross, "fees": fees,
                    "net_pnl": gross - fees, "paired": leg2 is not None, "matched_shares": matched,
                    "seconds_to_complete": (completed_at - st["t"]) if completed_at else None,
                    "won": 1 if gross > 0 else 0})
    return out


def aggregate(results, label, ctx, bootstrap=True):
    traded = [r for r in results if r["status"] == "TRADED"]
    n = len(traded)
    funnel = Counter(r["status"] for r in results)
    agg = {"candidate": label, "n_markets_in_split": len(results), "n_traded": n,
           "funnel": json.dumps(dict(funnel), sort_keys=True)}
    if not n:
        agg.update(win_rate=None, capital_usd=0.0, gross_pnl_usd=0.0, gross_roi_pct=None, est_fees_usd=0.0,
                   net_pnl_usd=0.0, net_roi_pct=None, max_drawdown_gross_usd=0.0, net_roi_ci95_low=None,
                   net_roi_ci95_high=None, n_paired=0)
        return agg
    cap = sum(r["capital"] for r in traded)
    gp = sum(r["gross_pnl"] for r in traded)
    fe = sum(r["fees"] for r in traded)
    ordered = sorted(traded, key=lambda r: r["open_ts"])
    agg.update(win_rate=round(sum(r["won"] for r in traded) / n, 4), capital_usd=round(cap, 2),
               gross_pnl_usd=round(gp, 2), gross_roi_pct=round(gp / cap * 100, 2) if cap else None,
               est_fees_usd=round(fe, 4), net_pnl_usd=round(gp - fe, 2),
               net_roi_pct=round((gp - fe) / cap * 100, 2) if cap else None,
               max_drawdown_gross_usd=round(drawdown([r["gross_pnl"] for r in ordered]), 2),
               n_paired=sum(1 for r in traded if r["paired"]))
    if bootstrap and n >= 10:
        B = ctx.args.bootstrap
        vals = []
        for _ in range(B):
            samp = [traded[ctx.rng.randrange(n)] for _ in range(n)]
            cs = sum(r["capital"] for r in samp)
            vals.append(sum(r["net_pnl"] for r in samp) / cs * 100 if cs else 0.0)
        vals.sort()
        agg["net_roi_ci95_low"] = round(vals[int(0.025 * B)], 2)
        agg["net_roi_ci95_high"] = round(vals[min(B - 1, int(0.975 * B))], 2)
    else:
        agg["net_roi_ci95_low"] = agg["net_roi_ci95_high"] = None
    return agg


def by_asset_and_day(results, label, leader_cids=frozenset()):
    """Desglose por asset, por dia y por 'mercado operado por el lider si/no'
    -- este ultimo expone directamente el sesgo de seleccion que tendria un
    analisis restringido a los mercados del lider."""
    out = []
    for key_name, key_fn in (("asset", lambda r: r["asset"]), ("day", lambda r: r["day"]),
                             ("leader_traded_market", lambda r: "yes" if r["condition_id"] in leader_cids else "no")):
        groups = defaultdict(list)
        for r in results:
            if r["status"] == "TRADED":
                groups[key_fn(r)].append(r)
        for k, rs in sorted(groups.items()):
            cap = sum(r["capital"] for r in rs)
            gp = sum(r["gross_pnl"] for r in rs)
            fe = sum(r["fees"] for r in rs)
            out.append({"candidate": label, "group_type": key_name, "group": k, "n_traded": len(rs),
                        "win_rate": round(sum(r["won"] for r in rs) / len(rs), 4), "capital_usd": round(cap, 2),
                        "gross_pnl_usd": round(gp, 2), "net_pnl_usd": round(gp - fe, 2),
                        "net_roi_pct": round((gp - fe) / cap * 100, 2) if cap else None})
    return out


def run_candidates(ctx, universe, leader_cids=frozenset()):
    """TRAIN se usa solo para calibrar; VALIDATION para ELEGIR entre
    calibraciones; TEST se evalua UNA vez por candidata. Se registra cuantas
    candidatas se probaron (para leer los p-valores/IC con esa multiplicidad).
    Completar par exige fill FULL de la 2da pierna (mas estricto que V2, que
    aceptaba PARTIAL)."""
    train, val, test = split_chrono(universe)
    ctx.notes["split"] = {"train": (len(train), fmt(train[0]["open_time_utc"]) if train else None, fmt(train[-1]["open_time_utc"]) if train else None),
                          "validation": (len(val), fmt(val[0]["open_time_utc"]) if val else None, fmt(val[-1]["open_time_utc"]) if val else None),
                          "test": (len(test), fmt(test[0]["open_time_utc"]) if test else None, fmt(test[-1]["open_time_utc"]) if test else None)}
    summary, detail, calib = [], [], []

    def evaluate(label, name, params, **kw):
        for split_name, ms in (("TRAIN", train), ("VALIDATION", val), ("TEST", test)):
            res = simulate_candidate(ctx, ms, name, params, **kw)
            agg = aggregate(res, label, ctx, bootstrap=(split_name == "TEST"))
            agg["split"] = split_name
            agg["params"] = json.dumps(params, sort_keys=True)
            summary.append(agg)
            if split_name == "TEST":
                detail.extend(by_asset_and_day(res, label, leader_cids))

    # baselines fijas (sin calibracion)
    evaluate("NO_TRADE", "NO_TRADE", {})
    evaluate("FAVORITE_1LEG", "FAVORITE", {})
    evaluate("UNDERDOG_1LEG", "UNDERDOG", {})
    evaluate("MOMENTUM_1LEG_T0.02_frozenV1", "MOMENTUM", {"threshold_pct": 0.02})
    evaluate("CONTRA_MOMENTUM_1LEG_T0.02", "CONTRA_MOMENTUM", {"threshold_pct": 0.02})
    evaluate("MOMENTUM_PARTIAL_HEDGE_paper_rules", "MOMENTUM", {"threshold_pct": 0.02}, partial_hedge=True)

    # momentum: umbral calibrado en TRAIN, elegido por net ROI en VALIDATION, evaluado en TEST
    best = None
    for thr in (0.0, 0.01, 0.02, 0.05, 0.10, 0.20):
        r_tr = aggregate(simulate_candidate(ctx, train, "MOMENTUM", {"threshold_pct": thr}), f"cal_T{thr}", ctx, bootstrap=False)
        r_va = aggregate(simulate_candidate(ctx, val, "MOMENTUM", {"threshold_pct": thr}), f"cal_T{thr}", ctx, bootstrap=False)
        calib.append({"family": "MOMENTUM_threshold", "param": thr, "train_n": r_tr["n_traded"], "train_net_roi": r_tr["net_roi_pct"],
                      "val_n": r_va["n_traded"], "val_net_roi": r_va["net_roi_pct"]})
        if r_va["n_traded"] >= 20 and r_va["net_roi_pct"] is not None and (best is None or r_va["net_roi_pct"] > best[1]):
            best = (thr, r_va["net_roi_pct"])
    if best:
        evaluate(f"MOMENTUM_1LEG_T{best[0]}_selected_on_VALIDATION", "MOMENTUM", {"threshold_pct": best[0]})

    # construccion de pares: umbral de coste combinado calibrado igual
    best = None
    for thr in (0.97, 0.98, 0.99, 1.00):
        r_va = aggregate(simulate_candidate(ctx, val, "UNDERDOG", {}, pair_threshold=thr), f"pair{thr}", ctx, bootstrap=False)
        r_tr = aggregate(simulate_candidate(ctx, train, "UNDERDOG", {}, pair_threshold=thr), f"pair{thr}", ctx, bootstrap=False)
        calib.append({"family": "PAIR_threshold(underdog_first)", "param": thr, "train_n": r_tr["n_traded"], "train_net_roi": r_tr["net_roi_pct"],
                      "val_n": r_va["n_traded"], "val_net_roi": r_va["net_roi_pct"]})
        if r_va["n_traded"] >= 20 and r_va["net_roi_pct"] is not None and (best is None or r_va["net_roi_pct"] > best[1]):
            best = (thr, r_va["net_roi_pct"])
    if best:
        evaluate(f"PAIR_BUILD_underdog_first_thr{best[0]}_selected_on_VALIDATION", "UNDERDOG", {}, pair_threshold=best[0])
    evaluate("PAIR_BUILD_underdog_first_thr0.99_fixed", "UNDERDOG", {}, pair_threshold=0.99)
    evaluate("PAIR_BUILD_momentum_first_thr0.99_fixed", "MOMENTUM", {"threshold_pct": 0.02}, pair_threshold=0.99)

    # sensibilidad a slippage: 1 tick en TEST para las 1-leg
    for label, name, params in (("FAVORITE_1LEG", "FAVORITE", {}), ("UNDERDOG_1LEG", "UNDERDOG", {}),
                                ("MOMENTUM_1LEG_T0.02_frozenV1", "MOMENTUM", {"threshold_pct": 0.02})):
        res = simulate_candidate(ctx, test, name, params, slippage_ticks=1)
        agg = aggregate(res, label + "_slip1tick", ctx, bootstrap=False)
        agg["split"] = "TEST"; agg["params"] = json.dumps({**params, "slippage_ticks": 1}, sort_keys=True)
        summary.append(agg)

    ctx.notes["n_candidate_evaluations_on_TEST"] = sum(1 for s in summary if s["split"] == "TEST")
    return summary, detail, calib


# ================================================ paper (forward) ===
def paper_forward(ctx):
    p = ctx.paper
    if p is None or not table_exists(p, "paper_decisions"):
        return None
    cols = columns_of(p, "paper_markets")
    cohort_filter = "AND pm.validation_cohort='OFFICIAL'" if "validation_cohort" in cols else ""
    res = rows(p, f"""
        SELECT pr.strategy, pr.condition_id, pr.winner, pr.resolved_at_utc, pr.capital_deployed_usd, pr.pnl_usd,
               pm.asset_symbol, pd.shares, pd.executable_price, pd.id AS decision_id
        FROM paper_resolutions pr
        JOIN paper_markets pm ON pm.condition_id = pr.condition_id
        JOIN paper_decisions pd ON pd.id = pr.decision_id
        WHERE pr.resolved_at_utc <= ? {cohort_filter}""", ctx.cutoff)
    hedges = {r["decision_id"]: r for r in rows(p, "SELECT decision_id, filled_shares, vwap FROM paper_hedge_fills WHERE executed_at_utc <= ?", ctx.cutoff)}
    n_checks = scalar(p, "SELECT count(*) FROM paper_hedge_checks WHERE checked_at_utc <= ?", ctx.cutoff) if table_exists(p, "paper_hedge_checks") else None
    out = []
    by_s = defaultdict(list)
    for r in res:
        by_s[r["strategy"]].append(r)
    for s, rs in sorted(by_s.items()):
        cap = sum(r["capital_deployed_usd"] for r in rs)
        gp = sum(r["pnl_usd"] for r in rs)
        fe = 0.0
        for r in rs:
            fe += taker_fee(r["shares"], r["executable_price"], ctx.fee_rate)
            h = hedges.get(r["decision_id"])
            if h:
                fe += taker_fee(h["filled_shares"], h["vwap"], ctx.fee_rate)
        cids_common = None
        out.append({"strategy": s, "n_resolved": len(rs), "win_rate": round(sum(1 for r in rs if r["pnl_usd"] > 0) / len(rs), 4),
                    "capital_usd": round(cap, 2), "gross_pnl_usd": round(gp, 2), "gross_roi_pct": round(gp / cap * 100, 2) if cap else None,
                    "est_fees_usd": round(fe, 4), "net_roi_pct": round((gp - fe) / cap * 100, 2) if cap else None,
                    "max_drawdown_usd": round(drawdown([r["pnl_usd"] for r in sorted(rs, key=lambda r: r["resolved_at_utc"])]), 2)})
    # mismos condition_id: momentum vs favorite
    mom = {r["condition_id"]: r for r in by_s.get("MOMENTUM_PURE", [])}
    fav = {r["condition_id"]: r for r in by_s.get("POLYMARKET_FAVORITE_BASELINE", [])}
    common = set(mom) & set(fav)
    same = []
    for label, d in (("MOMENTUM_PURE", mom), ("POLYMARKET_FAVORITE_BASELINE", fav)):
        rs = [d[c] for c in common]
        cap = sum(r["capital_deployed_usd"] for r in rs)
        gp = sum(r["pnl_usd"] for r in rs)
        same.append({"comparison": "same_condition_ids", "strategy": label, "n_common": len(common),
                     "gross_pnl_usd": round(gp, 2), "gross_roi_pct": round(gp / cap * 100, 2) if cap else None})
    return {"summary": out, "same_markets": same, "n_hedge_checks_total_COUNT_ONLY": n_checks, "n_hedge_fills": len(hedges)}


# ================================================== data map / DQ ===
def data_map(ctx, fill_stats):
    c = ctx.data
    tables = [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    info = []
    for t in tables:
        n = scalar(c, f"SELECT count(*) FROM {t}")
        info.append({"db": "data.db", "table": t, "rows": n, "columns": ", ".join(columns_of(c, t))})
    if ctx.paper:
        for r in ctx.paper.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            t = r["name"]
            info.append({"db": "paper_validation.db", "table": t, "rows": scalar(ctx.paper, f"SELECT count(*) FROM {t}"),
                         "columns": ", ".join(columns_of(ctx.paper, t))})
    ranges = {}
    for label, sql in (
        ("leader_trades_LIVE", "SELECT min(source_timestamp_utc), max(source_timestamp_utc) FROM leader_trades WHERE collection_method='LIVE'"),
        ("leader_trades_v2_history", "SELECT min(source_timestamp_utc), max(source_timestamp_utc) FROM leader_trades_v2" if table_exists(c, "leader_trades_v2") else None),
        ("orderbook_snapshots", "SELECT min(received_at_utc_ms)/1000.0, max(received_at_utc_ms)/1000.0 FROM orderbook_snapshots"),
        ("orderbook_snapshots_with_levels", "SELECT count(DISTINCT snapshot_id) , NULL FROM orderbook_levels"),
        ("underlying_prices", "SELECT min(received_at_utc), max(received_at_utc) FROM underlying_prices"),
        ("markets", "SELECT min(open_time_utc), max(open_time_utc) FROM markets"),
    ):
        if sql is None:
            continue
        r = c.execute(sql).fetchone()
        ranges[label] = (r[0], r[1])
    gaps = []
    if table_exists(c, "collector_events"):
        gaps = rows(c, """SELECT component, event_type, count(*) n, min(ts) first_ts, max(ts) last_ts FROM collector_events
                          WHERE ts <= ? AND event_type IN ('gap','reconnect','error','start','stop') GROUP BY component, event_type ORDER BY n DESC""", ctx.cutoff)
    # huecos de captura por delta entre snapshots REST consecutivos (cadencia fija 1/s por token;
    # los WS son event-driven y no sirven para medir huecos), umbral 30s
    ts_list = [r[0] / 1000.0 for r in c.execute(
        "SELECT received_at_utc_ms FROM orderbook_snapshots WHERE source='REST' AND received_at_utc_ms <= ? ORDER BY received_at_utc_ms",
        (ctx.cutoff * 1000.0,)).fetchall()]
    ob_gaps = []
    for i in range(1, len(ts_list)):
        d = ts_list[i] - ts_list[i - 1]
        if d > 30:
            ob_gaps.append({"gap_start_utc": fmt(ts_list[i - 1]), "gap_end_utc": fmt(ts_list[i]), "duration_s": round(d, 1)})
    return {"tables": info, "ranges": ranges, "events": gaps, "orderbook_gaps_gt30s": ob_gaps, "fill_stats": fill_stats}


# ========================================================= salidas ===
def write_csv(path, rows_, fieldnames=None):
    if not rows_:
        with open(path, "w", newline="") as f:
            f.write("")
        return
    fieldnames = fieldnames or list({k: None for r in rows_ for k in r.keys()}.keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows_:
            w.writerow(r)


def _ws_table(ws, rows_, start=1):
    if not rows_:
        ws.cell(row=start, column=1, value="(sin filas)")
        return start + 1
    headers = list({k: None for r in rows_ for k in r.keys()}.keys())
    for j, h in enumerate(headers, 1):
        ws.cell(row=start, column=j, value=h)
    for i, r in enumerate(rows_, start + 1):
        for j, h in enumerate(headers, 1):
            v = r.get(h)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str)
            ws.cell(row=i, column=j, value=v)
    return start + len(rows_) + 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--collector-db", required=True)
    ap.add_argument("--paper-db", default=None)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--cutoff-ts", type=float, default=None, help="REPORT_CUTOFF_TS fijo (epoch) para reproducibilidad; default: ahora")
    ap.add_argument("--leader-wallet", default=DEFAULT_LEADER_WALLET)
    ap.add_argument("--snapshot-tolerance-s", type=float, default=2.5)
    ap.add_argument("--underlying-pad-s", type=float, default=0.5,
                    help="compensa que received_at_utc del subyacente es el inicio de la request, no la recepcion")
    ap.add_argument("--fee-rate", type=float, default=DEFAULT_TAKER_FEE_RATE, help="tasa taker ASUMIDA (fee=shares*rate*p*(1-p)); 0 = sin fees")
    ap.add_argument("--stake-usd", type=float, default=10.0)
    ap.add_argument("--decision-offset-s", type=float, default=60.0)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--no-fills", action="store_true", help="no escribir trader_fills_canonical.csv")
    ap.add_argument("--skip-candidates", action="store_true", help="solo analisis del trader (mas rapido)")
    args = ap.parse_args(argv)

    ctx = Ctx(args)
    os.makedirs(args.output_dir, exist_ok=True)
    t0 = time.time()
    print(f"REPORT_CUTOFF_TS={ctx.cutoff:.3f} ({fmt(ctx.cutoff)})  version={VERSION}")

    fills, fill_stats = load_canonical_fills(ctx)
    markets = load_markets(ctx)
    by_market = defaultdict(list)
    n_no_market = 0
    for f in fills:
        if f["condition_id"] and f["condition_id"] in markets:
            by_market[f["condition_id"]].append(f)
        else:
            n_no_market += 1
    fill_stats["fills_without_market_metadata"] = n_no_market
    print(f"fills canonicos: {fill_stats['canonical_fills']}  mercados con fills: {len(by_market)}  sin metadata: {n_no_market}")

    seqs, fills_by_market = [], {}
    for cid, fl in by_market.items():
        m = markets[cid]
        if m.get("asset_symbol") not in ASSETS:
            continue  # esports u otros: fuera del analisis UP/DOWN
        seq, cf = reconstruct_market(ctx, m, fl)
        seqs.append(seq)
        fills_by_market[cid] = cf
    seqs.sort(key=lambda s: s["open_ts"] or 0)
    print(f"mercados reconstruidos (BTC/ETH/SOL): {len(seqs)}  resueltos: {sum(s['resolved_at_cutoff'] for s in seqs)}")

    # resumen del trader
    resolved = [s for s in seqs if s["resolved_at_cutoff"]]
    pending = [s for s in seqs if not s["resolved_at_cutoff"]]
    def _sum(key, rs):
        return round(sum((r[key] or 0) for r in rs), 2)
    summary_rows = []
    for label, rs in (("ALL", seqs), ("BTC", [s for s in seqs if s["asset"] == "BTC"]),
                      ("ETH", [s for s in seqs if s["asset"] == "ETH"]), ("SOL", [s for s in seqs if s["asset"] == "SOL"]),
                      ("5min", [s for s in seqs if s["window_minutes"] == 5]), ("15min", [s for s in seqs if s["window_minutes"] == 15])):
        rr = [s for s in rs if s["resolved_at_cutoff"]]
        cap = _sum("buy_cost_up", rr) + _sum("buy_cost_down", rr)
        gp = _sum("gross_pnl", rr)
        summary_rows.append({
            "group": label, "n_markets": len(rs), "n_resolved": len(rr), "n_pending": len(rs) - len(rr),
            "n_fills": sum(s["n_fills"] for s in rs), "n_buy": sum(s["n_buy"] for s in rs), "n_sell": sum(s["n_sell"] for s in rs),
            "pct_markets_both_sides": round(100 * sum(1 for s in rs if s["net_up_shares"] > 0 and s["net_down_shares"] > 0) / len(rs), 1) if rs else None,
            "median_coverage_ratio": quantiles([s["coverage_ratio"] for s in rs])[0.5],
            "buy_capital_resolved_usd": round(cap, 2),
            "gross_pnl_resolved_usd": gp, "gross_roi_resolved_pct": round(gp / cap * 100, 2) if cap else None,
            "paired_pnl_vwap_attrib_usd": _sum("paired_pnl_vwap_attrib", rr),
            "residual_pnl_usd": _sum("residual_pnl", rr), "realized_sell_pnl_usd": _sum("realized_sell_pnl", rr),
            "est_taker_fees_usd": _sum("est_taker_fees", rr),
            "net_pnl_if_taker_usd": round(gp - _sum("est_taker_fees", rr), 2),
            "pending_surplus_exposure_usd": _sum("pending_surplus_exposure_usd", [s for s in rs if not s["resolved_at_cutoff"]]),
            "max_drawdown_gross_usd": round(drawdown([s["gross_pnl"] for s in sorted(rr, key=lambda s: s["open_ts"] or 0)]), 2),
            "n_markets_pnl_identity_violation": sum(1 for s in rr if s["pnl_identity_residual"] not in (None, 0) and abs(s["pnl_identity_residual"]) > 1e-4),
            "n_same_second_ambiguities": sum(s["same_second_opposite_side_ambiguities"] for s in rs),
            "n_oversell_events": sum(s["oversell_events"] for s in rs),
        })

    hyps = hypothesis_tests(seqs, fills_by_market, ctx)
    rules = interpretable_rules(seqs, fills_by_market)

    cand_summary = cand_detail = calib = []
    universe = load_universe(ctx)
    leader_cids = set(fills_by_market)
    if not args.skip_candidates:
        cand_summary, cand_detail, calib = run_candidates(ctx, universe, frozenset(leader_cids))
    ctx.notes["universe_full_n"] = len(universe)
    ctx.notes["universe_traded_by_leader_n"] = sum(1 for m in universe if m["condition_id"] in leader_cids)

    paper = paper_forward(ctx)
    dmap = data_map(ctx, fill_stats)

    # ---------------- archivos ----------------
    od = args.output_dir
    write_csv(os.path.join(od, "trader_behavior_summary.csv"), summary_rows)
    write_csv(os.path.join(od, "trader_market_sequences.csv"), seqs)
    write_csv(os.path.join(od, "hypothesis_tests.csv"), hyps)
    write_csv(os.path.join(od, "strategy_candidates.csv"), cand_summary)
    write_csv(os.path.join(od, "strategy_candidates_by_asset_day.csv"), cand_detail)
    write_csv(os.path.join(od, "strategy_candidates_calibration.csv"), calib)
    if not args.no_fills:
        all_fills = [f for cid in fills_by_market for f in fills_by_market[cid]]
        write_csv(os.path.join(od, "trader_fills_canonical.csv"), all_fills)

    with open(os.path.join(od, "strategy_research_data_map.md"), "w") as f:
        f.write(f"# Data map (generado desde las bases reales)\n\nREPORT_CUTOFF_TS: {ctx.cutoff:.3f} ({fmt(ctx.cutoff)})  version {VERSION}\n\n")
        f.write("## Fuentes canonicas usadas\n\n")
        f.write("- Historico del lider: `leader_trades_v2` (origin=DATA_API_HISTORY). Excluye nada; `dedup_ambiguous=1` se conserva y se cuenta.\n")
        f.write("- LIVE del lider: `leader_trades` con collection_method='LIVE' e is_startup_batch=0.\n")
        f.write("- EXCLUIDOS: `leader_trades` BACKFILL (import de live_trades.jsonl / backfill.py --api, token_id NULL, duplicados) y startup batch.\n")
        f.write("- Union multiset por clave natural (tx_hash, ts redondeado, condition_id, outcome, side, price, shares): el historico fija la multiplicidad; LIVE solo agrega lo no cubierto.\n")
        f.write("- Order book: `orderbook_snapshots` + `orderbook_levels` con event_ts<=t Y received_ts<=t, tolerancia "
                f"{ctx.tol}s. Snapshots sin niveles (WS price_change) = NO_DEPTH (nunca fill asumido).\n")
        f.write(f"- Subyacente: `underlying_prices` con received_at_utc <= t - {ctx.upad}s (pad por timestamp de inicio de request).\n")
        f.write("- Mercados/resolucion: `markets` (winner, resolution_time_utc<=corte).\n")
        f.write("- Paper: `paper_markets/paper_decisions/paper_hedge_fills/paper_resolutions` (cohorte OFFICIAL). `paper_hedge_checks` solo COUNT.\n")
        f.write("- NO usados a proposito: `leader_inventory_timeline` (ignora SELL, tabla vieja con duplicados), `trade_context` (executable parcial, pierna contraria top-of-book, subconjunto).\n\n")
        f.write("## Union de fills\n\n")
        for k, v in sorted(fill_stats.items()):
            f.write(f"- {k}: {v}\n")
        f.write("\n## Rangos temporales\n\n")
        for k, (a, b) in dmap["ranges"].items():
            f.write(f"- {k}: {fmt(a) if isinstance(a, (int, float)) and a and a > 1e9 else a} -> {fmt(b) if isinstance(b, (int, float)) and b and b > 1e9 else b}\n")
        f.write("\n## Tablas\n\n| db | tabla | filas | columnas |\n|---|---|---:|---|\n")
        for t in dmap["tables"]:
            f.write(f"| {t['db']} | {t['table']} | {t['rows']} | {t['columns']} |\n")
        f.write("\n## Eventos del collector (gap/reconnect/error/start/stop)\n\n")
        for e in dmap["events"]:
            f.write(f"- {e['component']}/{e['event_type']}: {e['n']} ({fmt(e['first_ts'])} -> {fmt(e['last_ts'])})\n")
        f.write(f"\n## Huecos de order book >30s: {len(dmap['orderbook_gaps_gt30s'])}\n\n")
        for g in dmap["orderbook_gaps_gt30s"][:200]:
            f.write(f"- {g['gap_start_utc']} -> {g['gap_end_utc']} ({g['duration_s']}s)\n")
        if ctx.warnings:
            f.write("\n## Avisos\n\n" + "\n".join(f"- {w}" for w in ctx.warnings) + "\n")

    with open(os.path.join(od, "strategy_research.md"), "w") as f:
        f.write(f"# Strategy research -- corrida local\n\nREPORT_CUTOFF_TS={ctx.cutoff:.3f} ({fmt(ctx.cutoff)}) | fee_rate asumida={ctx.fee_rate} | stake=${ctx.stake} | decision=open+{ctx.decision_offset:.0f}s | tolerancia={ctx.tol}s | pad subyacente={ctx.upad}s\n\n")
        f.write("## Trader: resumen\n\n")
        for r in summary_rows:
            f.write(f"- {r['group']}: mercados={r['n_markets']} (resueltos {r['n_resolved']}, pendientes {r['n_pending']}), fills={r['n_fills']} (BUY {r['n_buy']} / SELL {r['n_sell']}), "
                    f"ambos lados={r['pct_markets_both_sides']}%, cov mediana={r['median_coverage_ratio']}, capital(res)=${r['buy_capital_resolved_usd']}, gross=${r['gross_pnl_resolved_usd']} "
                    f"({r['gross_roi_resolved_pct']}%), paired=${r['paired_pnl_vwap_attrib_usd']}, residual=${r['residual_pnl_usd']}, sells=${r['realized_sell_pnl_usd']}, "
                    f"fees_est=${r['est_taker_fees_usd']}, net_if_taker=${r['net_pnl_if_taker_usd']}, pendiente=${r['pending_surplus_exposure_usd']}, DD=${r['max_drawdown_gross_usd']}\n")
        f.write("\nDescomposicion: gross = payout(net) + proceeds(SELL) - coste total de BUY = paired(VWAP) + residual + realized_sell (identidad verificada por mercado; violaciones: "
                f"{summary_rows[0]['n_markets_pnl_identity_violation'] if summary_rows else 'n/a'}). 'paired' usa atribucion VWAP historica: NO es coste simultaneo ejecutable -- ver "
                "median_simultaneous_pair_cost_exec en trader_market_sequences.csv para eso.\n")
        f.write("\n## Hipotesis (sin atribuir intencion)\n\n| hipotesis | grupo | n | tasa | IC95 | p | veredicto |\n|---|---|---:|---:|---|---:|---|\n")
        for h in hyps:
            f.write(f"| {h['hypothesis']} | {h['group']} | {h['n']} | {h['observed_rate']} | [{h['ci95_low']},{h['ci95_high']}] | {h['p_value_vs_baseline']} | {h['verdict']} |\n")
        f.write(f"\n## Reglas interpretables (arbol depth-2, primera pierna Up=1)\n\nn={rules.get('n')} test_acc={rules.get('acc_test')} majority_acc={rules.get('acc_majority_test')} -- {rules.get('note')}\n\n")
        for r in rules.get("rules", []):
            f.write(f"- {r}\n")
        f.write("\n## Candidatas (universo completo, TRAIN/VALIDATION/TEST cronologico)\n\n")
        f.write(f"Split: {json.dumps(ctx.notes.get('split'), default=str)}\n\nUniverso: {ctx.notes.get('universe_full_n')} mercados; operados por el lider: {ctx.notes.get('universe_traded_by_leader_n')}. "
                f"Evaluaciones sobre TEST: {ctx.notes.get('n_candidate_evaluations_on_TEST')} (leer IC/p con esa multiplicidad).\n\n")
        f.write("| candidata | split | n | win | capital | gross | ROI% | fees | net ROI% | IC95 net ROI | DD | pares |\n|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|\n")
        for s in cand_summary:
            f.write(f"| {s['candidate']} | {s['split']} | {s['n_traded']} | {s.get('win_rate')} | {s.get('capital_usd')} | {s.get('gross_pnl_usd')} | {s.get('gross_roi_pct')} | {s.get('est_fees_usd')} | {s.get('net_roi_pct')} | [{s.get('net_roi_ci95_low')},{s.get('net_roi_ci95_high')}] | {s.get('max_drawdown_gross_usd')} | {s.get('n_paired')} |\n")
        if paper:
            f.write("\n## Paper forward (cohorte OFFICIAL)\n\n")
            for r in paper["summary"]:
                f.write(f"- {r}\n")
            for r in paper["same_markets"]:
                f.write(f"- {r}\n")
            f.write(f"- hedge_checks (COUNT): {paper['n_hedge_checks_total_COUNT_ONLY']}, hedge_fills: {paper['n_hedge_fills']}\n")
        f.write("\n## Avisos\n\n" + ("\n".join(f"- {w}" for w in ctx.warnings) or "- ninguno") + "\n")
        f.write("\n## Lo que este informe NO afirma\n\n- No afirma la intencion del trader: solo frecuencias vs azar.\n- No propone una V4 definitiva: las candidatas son baselines y familias parametrizadas; cualquier eleccion debe hacerse sobre VALIDATION y confirmarse en TEST una sola vez.\n- Fees: tasa ASUMIDA; maker/taker del lider desconocido.\n")

    if openpyxl is not None:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        ws = wb.create_sheet("Overview")
        ov = [{"key": "REPORT_CUTOFF_TS", "value": f"{ctx.cutoff:.3f} ({fmt(ctx.cutoff)})"}, {"key": "version", "value": VERSION},
              {"key": "fee_rate_assumed", "value": ctx.fee_rate}, {"key": "stake_usd", "value": ctx.stake},
              {"key": "snapshot_tolerance_s", "value": ctx.tol}, {"key": "underlying_pad_s", "value": ctx.upad},
              {"key": "universe_full_n", "value": ctx.notes.get("universe_full_n")}, {"key": "universe_traded_by_leader_n", "value": ctx.notes.get("universe_traded_by_leader_n")},
              {"key": "split", "value": json.dumps(ctx.notes.get("split"), default=str)},
              {"key": "n_candidate_evaluations_on_TEST", "value": ctx.notes.get("n_candidate_evaluations_on_TEST")},
              {"key": "elapsed_s", "value": round(time.time() - t0, 1)}]
        ov += [{"key": f"fills.{k}", "value": v} for k, v in sorted(fill_stats.items())]
        ov += [{"key": "warning", "value": w} for w in ctx.warnings]
        _ws_table(ws, ov)
        _ws_table(wb.create_sheet("Data Map"), dmap["tables"])
        _ws_table(wb.create_sheet("Trader Summary"), summary_rows)
        _ws_table(wb.create_sheet("Market Sequences"), seqs)
        _ws_table(wb.create_sheet("Hypotheses"), hyps)
        _ws_table(wb.create_sheet("Rules Tree"), [{"rule": r} for r in rules.get("rules", [])] + [{"rule": f"n={rules.get('n')} acc_test={rules.get('acc_test')} acc_majority={rules.get('acc_majority_test')} {rules.get('note')}"}])
        _ws_table(wb.create_sheet("Candidates"), cand_summary)
        _ws_table(wb.create_sheet("Candidates Calibration"), calib)
        _ws_table(wb.create_sheet("Candidates By Asset Day"), cand_detail)
        if paper:
            r0 = _ws_table(wb.create_sheet("Forward Paper"), paper["summary"])
            r0 = _ws_table(wb["Forward Paper"], paper["same_markets"], start=r0 + 1)
            _ws_table(wb["Forward Paper"], [{"n_hedge_checks_COUNT_ONLY": paper["n_hedge_checks_total_COUNT_ONLY"], "n_hedge_fills": paper["n_hedge_fills"]}], start=r0 + 1)
        dq = wb.create_sheet("Data Quality")
        r0 = _ws_table(dq, dmap["events"])
        _ws_table(dq, dmap["orderbook_gaps_gt30s"], start=r0 + 1)
        wb.save(os.path.join(od, "strategy_research.xlsx"))
    else:
        ctx.warn("openpyxl no instalado: se omite strategy_research.xlsx (los CSV/MD estan completos)")

    print(f"listo en {time.time() - t0:.1f}s -> {od}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
