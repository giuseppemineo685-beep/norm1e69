#!/usr/bin/env python3
"""
Ejecutor de PLATA REAL: copia a norm1e69 (0x41e2e1ccf1e4940029af02259a31c6b89b9fa354)
colocando ordenes de verdad en Polymarket via su API oficial (py-clob-client),
en vez de simular como scripts/papertrader.py. Misma logica de proporcion
por mercado ya validada (ver README) - tope al TOTAL del mercado repartido
segun su proporcion real Up/Down, no un tope por lado.

Copia 1:1: mismo mercado, mismo lado, mismo monto en dolares que ella -
sin filtro de conviccion ni tope proporcional (ella hace cientos de trades
chicos por minuto; filtrar o recalcular por mercado nos haria perder
operaciones suyas y el resultado dejaria de ser el mismo). El unico piso
es el minimo real de Polymarket, $1 - no se puede copiar por debajo de eso
porque no se puede.

Aviso honesto: "exactamente igual" no es 100% posible - hay demora real
(deteccion + red) entre que ella compra y que nosotros compramos, asi que
el precio puede moverse un poco. Es la maxima fidelidad posible, no una
garantia matematica de resultado identico.

=================== SEGURIDAD - LEER ANTES DE CORRER ===================
- La private key NUNCA se pega en este repo, en el chat, ni en ningun
  archivo versionado. Se lee UNICAMENTE de la variable de entorno
  POLY_PRIVATE_KEY, que el dueno configura el mismo directamente como
  secret de GitHub Actions (Settings -> Secrets and variables -> Actions)
  o en su propio entorno local. Nadie mas la ve.
- Usar de preferencia una wallet separada, con fondos limitados, dedicada
  solo a este bot - no la wallet/cuenta principal. Si igual se usa la
  cuenta principal (decision del dueno), el limite de riesgo lo da
  LIVE_MAX_TOTAL_CAPITAL, no la separacion de wallets.
- Por defecto este script corre en DRY RUN: detecta, calcula el tamano de
  la orden, LOGUEA lo que haria, pero no manda nada a la blockchain. Para
  que coloque ordenes reales hace falta poner la variable de entorno
  LIVE=1 explicitamente. Esto es intencional - un arranque accidental sin
  LIVE=1 no puede perder plata.
==========================================================================
"""
import concurrent.futures, json, os, time, urllib.request, sys
from pathlib import Path

WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"  # norm1e69 - a quien copiamos
POLY_MIN_TRADE = 1.0  # minimo real de Polymarket - piso duro, no un filtro nuestro
MAX_SLIPPAGE = 0.97  # fusible SOLO anti-glitch (precio corrupto/fuera de 0-1 con margen) -
# ojo: con 0.25 esto SI actuaba como filtro real, no solo anti-glitch: se
# probo con datos reales y bloqueaba el 33% de sus trades (185 de 562) sin
# que hubiera ninguna otra razon (0 por falta de cash, 0 ordenes fallidas).
# Contradice "copiar 1:1, sin freno" - subido a 0.97 para que solo frene
# datos realmente rotos, no movimientos de precio normales cerca de 0 o 1.
POLL_INTERVAL = 1  # vuelto a 1s: con 0.3s empezaron fallos de conexion a
# data-api.polymarket.com (confirmado desde dos redes distintas, no era
# solo el sandbox) justo despues de bajar el intervalo - señal de rate
# limit/bloqueo. No vale la pena ganar fracciones de segundo si eso corta
# la deteccion entera.
RESOLVE_CHECK_INTERVAL = 15

# columna de comparacion en el dashboard: un segundo papel en paralelo que
# SI aplica el filtro de 25c como si fuera una decision real, para poder
# comparar "con filtro" vs "sin filtro" con los mismos datos en vez de
# adivinar. No afecta ni al papel principal ni a las ordenes reales.
COMPARISON_SLIPPAGE = 0.25

# capital de papel: validado por backtest sobre un dia real completo (3500
# trades, 239 mercados) - cubre el pico real de exposicion simultanea
# (~$1905) con margen, cero trades perdidos por falta de cash.
PAPER_START_CASH = float(os.environ.get("PAPER_START_CASH", "2500.0"))

# sin freno: LIVE_MAX_TOTAL_CAPITAL sin configurar = sin tope propio, copia
# todo lo que de. El backstop real pasa a ser el balance real en Polymarket
# (una orden que no se puede pagar, la rechaza el exchange, no este script).
# ojo: GitHub Actions declara la env var igual aunque el secret/variable de
# origen no exista - queda "" (string vacio), no ausente. os.environ.get(x,
# default) solo aplica el default si la KEY falta, no si esta vacia - por
# eso todo lo opcional de aca usa "or" en vez de un segundo argumento.
_cap_env = os.environ.get("LIVE_MAX_TOTAL_CAPITAL")
LIVE_MAX_TOTAL_CAPITAL = float(_cap_env) if _cap_env else float("inf")

LIVE = os.environ.get("LIVE") == "1"
PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY") or None
FUNDER = os.environ.get("POLY_FUNDER") or None  # direccion que tiene los fondos en Polymarket
SIGNATURE_TYPE = int(os.environ.get("POLY_SIGNATURE_TYPE") or "1")  # 1=email/Magic wallet, 0=MetaMask/hardware
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "live_state.json"
LOG_PATH = ROOT / "state" / "live_trades.jsonl"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get_json(url, timeout=10):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def make_client():
    """Arma el cliente de Polymarket. Solo se llama si LIVE=1 - en dry run
    ni siquiera hace falta tener las credenciales configuradas."""
    if not PRIVATE_KEY:
        log("ERROR: falta POLY_PRIVATE_KEY (env var) y LIVE=1 esta activo. Abortando.")
        sys.exit(1)
    if not FUNDER:
        log("ERROR: falta POLY_FUNDER (direccion que tiene los fondos). Abortando.")
        sys.exit(1)
    from py_clob_client.client import ClobClient
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                         signature_type=SIGNATURE_TYPE, funder=FUNDER)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_usdc_balance(client):
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    # la API devuelve el balance en unidades de 10^6 (USDC tiene 6 decimales)
    return float(bal["balance"]) / 1_000_000


def place_market_buy(client, token_id, usd_amount):
    """Manda una orden de mercado FOK (fill-or-kill: se llena entera ya
    mismo o se cancela sola - nunca queda una orden colgada en el libro)."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    order_args = MarketOrderArgs(token_id=token_id, amount=usd_amount, side=BUY, order_type=OrderType.FOK)
    signed = client.create_market_order(order_args)
    return client.post_order(signed, OrderType.FOK)


def default_state():
    return {
        "seen_leader_keys": [],
        "total_invested_live": 0.0,
        "started_at": time.time(),
        "n_detected": 0, "n_copied": 0,
        "n_skipped_min": 0, "n_skipped_slippage": 0, "n_skipped_cap_seguridad": 0,
        "n_orders_failed": 0,
        "delays_measured": [],
        # papel SIN filtro: corre siempre en paralelo, LIVE o no, para poder
        # comparar. Es el que representa "copia 1:1 de verdad".
        "paper_cash": PAPER_START_CASH,
        "paper_start_cash": PAPER_START_CASH,
        "paper_positions": {},  # {conditionId: {"Up":shares,"Down":shares,"cost":total,"title":str}}
        "n_paper_skipped_cash": 0,
        # papel CON filtro de 25c: columna de comparacion, misma plata
        # inicial, misma logica, pero descarta el trade si el precio se
        # movio mas de COMPARISON_SLIPPAGE desde que ella compro.
        "paper_b_cash": PAPER_START_CASH,
        "paper_b_start_cash": PAPER_START_CASH,
        "paper_b_positions": {},
        "n_paper_b_skipped_slippage": 0,
        "n_paper_b_skipped_cash": 0,
    }


def load_state():
    """OJO: cuando se agregan campos nuevos al estado, un state.json viejo
    ya guardado no los tiene. Sin este merge, cualquier acceso directo tipo
    state["campo_nuevo"] explota con KeyError apenas arranca - crashea en
    loop igual que el bug de la env var vacia. Siempre mergear sobre los
    defaults, nunca devolver el json crudo tal cual."""
    merged = default_state()
    if STATE_PATH.exists():
        merged.update(json.loads(STATE_PATH.read_text()))
    return merged


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def append_log(rec):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


def current_market_price(condition_id, outcome, timeout=6):
    try:
        d = http_get_json(f"https://data-api.polymarket.com/trades?market={condition_id}&limit=20", timeout=timeout)
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
        winner = next((tk["outcome"] for tk in tokens if tk.get("winner")), None)
        if winner:
            cache[condition_id] = winner
        return winner
    except Exception:
        return None


def _settle_one_book(state, res_cache, positions_key, cash_key, label, event_type):
    still_open = {}
    for cid, pos in state[positions_key].items():
        winner = get_resolution(cid, res_cache)
        if winner is None:
            still_open[cid] = pos
            continue
        payout = pos["shares"].get(winner, 0.0)
        state[cash_key] += payout
        pnl = payout - pos["cost"]
        log(f"CIERRE ({label}) {pos.get('title','')[:35]:35s} pnl=${pnl:+.2f}  cash=${state[cash_key]:.2f}")
        append_log({"ts": time.time(), "type": event_type, "conditionId": cid, "winner": winner,
                    "pnl": pnl, "cost": pos["cost"], "market_title": pos.get("title")})
    state[positions_key] = still_open


def settle_paper_positions(state, res_cache):
    """El papel corre siempre (LIVE o no) para poder comparar. Liquida las
    posiciones de ambos libros (sin filtro y con filtro de 25c) cuyo
    mercado ya resolvio."""
    _settle_one_book(state, res_cache, "paper_positions", "paper_cash", "papel", "close_paper")
    _settle_one_book(state, res_cache, "paper_b_positions", "paper_b_cash", "papel-filtrado", "close_paper_b")


def process_new_trade(t, state, now, client, price_cache):
    """Copia 1:1: mismo mercado, mismo lado, mismo monto en dolares que
    ella gasto en ESTE trade puntual. Sin filtro, sin escalar.

    price_cache vive UN SOLO poll (se resetea en main()) - ella repite
    mucho el mismo mercado en trades seguidos, asi que cachear el precio
    de referencia por (mercado, lado) dentro del mismo poll evita pedirlo
    de nuevo por cada trade individual, que era lo que hacia que el
    procesamiento de un lote grande no diera abasto y el atraso creciera
    sin parar en vez de mantenerse chico."""
    leader_cost = t["size"] * t["price"]
    state["n_detected"] += 1
    delay = now - t["timestamp"]
    state["delays_measured"] = (state.get("delays_measured", []) + [delay])[-1000:]

    condition_id, outcome, token_id = t["conditionId"], t["outcome"], t["asset"]
    copy_cost = leader_cost
    if copy_cost < POLY_MIN_TRADE:
        copy_cost = POLY_MIN_TRADE  # no se puede operar por debajo del minimo real de Polymarket

    cache_key = (condition_id, outcome)
    if cache_key not in price_cache:
        price_cache[cache_key] = current_market_price(condition_id, outcome)
    fill_ref_price = price_cache[cache_key]
    if fill_ref_price is None:
        fill_ref_price = t["price"]
    if abs(fill_ref_price - t["price"]) > MAX_SLIPPAGE:
        state["n_skipped_slippage"] += 1
        log(f"SALTEADO (precio roto)  {t.get('title','')[:40]:40s} {outcome:5s} lider@{t['price']:.3f} ahora@{fill_ref_price:.3f}")
        return

    # --- papel SIN filtro: corre siempre, para tener el registro completo
    # de "a que precio y con cuanta demora hubiesemos entrado" (copia 1:1 real)
    if copy_cost > state["paper_cash"]:
        state["n_paper_skipped_cash"] += 1
    else:
        state["paper_cash"] -= copy_cost
        pos = state["paper_positions"].setdefault(
            condition_id, {"shares": {}, "cost": 0.0, "title": t.get("title")})
        pos["shares"][outcome] = pos["shares"].get(outcome, 0.0) + copy_cost / fill_ref_price
        pos["cost"] += copy_cost

    # --- papel CON filtro de 25c: columna de comparacion en paralelo
    if abs(fill_ref_price - t["price"]) > COMPARISON_SLIPPAGE:
        state["n_paper_b_skipped_slippage"] += 1
    elif copy_cost > state["paper_b_cash"]:
        state["n_paper_b_skipped_cash"] += 1
    else:
        state["paper_b_cash"] -= copy_cost
        pos_b = state["paper_b_positions"].setdefault(
            condition_id, {"shares": {}, "cost": 0.0, "title": t.get("title")})
        pos_b["shares"][outcome] = pos_b["shares"].get(outcome, 0.0) + copy_cost / fill_ref_price
        pos_b["cost"] += copy_cost

    # --- real: solo si LIVE=1
    resp = {"dry_run": True}
    if LIVE:
        if state["total_invested_live"] + copy_cost > LIVE_MAX_TOTAL_CAPITAL:
            state["n_skipped_cap_seguridad"] += 1
            log(f"SALTEADO por tope de seguridad (${LIVE_MAX_TOTAL_CAPITAL:.2f})")
            return
        try:
            resp = place_market_buy(client, token_id, copy_cost)
            log(f"ORDEN REAL  {t.get('title','')[:40]:40s} {outcome:5s} +${copy_cost:.2f} demora={delay:.1f}s  resp={resp}")
        except Exception as e:
            state["n_orders_failed"] += 1
            log(f"ERROR colocando orden real: {e}")
            return
        state["total_invested_live"] += copy_cost
    else:
        log(f"[PAPER] {t.get('title','')[:40]:40s} {outcome:5s} +${copy_cost:.2f}@~{fill_ref_price:.3f} demora={delay:.1f}s")

    state["n_copied"] += 1
    append_log({"ts": now, "type": "open", "live": LIVE, "conditionId": condition_id, "outcome": outcome,
                "cost": copy_cost, "ref_price": fill_ref_price, "leader_price": t["price"],
                "leader_cost": leader_cost, "delay_s": delay,
                "market_title": t.get("title"), "response": resp})


def main():
    log(f"iniciando live_trader.py - LIVE={LIVE} (dry_run={'NO' if LIVE else 'SI'}) "
        f"tope_seguridad=${LIVE_MAX_TOTAL_CAPITAL:.2f} copiando a norm1e69")
    client = make_client() if LIVE else None
    if LIVE:
        bal = get_usdc_balance(client)
        log(f"balance real en Polymarket: ${bal:.2f}")
        if bal < POLY_MIN_TRADE:
            log("balance insuficiente, abortando arranque en modo LIVE")
            sys.exit(1)

    state = load_state()
    state["_live_flag"] = LIVE  # para que el dashboard muestre bien el modo actual
    seen = set(state.get("seen_leader_keys", []))
    warm_start = len(seen) == 0
    res_cache = {}
    last_resolve_check = 0

    while True:
        try:
            trades = http_get_json(f"https://data-api.polymarket.com/trades?user={WALLET}&limit=100", timeout=10)
            now = time.time()
            new_trades = []
            for t in reversed(trades):
                key = t["transactionHash"] + str(t["timestamp"]) + str(t["size"])
                if key in seen:
                    continue
                seen.add(key)
                if not warm_start:
                    new_trades.append(t)

            # Pre-buscar el precio de referencia de todos los (mercado, lado)
            # unicos de este lote EN PARALELO, antes de procesar ningun
            # trade. Con el cache solo (sin esto) el atraso seguia creciendo
            # porque ella opera varios mercados distintos a la vez (BTC/ETH/
            # SOL), no solo el mismo repetido - una consulta de red por
            # trade, en fila, no daba abasto a su ritmo real.
            if new_trades:
                price_cache = {}
                unique_keys = {(t["conditionId"], t["outcome"]) for t in new_trades}
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(unique_keys))) as ex:
                    futures = {ex.submit(current_market_price, cid, oc): (cid, oc) for cid, oc in unique_keys}
                    for fut in concurrent.futures.as_completed(futures):
                        price_cache[futures[fut]] = fut.result()
                for t in new_trades:
                    # cada trade en su propio try: un error en UNO (ej. un
                    # mercado con outcomes que no son Up/Down, como paso con
                    # un mercado de esports) no debe abortar el resto del
                    # lote - eso fue justo lo que causo demoras de +300s: la
                    # excepcion se propagaba y los demas trades del lote
                    # quedaban en "seen" sin haberse procesado nunca.
                    try:
                        process_new_trade(t, state, now, client, price_cache)
                    except Exception as e:
                        log(f"ERROR procesando trade individual (se sigue con el resto del lote): {e}")

            if warm_start:
                log(f"warm start: {len(seen)} trades existentes sembrados sin copiar")
            warm_start = False
            state["seen_leader_keys"] = list(seen)[-3000:]
        except Exception as e:
            log(f"error polling trades: {e}")

        if time.time() - last_resolve_check > RESOLVE_CHECK_INTERVAL:
            settle_paper_positions(state, res_cache)
            last_resolve_check = time.time()

        save_state(state)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
