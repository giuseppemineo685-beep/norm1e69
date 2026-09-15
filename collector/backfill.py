"""
Backfill histórico -- separado y marcado (`collection_method='BACKFILL'`)
de lo capturado en vivo. Dos fuentes:

1. `state/live_trades.jsonl` ya existente en el repo: se extraen SOLO los
   campos del lado del líder (precio, mercado, timestamp) de los eventos
   `open` -- se descartan a propósito `cost`/`ref_price`/`response` (esos
   son datos de la copia/ejecución, no del líder) para no filtrar nada de
   mirror hacia este dataset de análisis.
2. Paginación best-effort contra `data-api.polymarket.com/trades` -- la
   API pública no documenta un cursor estable para este endpoint; se
   intenta con `offset` y se para en cuanto deja de traer trades nuevos
   (o tras un tope de páginas), sin asumir que existe más historia de la
   que realmente se puede recuperar. No se inventa order book histórico
   para ninguno de los dos casos -- de eso ya se encarga trade_context.py
   marcando `context_available=False` cuando no hay snapshot real.
"""
import json
import sys
import time
from pathlib import Path

import db
import polymarket_api as pm
from config import LEADER_WALLET, REPO_ROOT

LIVE_TRADES_JSONL = REPO_ROOT / "state" / "live_trades.jsonl"


def import_live_trades_jsonl(path: Path = LIVE_TRADES_JSONL):
    if not path.exists():
        print(f"no existe {path}, nada que importar")
        return 0
    n = 0
    now = time.time()
    with db.connect() as conn:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "open":
                continue  # only the leader-side of an "open" event; closes are our own PnL, not theirs
            delay = rec.get("delay_s") or 0
            source_ts = rec["ts"] - delay
            condition_id = rec.get("conditionId")
            # leader_price y leader_cost son AMBOS datos del lado del lider (su
            # precio y su monto real gastado en ESE trade) -- cost/ref_price son
            # los nuestros (de la copia) y se siguen descartando a proposito.
            # shares se deriva de estos dos, ya que el log no lo guarda directo.
            leader_price = rec.get("leader_price")
            leader_cost = rec.get("leader_cost")
            shares = (leader_cost / leader_price) if (leader_price and leader_cost is not None) else None
            raw = {"market_title": rec.get("market_title"), "outcome": rec.get("outcome"),
                   "leader_price": leader_price, "leader_cost": leader_cost,
                   "imported_from": "live_trades.jsonl"}
            cur = conn.execute(
                """INSERT OR IGNORE INTO leader_trades
                   (leader_wallet, trade_id, transaction_hash, source_timestamp_utc, received_at_utc,
                    detection_latency_ms, condition_id, market_slug, market_title, asset_symbol,
                    token_id, outcome, side, price, shares, usdc_amount,
                    seconds_since_market_open, seconds_to_market_close, maker_taker, raw_payload,
                    collection_method)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, 'BUY', ?, ?, ?,
                           NULL, NULL, NULL, ?, 'BACKFILL')""",
                (LEADER_WALLET, f"live_trader_import:{rec['ts']}", source_ts, now, delay * 1000.0,
                 condition_id, rec.get("market_title"), rec.get("outcome"), leader_price, shares,
                 leader_cost, json.dumps(raw)),
            )
            n += cur.rowcount > 0
    print(f"importados {n} leader-side events nuevos desde {path}")
    return n


def backfill_data_api(max_pages=20, page_size=100):
    """Best-effort: intenta paginar hacia atrás con `offset`. Si la API no lo
    soporta, la primera página repetida detiene el intento sin loop infinito."""
    seen_hashes = set()
    total_new = 0
    for page in range(max_pages):
        try:
            trades = pm.get_leader_trades(LEADER_WALLET, limit=page_size, offset=page * page_size)
        except Exception as e:
            print(f"backfill_data_api: parada en pagina {page}, error: {e}")
            break
        if not trades:
            break
        hashes = {t.get("transactionHash") for t in trades}
        if hashes <= seen_hashes:
            print(f"backfill_data_api: pagina {page} no trajo nada nuevo (offset probablemente no soportado), paro")
            break
        seen_hashes |= hashes
        now = time.time()
        with db.connect() as conn:
            for t in trades:
                try:
                    source_ts = float(t["timestamp"])
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO leader_trades
                           (leader_wallet, trade_id, transaction_hash, source_timestamp_utc, received_at_utc,
                            detection_latency_ms, condition_id, market_slug, market_title, asset_symbol,
                            token_id, outcome, side, price, shares, usdc_amount,
                            seconds_since_market_open, seconds_to_market_close, maker_taker, raw_payload,
                            collection_method)
                           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'BACKFILL')""",
                        (LEADER_WALLET, t.get("id"), t["transactionHash"], source_ts, now,
                         (now - source_ts) * 1000.0, t.get("conditionId"), t.get("title"),
                         t.get("asset"), t.get("outcome"), t.get("side"), float(t["price"]),
                         float(t["size"]), float(t["price"]) * float(t["size"]),
                         t.get("makerTaker"), json.dumps(t, default=str)),
                    )
                    total_new += cur.rowcount > 0
                except (KeyError, ValueError, TypeError):
                    continue
    print(f"backfill_data_api: {total_new} trades nuevos en {min(max_pages, page + 1)} paginas")
    return total_new


if __name__ == "__main__":
    db.init_db()
    import_live_trades_jsonl()
    if "--api" in sys.argv:
        backfill_data_api()
