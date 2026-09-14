#!/usr/bin/env python3
"""
Nuestro propio "Polycool" - copy-trading en PAPEL de una sola wallet
(0x3048d65321be3497164cdfc2996f94f98a2e7537, alias "x-MoneyForWhiskas" /
antes "@justoneofmystrategies"), sin el 1% de fee de Polycool y con el
filtro de conviccion que la app no ofrece.

Reglas (validadas por backtest sobre ~50h reales, ver README):
- Solo copia si SU trade individual costo >= LEADER_MIN_TRADE (conviccion)
- Por cada mercado (Up+Down juntos) mantenemos SU proporcion real entre los
  dos lados, aplicando MIRROR_PCT al total y un tope MAX_MARKET_TOTAL al
  TOTAL del mercado (no un tope independiente por lado). Cada trade nuevo de
  ella actualiza cuanto lleva invertido en cada lado y recalculamos cuanto
  nos falta agregar de este lado para seguir esa misma proporcion.
  (2026-09-14: se encontro y corrigio un bug donde el tope viejo se aplicaba
  por lado por separado, lo que aplastaba apuestas muy asimetricas tipo
  87%/13% en casi 50/50 para nosotros - causo una perdida real de ~$40.
  Ver README para el detalle del bug y la validacion del arreglo.)
- Nunca opera si el incremento quedaria por debajo de POLY_MIN_TRADE ($1, el
  minimo real de Polymarket) o si no alcanza el efectivo disponible
- El precio de entrada de cada posicion es un promedio ponderado de todos
  los incrementos que se le fueron agregando, no el precio de un solo trade
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
MAX_MARKET_TOTAL = 20.0  # tope al TOTAL del mercado (ambos lados juntos), no por lado -
# el tope viejo "MAX_PER_TRADE por lado" aplastaba el lado fuerte pero no el
# debil, convirtiendo apuestas 87%/13% en casi 50/50. Validado contra datos
# reales del 2026-09-14: -17% a -21% con tope por lado, -5% con tope total
# preservando la proporcion (mas cerca de su +3.5% real).
MAX_SLIPPAGE = 0.10  # 10 centavos - misma guardia configurada en la cuenta real de Polycool
POLY_MIN_TRADE = 1.0
START_CASH = 600.0
POLL_INTERVAL = 1  # segundos - bajado de 3s a 1s: sus trades reales mostraron demoras de 1-3s
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
        "leader_state": {},  # {conditionId: {"Up": costo_acumulado_de_ella, "Down": costo_acumulado_de_ella}}
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


def get_position(state, condition_id, outcome):
    for p in state["open_positions"]:
        if p["conditionId"] == condition_id and p["outcome"] == outcome:
            return p
    return None


def process_new_trade(t, state, now):
    """Cada trade nuevo de ella actualiza cuanto lleva invertido en CADA lado
    de este mercado (leader_state), y recalculamos el objetivo proporcional
    completo para el lado que acaba de mover - preservando su ratio real
    Up/Down con un tope al TOTAL del mercado, no un tope independiente por
    lado. Solo compramos la DIFERENCIA entre ese objetivo y lo que ya
    tenemos invertido en ese lado (nunca vendemos en este sistema de papel)."""
    leader_cost = t["size"] * t["price"]
    state["n_detected"] += 1

    if leader_cost < LEADER_MIN_TRADE:
        state["n_skipped_conviction"] += 1
        return

    delay = now - t["timestamp"]
    state["delays_measured"] = (state["delays_measured"] + [delay])[-500:]

    condition_id = t["conditionId"]
    outcome = t["outcome"]

    fill_price = current_market_price(condition_id, outcome)
    if fill_price is None:
        fill_price = t["price"]  # ultimo recurso si no hay trades recientes visibles

    if abs(fill_price - t["price"]) > MAX_SLIPPAGE:
        state["n_skipped_slippage"] = state.get("n_skipped_slippage", 0) + 1
        log(
            f"SALTEADO por slippage  {t.get('title','')[:40]:40s} {outcome:5s} "
            f"lider@{t['price']:.3f} ahora@{fill_price:.3f} (mov>{MAX_SLIPPAGE:.2f})"
        )
        return

    # cuanto lleva invertido ELLA en cada lado de este mercado hasta ahora
    leader_state = state.setdefault("leader_state", {})
    m = leader_state.setdefault(condition_id, {"Up": 0.0, "Down": 0.0})
    m[outcome] = m.get(outcome, 0.0) + leader_cost
    her_total = m["Up"] + m["Down"]

    # tope al TOTAL de ambos lados en este mercado, preservando su proporcion real
    target_total = min(her_total * MIRROR_PCT, MAX_MARKET_TOTAL)
    target_this_side = target_total * (m[outcome] / her_total) if her_total > 0 else 0.0

    existing = get_position(state, condition_id, outcome)
    our_cost_so_far = existing["cost"] if existing else 0.0
    copy_cost = target_this_side - our_cost_so_far

    if copy_cost < POLY_MIN_TRADE:
        # ya estamos al objetivo proporcional de este lado (o el incremento
        # es demasiado chico para el minimo real de Polymarket) - no hay
        # nada nuevo que agregar todavia, no es un error
        state["n_skipped_min"] += 1
        return
    if copy_cost > state["cash"]:
        state["n_skipped_cash"] += 1
        return

    shares = copy_cost / fill_price
    state["cash"] -= copy_cost
    state["n_copied"] += 1

    if existing:
        new_cost = existing["cost"] + copy_cost
        new_shares = existing["shares"] + shares
        existing["price_paid"] = new_cost / new_shares  # promedio ponderado
        existing["cost"] = new_cost
        existing["shares"] = new_shares
        existing["leader_price"] = t["price"]
        existing["leader_cost"] = m[outcome]
        existing["delay_s"] = delay
        existing["last_updated"] = now
        pos_rec = existing
    else:
        pos_rec = {
            "conditionId": condition_id,
            "outcome": outcome,
            "market_title": t.get("title"),
            "cost": copy_cost,
            "shares": shares,
            "price_paid": fill_price,
            "leader_price": t["price"],
            "leader_cost": m[outcome],
            "leader_tx": t["transactionHash"],
            "delay_s": delay,
            "opened_at": now,
            "last_updated": now,
        }
        state["open_positions"].append(pos_rec)

    her_pct = (m[outcome] / her_total * 100) if her_total else 0.0
    log(
        f"COPIA  {t.get('title','')[:40]:40s} {outcome:5s} "
        f"lider_lado=${m[outcome]:.2f}/${her_total:.2f} ({her_pct:.0f}%)  "
        f"nosotros +${copy_cost:.2f}@{fill_price:.3f} (lado=${pos_rec['cost']:.2f})  "
        f"demora={delay:.1f}s  cash=${state['cash']:.2f}"
    )
    append_log({"type": "open", "ts": now, "delta_cost": copy_cost, "delta_price": fill_price, **pos_rec})


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
    resolved_market_ids = set()
    for pos in state["open_positions"]:
        winner = get_resolution(pos["conditionId"], res_cache)
        if winner is None:
            still_open.append(pos)
            continue
        resolved_market_ids.add(pos["conditionId"])
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
    # limpiamos leader_state de mercados ya resueltos (5 min, no van a tener
    # mas trades) para que no crezca sin limite
    leader_state = state.get("leader_state", {})
    for cid in resolved_market_ids:
        leader_state.pop(cid, None)


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
