"""
UNICO archivo de todo live_micro_favorite_* que puede importar el SDK real
(`polymarket`) o tocar una private key. Aislado a proposito, igual criterio
que live_micro/live_micro_order_client.py (el del ejecutor momentum) --
pero un archivo FISICAMENTE DISTINTO, sin ningun import cruzado entre los
dos ejecutores nuevos.

Dos tipos de llamada de red, claramente separados:
  1) get_market_meta(): GET publico, SIN credenciales -- necesario porque
     data.db no guarda tick_size/minimum_order_size (se sigue reportando de
     forma informativa, aunque ya NO bloquea el sizing -- ver mas abajo).
     Se usa en DRY_RUN y LIVE.
  2) make_client()/get_usdc_balance()/place_fak_market_buy() -- SOLO se
     llaman si live_micro_favorite_config.LIVE_MICRO_FAVORITE_ENABLED es True.

Construccion de orden (2026-09-16, corregido tras verificacion real):
`place_market_order(side="BUY", amount=usd, max_spend=usd, max_price=precio,
order_type="FAK")` -- NO `place_limit_order(size=shares, ...)`. Verificado
contra el codigo fuente real de polymarket-client==0.10.0 (instalado en un
venv aislado y desechable solo para inspeccion, nunca en este proceso) y
contra 15 fills LIVE reales de scripts/live_trader.py
(state/live_trades.jsonl): 2 de esos fills tuvieron menos de 5 shares
(1.69 y 2.44), y las 2 API metadata endpoints de esos mismos mercados
siguen reportando minimum_order_size=5 -- confirma que ese minimo NO aplica
a la construccion de ordenes por monto (amount-based), solo a limit orders
por tamaño en shares. Ver docs/LIVE_MICRO_FAVORITE.md, seccion "Verificacion
del mecanismo de proteccion de precio real".

`max_price` y `max_spend` (con fees incluidas) quedan matematicamente
codificados en maker_amount/taker_amount ANTES de firmar (ver
_prepare_protected_market_order_draft en el SDK) -- la proteccion es parte
de la orden firmada (EIP-712), no solo un chequeo previo del lado cliente.
"""
import json
import time
import urllib.request

import live_micro_favorite_config as cfg

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_market_meta(condition_id, timeout=8):
    """Lectura publica, sin credenciales. Devuelve dict con tick_size y
    minimum_order_size REALES del mercado, o {'error': ...} si fallo
    (fail closed -- el llamador debe tratarlo como 'no operar')."""
    try:
        d = _http_get_json(f"{cfg.HOST}/markets/{condition_id}", timeout=timeout)
    except Exception as e:
        return {"error": str(e)}
    tick_size = d.get("minimum_tick_size") or d.get("tick_size")
    min_order_size = d.get("minimum_order_size") or d.get("min_order_size")
    return {
        "tick_size": float(tick_size) if tick_size is not None else None,
        "minimum_order_size": float(min_order_size) if min_order_size is not None else None,
        "raw": d,
    }


def make_client():
    """SOLO se llama si LIVE_MICRO_FAVORITE_ENABLED. Nunca en DRY_RUN."""
    if not cfg.LIVE_MICRO_FAVORITE_ENABLED:
        raise RuntimeError("make_client() llamado sin LIVE_MICRO_FAVORITE_ENABLED -- esto no deberia pasar nunca")
    if not cfg.LIVE_MICRO_FAVORITE_PRIVATE_KEY:
        raise RuntimeError("falta LIVE_MICRO_FAVORITE_PRIVATE_KEY (env var)")
    if not cfg.LIVE_MICRO_FAVORITE_FUNDER:
        raise RuntimeError("falta LIVE_MICRO_FAVORITE_FUNDER (env var, direccion con los fondos)")
    from polymarket import SecureClient
    return SecureClient.create(private_key=cfg.LIVE_MICRO_FAVORITE_PRIVATE_KEY, wallet=cfg.LIVE_MICRO_FAVORITE_FUNDER)


def get_usdc_balance(client):
    bal = client.get_balance_allowance(asset_type="COLLATERAL")
    return bal.balance / 1_000_000  # USDC: 6 decimales


def place_fak_market_buy(client, token_id, amount_usd, max_spend_usd, max_price):
    """Orden de mercado FAK, protegida por max_price (techo de precio real,
    codificado en la orden firmada) y max_spend (gasto all-in maximo,
    incluye fees estimadas por el propio SDK). NUNCA se llama en DRY_RUN.

    amount_usd: monto objetivo a gastar (antes de que el SDK lo reduzca si
    hiciera falta para respetar max_spend con fees incluidas).
    """
    t0 = time.monotonic()
    resp = client.place_market_order(
        asset_id=token_id, side="BUY", amount=amount_usd,
        max_spend=max_spend_usd, max_price=max_price, order_type=cfg.ORDER_TYPE)
    latency_ms = (time.monotonic() - t0) * 1000.0
    return resp, latency_ms
