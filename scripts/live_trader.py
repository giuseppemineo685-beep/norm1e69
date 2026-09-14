#!/usr/bin/env python3
"""
Ejecutor de PLATA REAL: copia a norm1e69 (0x41e2e1ccf1e4940029af02259a31c6b89b9fa354)
colocando ordenes de verdad en Polymarket via su API oficial (py-clob-client),
en vez de simular como scripts/papertrader.py. Misma logica de proporcion
por mercado ya validada (ver README) - tope al TOTAL del mercado repartido
segun su proporcion real Up/Down, no un tope por lado.

Parametros calibrados para norm1e69 especificamente (su escala de apuesta es
mucho mas chica que la wallet original - mediana ~$5-7, no ~$50-100 - asi
que LEADER_MIN_TRADE se bajo de $20 a $5; validado con backtest sobre 239
mercados reales del 2026-09-14: filtrar por tamano no mejora su edge como
si lo hacia con la otra wallet, asi que el filtro aca es solo para
descartar ruido, no una senal de conviccion).

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
import json, os, time, urllib.request, sys
from pathlib import Path

WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"  # norm1e69 - a quien copiamos
LEADER_MIN_TRADE = 5.0
MIRROR_PCT = 0.15
MAX_MARKET_TOTAL = 20.0
MAX_SLIPPAGE = 0.10
POLY_MIN_TRADE = 1.0
POLL_INTERVAL = 1

# tope duro de seguridad: nunca invertir mas de esto en total (posiciones
# abiertas + ya gastado), sin importar lo que diga la logica de arriba.
# Es un freno de mano aparte del tope por mercado.
LIVE_MAX_TOTAL_CAPITAL = float(os.environ.get("LIVE_MAX_TOTAL_CAPITAL", "100.0"))

LIVE = os.environ.get("LIVE") == "1"
PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY")
FUNDER = os.environ.get("POLY_FUNDER")  # direccion que tiene los fondos en Polymarket
SIGNATURE_TYPE = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))  # 1=email/Magic wallet, 0=MetaMask/hardware
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


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "seen_leader_keys": [],
        "leader_state": {},  # {conditionId: {"Up": costo_ella, "Down": costo_ella}}
        "our_positions": {},  # {"conditionId|outcome": {"cost": x, "shares": y}}
        "total_invested_live": 0.0,
        "started_at": time.time(),
        "n_detected": 0, "n_copied": 0, "n_skipped_conviction": 0,
        "n_skipped_min": 0, "n_skipped_slippage": 0, "n_skipped_cap_seguridad": 0,
        "n_orders_failed": 0,
    }


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


def process_new_trade(t, state, now, client):
    leader_cost = t["size"] * t["price"]
    state["n_detected"] += 1
    if leader_cost < LEADER_MIN_TRADE:
        state["n_skipped_conviction"] += 1
        return

    condition_id, outcome, token_id = t["conditionId"], t["outcome"], t["asset"]
    fill_ref_price = current_market_price(condition_id, outcome)
    if fill_ref_price is None:
        fill_ref_price = t["price"]
    if abs(fill_ref_price - t["price"]) > MAX_SLIPPAGE:
        state["n_skipped_slippage"] += 1
        log(f"SALTEADO por slippage  {t.get('title','')[:40]:40s} {outcome:5s} lider@{t['price']:.3f} ahora@{fill_ref_price:.3f}")
        return

    m = state["leader_state"].setdefault(condition_id, {"Up": 0.0, "Down": 0.0})
    m[outcome] = m.get(outcome, 0.0) + leader_cost
    her_total = m["Up"] + m["Down"]
    target_total = min(her_total * MIRROR_PCT, MAX_MARKET_TOTAL)
    target_this_side = target_total * (m[outcome] / her_total) if her_total > 0 else 0.0

    key = f"{condition_id}|{outcome}"
    existing = state["our_positions"].get(key)
    already = existing["cost"] if existing else 0.0
    copy_cost = target_this_side - already

    if copy_cost < POLY_MIN_TRADE:
        state["n_skipped_min"] += 1
        return
    if state["total_invested_live"] + copy_cost > LIVE_MAX_TOTAL_CAPITAL:
        state["n_skipped_cap_seguridad"] += 1
        log(f"SALTEADO por tope de seguridad (${LIVE_MAX_TOTAL_CAPITAL:.2f}) - ya invertido ${state['total_invested_live']:.2f}")
        return

    her_pct = (m[outcome] / her_total * 100) if her_total else 0.0
    if not LIVE:
        log(f"[DRY RUN] copiaria {t.get('title','')[:40]:40s} {outcome:5s} "
            f"lider_lado=${m[outcome]:.2f}/${her_total:.2f} ({her_pct:.0f}%)  +${copy_cost:.2f}@~{fill_ref_price:.3f}")
        resp = {"dry_run": True}
    else:
        try:
            resp = place_market_buy(client, token_id, copy_cost)
            log(f"ORDEN REAL  {t.get('title','')[:40]:40s} {outcome:5s} +${copy_cost:.2f}  resp={resp}")
        except Exception as e:
            state["n_orders_failed"] += 1
            log(f"ERROR colocando orden real: {e}")
            return

    state["total_invested_live"] += copy_cost
    if existing:
        existing["cost"] += copy_cost
    else:
        state["our_positions"][key] = {"cost": copy_cost, "market_title": t.get("title")}
    state["n_copied"] += 1
    append_log({"ts": now, "live": LIVE, "conditionId": condition_id, "outcome": outcome,
                "cost": copy_cost, "ref_price": fill_ref_price, "leader_cost": leader_cost,
                "leader_total_side": m[outcome], "leader_total_market": her_total,
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

    while True:
        try:
            trades = http_get_json(f"https://data-api.polymarket.com/trades?user={WALLET}&limit=100", timeout=10)
            now = time.time()
            for t in reversed(trades):
                key = t["transactionHash"] + str(t["timestamp"]) + str(t["size"])
                if key in seen:
                    continue
                seen.add(key)
                if warm_start:
                    continue
                process_new_trade(t, state, now, client)
            if warm_start:
                log(f"warm start: {len(seen)} trades existentes sembrados sin copiar")
            warm_start = False
            state["seen_leader_keys"] = list(seen)[-3000:]
        except Exception as e:
            log(f"error polling trades: {e}")

        save_state(state)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
