"""
Configuracion central del ejecutor micro-LIVE para POLYMARKET_FAVORITE_BASELINE
-- candidato NUEVO, reemplaza a MOMENTUM_PURE (live_micro/, que queda intacto,
sin tocar ni borrar) como estrategia elegida para la prueba micro-LIVE.

Paquete completamente SEPARADO de live_micro/ (el ejecutor de MOMENTUM_PURE):
nombres de variable de entorno distintos, base de datos distinta, gate LIVE
distinto -- para que ambos puedan correr en paralelo sin ninguna posibilidad
de cruzarse (ninguno puede leer las credenciales o el estado del otro).

Este archivo NO importa `polymarket`, NO lee credenciales de ningun otro
ejecutor (ni el viejo scripts/live_trader.py ni live_micro/), NO importa
scripts.live_trader. Ver docs/LIVE_MICRO_FAVORITE.md, seccion Aislamiento.
"""
import os

# --------------------------------------------------------------- limites ---
MAX_ORDER_USD = 2.0                 # maximo por operacion, ALL-IN (incluye fees estimadas) --
# se pasa como amount Y max_spend a place_market_order(), asi que el SDK
# mismo reduce el monto si hace falta para que amount+fee nunca supere esto.
# Ya NO se usa minimum_order_size (en shares) para rechazar ordenes: se
# verifico que ese minimo NO aplica a place_market_order(amount=...) --
# solo a construccion de limit orders por tamaño en shares. Ver
# docs/LIVE_MICRO_FAVORITE.md. minimum_order_size se sigue registrando de
# forma informativa en cada intento, nunca bloquea.
MAX_ORDERS = 1                      # TEMPORAL (2026-09-16, prueba de verificacion de ejecucion
# real pedida explicitamente): normalmente 10, bajado a 1 para esta corrida
# -- ver STOP_AFTER_FIRST_ATTEMPT abajo. Revertir a 10 cuando el usuario lo pida.
MAX_CAPITAL_DEPLOYED_USD = MAX_ORDERS * MAX_ORDER_USD  # derivado, no independiente. = $2.00 mientras MAX_ORDERS=1
KILL_SWITCH_LOSS_USD = 10.0         # kill switch: perdida acumulada realizada
MAX_CONSECUTIVE_FAILURES = 3        # detener tras N ordenes fallidas consecutivas
MAX_SNAPSHOT_AGE_S = 1.0            # nunca operar con snapshot de order book mas viejo que esto

STOP_AFTER_FIRST_ATTEMPT = True     # TEMPORAL: el proceso se detiene solo tras procesar
# el primer mercado que llega a su instante de decision -- sea cual sea el
# resultado (FILLED/PARTIAL/REJECTED/ERROR/cualquier SKIPPED_*) -- en vez de
# seguir corriendo indefinidamente. Pedido explicitamente para esta prueba
# puntual de verificacion de ejecucion real.

# --------------------------------------------------------- universo/regla ---
ALLOWED_ASSETS = ("BTC", "ETH", "SOL")
WINDOW_MINUTES = 5
STRATEGY = "POLYMARKET_FAVORITE_BASELINE"   # unica estrategia habilitada -- nunca cobertura de la pierna contraria
ORDER_TYPE = "FAK"                  # fill-and-kill / marketable limit -- nunca GTC/GTD, nunca queda abierta

# reglas de decision -- IMPORTADAS de collector/run_paper_validation.py
# (decide_favorite(), no reescrita aqui) para garantizar "exactamente la
# misma regla congelada del paper". Ver live_micro_favorite_executor.py.

LIMIT_PRICE_BUFFER = 0.0            # limit_price = best_ask + esto. 0.0 = el mas conservador:
# si el book se movio ni un tick desde el snapshot fresco, el FAK no llena
# (nunca paga peor de lo que se vio) en vez de arriesgar slippage.
# Solo usado como referencia/gate para DRY_RUN -- LIVE calcula su propio
# max_price contra el book en vivo, ver MAX_PAPER_PRICE_SLIPPAGE abajo.

# ----------------------------------------------- reintentos LIVE (2026-09-17) ---
# Propuesta del handoff: en vez de fiarse del snapshot guardado en data.db
# como cotizacion final de ejecucion, LIVE re-consulta el order book REAL
# del CLOB (lectura publica, sin credenciales) inmediatamente antes de cada
# envio. Cada mercado candidato puede generar hasta MAX_LIVE_SUBMISSIONS_PER_MARKET
# envios REALES (cada uno cuenta individualmente contra MAX_ORDERS/kill
# switch, igual que antes) -- se detiene en el primer FILLED/PARTIAL, o tras
# agotar el tope sin fill. Mientras MAX_ORDERS=1 (temporal, ver arriba) esto
# en la practica sigue limitado a 1 envio real total por corrida -- subir
# MAX_ORDERS es una decision aparte, no implicita en este cambio.
MAX_LIVE_SUBMISSIONS_PER_MARKET = 3
MAX_PAPER_PRICE_SLIPPAGE = 0.02     # si el precio ejecutable EN VIVO (para el monto
# completo MAX_ORDER_USD) supera paper_expected_price en mas de esto, no se
# envia ninguna orden en ese intento (no consume cupo de MAX_LIVE_SUBMISSIONS_PER_MARKET)
# y se detiene esta oportunidad -- fail closed, nunca se persigue el precio.

# --------------------------------------------------------- credenciales ---
# Nombres DISTINTOS a los del bot viejo (POLY_PRIVATE_KEY/POLY_FUNDER/LIVE/
# LIVE_MAX_TOTAL_CAPITAL) Y DISTINTOS a los del otro ejecutor nuevo
# (LIVE_MICRO_PRIVATE_KEY/LIVE_MICRO_FUNDER/live_micro/), a proposito: los
# dos ejecutores micro-LIVE (momentum y favorite) deben poder correr en
# paralelo sin que ninguno pueda leer la configuracion del otro por accidente.
LIVE_MICRO_FAVORITE_PRIVATE_KEY = os.environ.get("LIVE_MICRO_FAVORITE_PRIVATE_KEY") or None
LIVE_MICRO_FAVORITE_FUNDER = os.environ.get("LIVE_MICRO_FAVORITE_FUNDER") or None
LIVE_MICRO_FAVORITE_SIGNATURE_TYPE = int(os.environ.get("LIVE_MICRO_FAVORITE_SIGNATURE_TYPE") or "1")

# --------------------------------------------------------------- gate LIVE ---
# Doble gate, independiente del de live_micro/ (momentum):
#   1) LIVE_MICRO_FAVORITE=1
#   2) LIVE_MICRO_FAVORITE_CONFIRM=I_UNDERSTAND_REAL_MONEY (frase exacta)
# Sin AMBAS, el ejecutor corre en DRY_RUN pase lo que pase.
_live_flag = (os.environ.get("LIVE_MICRO_FAVORITE") or "").strip().lower() in ("1", "true", "yes")
_confirm_phrase = os.environ.get("LIVE_MICRO_FAVORITE_CONFIRM") or ""
LIVE_MICRO_FAVORITE_ENABLED = _live_flag and _confirm_phrase == "I_UNDERSTAND_REAL_MONEY"

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

POLL_INTERVAL_S = 5
