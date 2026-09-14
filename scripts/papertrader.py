#!/usr/bin/env python3
"""
Nuestro propio "Polycool" - copy-trading en PAPEL de una sola wallet
(0x3048d65321be3497164cdfc2996f94f98a2e7537, alias "x-MoneyForWhiskas" /
antes "@justoneofmystrategies"), sin el 1% de fee de Polycool y con el
filtro de conviccion que la app no ofrece.

Reglas (validadas por backtest sobre ~50h reales, ver README):
- Solo copia si SU trade individual costo >= LEADER_MIN_TRADE (conviccion)
- Mirror MIRROR_PCT de ese costo, con tope MAX_PER_TRADE
- Nunca opera si el trade quedaria por debajo de POLY_MIN_TRADE ($1, el
  minimo real de Polymarket) o si no alcanza el efectivo disponible
- Arranca con START_CASH de capital en PAPEL (nada de plata real todavia)

Demora: NO se asume un numero fijo. Cada vez que detectamos un trade suyo
nuevo, medimos el delay real (su timestamp vs el instante en que lo vemos)
y usamos el precio REAL vigente en ese mercado en ESE momento (no el precio
que ella consiguio) como precio de llenado del papel - asi la demora y el
slippage quedan reflejados con datos reales, no con una suposicion.

Salida: state/paper_state.json (cash, posiciones abiertas, historial) +
state/paper_trades.jsonl (log append-only de cada trade papel).
"""
import json, re, time, urllib.request, os
from pathlib import Path

WALLET = "0x3048d65321be3497164cdfc2996f94f98a2e7537"
LEADER_MIN_TRADE = 20.0
MIRROR_PCT = 0.15
MAX_PER_TRADE = 10.0
MAX_SLIPPAGE = 0.10  # 10 centavos - misma guardia configurada en la cuenta real de Polycool
POLY_MIN_TRADE = 1.0
START_CASH = 600.0
POLL_INTERVAL = 3  # segundos - lo mas rapido que tiene sentido para una sola wallet
RESOLVE_CHECK_INTERVAL = 15
SNAPSHOT_INTERVAL = 600  # 10 minutos - performance a lo largo del tiempo

# 2026-09-14 ~10:22 UTC: el owner deposito $200 REALES en Polycool con esta
# misma configuracion (15% / $10 max / $20 min trader / guardia 10c). Este
# sistema en papel sigue de ahora en mas como referencia para comparar
# contra el rendimiento real de esa cuenta.
REAL_TRADING_STARTED_AT = 1789381327
OWNER_WALLET = "0xb3B50facc6189C01A98ED909B807CFBA8A3951C4"  # wallet real del owner en Polycool

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "paper_state.json"
LOG_PATH = ROOT / "state" / "paper_trades.jsonl"
SNAPSHOT_PATH = ROOT / "state" / "performance_snapshots.jsonl"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "cash": START_CASH,
        "start_cash": START_CASH,
        "open_positions": [],  # [{conditionId, outcome, cost, shares, price_paid, leader_tx, opened_at}]
        "seen_leader_keys": [],
        "started_at": time.time(),
        "n_detected": 0,
        "n_copied": 0,
        "n_skipped_conviction": 0,
        "n_skipped_min": 0,
        "n_skipped_cash": 0,
        "n_skipped_slippage": 0,
        "delays_measured": [],  # ultimos N delays reales en segundos
    }


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def append_log(rec):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


def current_market_price(condition_id, outcome, timeout=6):
    """Precio real vigente ahora mismo: el ultimo trade de CUALQUIER
    participante en ese mercado/outcome. Esto es lo que 'pagariamos' si
    copiaramos justo en este instante - captura la demora real."""
    try:
        d = http_get_json(
            f"https://data-api.polymarket.com/trades?market={condition_id}&limit=20",
            timeout=timeout,
        )
        for t in d:
            if t.get("outcome") == outcome:
                return t["price"]
    except Exception:
        pass
    return None


def get_resolution(condition_id, cache):
    if condition_id in cache:
        return cache[condition_id]
    try:
        d = http_get_json(f"https://clob.polymarket.com/markets/{condition_id}", timeout=8)
        tokens = d.get("tokens", [])
        winner = next((t["outcome"] for t in tokens if t.get("winner")), None)
        if winner:
            cache[condition_id] = winner
        return winner
    except Exception:
        return None


def process_new_trade(t, state, now):
    leader_cost = t["size"] * t["price"]
    state["n_detected"] += 1

    if leader_cost < LEADER_MIN_TRADE:
        state["n_skipped_conviction"] += 1
        return

    delay = now - t["timestamp"]
    state["delays_measured"] = (state["delays_measured"] + [delay])[-500:]

    fill_price = current_market_price(t["conditionId"], t["outcome"])
    if fill_price is None:
        fill_price = t["price"]  # ultimo recurso si no hay trades recientes visibles

    if abs(fill_price - t["price"]) > MAX_SLIPPAGE:
        state["n_skipped_slippage"] = state.get("n_skipped_slippage", 0) + 1
        log(
            f"SALTEADO por slippage  {t.get('title','')[:40]:40s} {t['outcome']:5s} "
            f"lider@{t['price']:.3f} ahora@{fill_price:.3f} (mov>{MAX_SLIPPAGE:.2f})"
        )
        return

    copy_cost = min(leader_cost * MIRROR_PCT, MAX_PER_TRADE)
    if copy_cost < POLY_MIN_TRADE:
        state["n_skipped_min"] += 1
        return
    if copy_cost > state["cash"]:
        state["n_skipped_cash"] += 1
        return

    shares = copy_cost / fill_price
    state["cash"] -= copy_cost
    state["n_copied"] += 1
    state["open_positions"].append({
        "conditionId": t["conditionId"],
        "outcome": t["outcome"],
        "market_title": t.get("title"),
        "cost": copy_cost,
        "shares": shares,
        "price_paid": fill_price,
        "leader_price": t["price"],
        "leader_cost": leader_cost,
        "leader_tx": t["transactionHash"],
        "delay_s": delay,
        "opened_at": now,
    })
    log(
        f"COPIA  {t.get('title','')[:40]:40s} {t['outcome']:5s} "
        f"lider=${leader_cost:.2f}@{t['price']:.3f}  nosotros=${copy_cost:.2f}@{fill_price:.3f}  "
        f"demora={delay:.1f}s  cash=${state['cash']:.2f}"
    )
    append_log({"type": "open", "ts": now, **state["open_positions"][-1]})


def append_snapshot(state):
    """Foto de performance cada SNAPSHOT_INTERVAL, para poder ver la curva
    en el tiempo y comparar despues contra el historial real de Polycool."""
    equity = state["cash"] + sum(p["cost"] for p in state["open_positions"])
    snap = {
        "ts": time.time(),
        "cash": state["cash"],
        "equity": equity,
        "open_positions": len(state["open_positions"]),
        "n_detected": state.get("n_detected", 0),
        "n_copied": state.get("n_copied", 0),
        "n_skipped_slippage": state.get("n_skipped_slippage", 0),
    }
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_PATH, "a") as f:
        f.write(json.dumps(snap) + "\n")
    log(f"SNAPSHOT equity=${equity:.2f} cash=${state['cash']:.2f} abiertas={len(state['open_positions'])}")


REAL_LOG_PATH = ROOT / "state" / "real_trades.jsonl"
_real_seen = set()


def poll_real_wallet():
    """Registra lo que Polycool REALMENTE ejecuto en la wallet del owner
    (publica, no hace falta ninguna clave) - para comparar despues contra
    lo que este sistema hubiera copiado."""
    try:
        trades = http_get_json(
            f"https://data-api.polymarket.com/trades?user={OWNER_WALLET}&limit=100", timeout=10
        )
    except Exception as e:
        log(f"error polling wallet real: {e}")
        return
    for t in reversed(trades):
        key = t["transactionHash"] + str(t["timestamp"]) + str(t["size"])
        if key in _real_seen:
            continue
        _real_seen.add(key)
        if t["timestamp"] < REAL_TRADING_STARTED_AT:
            continue  # actividad de antes del deposito de $200, no es parte de esta prueba
        rec = {
            "ts": time.time(),
            "trade_timestamp": t["timestamp"],
            "market_title": t.get("title"),
            "outcome": t["outcome"],
            "price": t["price"],
            "size": t["size"],
            "cost": t["size"] * t["price"],
            "transactionHash": t["transactionHash"],
        }
        REAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REAL_LOG_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
        log(
            f"REAL (Polycool)  {t.get('title','')[:40]:40s} {t['outcome']:5s} "
            f"${rec['cost']:.2f}@{t['price']:.3f}"
        )


def settle_positions(state, res_cache):
    still_open = []
    for pos in state["open_positions"]:
        winner = get_resolution(pos["conditionId"], res_cache)
        if winner is None:
            still_open.append(pos)
            continue
        correct = pos["outcome"] == winner
        payout = pos["shares"] if correct else 0.0
        state["cash"] += payout
        pnl = payout - pos["cost"]
        log(
            f"CIERRE {pos.get('market_title','')[:40]:40s} {pos['outcome']:5s} "
            f"{'GANO' if correct else 'PERDIO'}  pnl=${pnl:+.2f}  cash=${state['cash']:.2f}"
        )
        append_log({"type": "close", "ts": time.time(), "correct": correct, "pnl": pnl, **pos})
    state["open_positions"] = still_open


def main():
    state = load_state()
    seen = set(state.get("seen_leader_keys", []))
    # primera vez que este proceso arranca (nunca vio nada) -> el primer poll
    # trae hasta 100 trades VIEJOS de la wallet, no detecciones en tiempo
    # real. Los marcamos como vistos sin copiarlos, para no ensuciar las
    # metricas de demora ni abrir posiciones de papel con precios ya
    # obsoletos. Si el proceso se reinicia con seen_leader_keys ya poblado,
    # esto no aplica - sigue copiando normal.
    warm_start = len(seen) == 0
    res_cache = {}
    last_resolve_check = 0
    last_snapshot = 0
    log(f"iniciando - cash actual=${state['cash']:.2f} (arranco con ${state['start_cash']:.2f}) "
        f"posiciones abiertas={len(state['open_positions'])} warm_start={warm_start} "
        f"guardia_slippage=${MAX_SLIPPAGE:.2f}")

    while True:
        try:
            trades = http_get_json(
                f"https://data-api.polymarket.com/trades?user={WALLET}&limit=100", timeout=10
            )
            now = time.time()
            for t in reversed(trades):  # orden cronologico
                key = t["transactionHash"] + str(t["timestamp"]) + str(t["size"])
                if key in seen:
                    continue
                seen.add(key)
                if warm_start:
                    continue  # solo sembramos "ya visto", no copiamos historial
                process_new_trade(t, state, now)
            if warm_start:
                log(f"warm start: se sembraron {len(seen)} trades existentes sin copiar, arrancando a detectar desde aca")
            warm_start = False
            state["seen_leader_keys"] = list(seen)[-3000:]
        except Exception as e:
            log(f"error polling trades: {e}")

        poll_real_wallet()

        if time.time() - last_resolve_check > RESOLVE_CHECK_INTERVAL:
            settle_positions(state, res_cache)
            last_resolve_check = time.time()

        if time.time() - last_snapshot > SNAPSHOT_INTERVAL:
            append_snapshot(state)
            last_snapshot = time.time()

        save_state(state)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
