# Ejecutor micro-LIVE (POLYMARKET_FAVORITE_BASELINE)

Candidato ACTUAL de la prueba micro-LIVE -- reemplaza a MOMENTUM_PURE
(`live_micro/`, que queda intacto y sin tocar, ya no es el candidato activo
pero sigue disponible). Paralelo al paper trading, exclusivamente para
validar ejecucion real -- no copia al trader, no reacciona a sus
operaciones. Usa **exactamente** la señal congelada `decide_favorite()` de
`collector/run_paper_validation.py` (import directo, no reimplementada).

**LIVE nunca se activa por defecto.** Ver "Gate LIVE" mas abajo.

## Aislamiento (bot viejo Y del otro ejecutor nuevo)

Mismas garantias que `live_micro/` (ver `live_micro/docs/LIVE_MICRO.md`,
seccion Aislamiento), MAS una capa adicional: este paquete es tambien
independiente de `live_micro/` (el ejecutor MOMENTUM_PURE) -- pueden correr
en paralelo sin que ninguno pueda leer la configuracion, credenciales o
estado del otro. Verificado por test:

| Garantia | Test |
|---|---|
| Sin referencia a `scripts.live_trader` | `test_no_reference_to_old_live_trader_anywhere` |
| `scripts/live_trader.py` intacto | `test_old_live_trader_file_is_byte_identical_untouched` |
| Cero acoplamiento con `live_micro/` (nombres de env var, modulos) | `test_no_coupling_with_momentum_executor` |
| Solo `live_micro_favorite_order_client.py` importa el SDK | `test_only_order_client_imports_polymarket_sdk_or_reads_its_credentials` |
| Nombres de env var distintos al bot viejo y al ejecutor momentum | `test_env_var_names_distinct_from_old_bot_and_momentum_executor` |
| Importar los modulos nuevos no carga `scripts.live_trader` ni `live_micro_config` (el del otro ejecutor) | `test_importing_favorite_modules_does_not_pull_in_old_live_trader_or_momentum` |
| Cero `PARTIAL_HEDGE` | `test_no_partial_hedge_strategy_referenced_anywhere` |

Vive en `live_micro_favorite/`, fuera de `collector/` (mismo motivo que
`live_micro/`: `collector/tests/test_integration_safe_mode.py`, sin tocar,
sigue probando que ese paquete nunca puede ejecutar).

## Limites obligatorios (`live_micro_favorite_config.py`)

| Limite | Valor | Nota |
|---|---|---|
| Maximo por operacion | $2.00 **all-in, incluye fees** | Se pasa como `amount` Y `max_spend` a `place_market_order()` -- el SDK reduce el monto real si hace falta para que amount+fee nunca supere esto. `minimum_order_size` (shares) YA NO bloquea -- verificado que no aplica a este tipo de orden (ver seccion mas abajo) -- se sigue registrando solo informativamente. |
| Maximo de operaciones | **1** (TEMPORAL, ver nota) | Normalmente 10 -- bajado a 1 para la prueba puntual de verificacion de ejecucion real pedida el 2026-09-16. Revertir cuando se pida. |
| Capital maximo desplegado | $2.00 mientras MAX_ORDERS=1 | **Derivado** (MAX_ORDERS x MAX_ORDER_USD), nunca un numero elegido aparte |
| Kill switch por perdida | -$10.00 acumulado realizado | |
| Fallos consecutivos | 3 | |
| Antigüedad maxima de snapshot | 1.0s (reloj de pared real al momento del intento, no el instante de decision) | Misma correccion ya validada en `live_micro/`: se re-consulta el book mas fresco al momento REAL del intento, no el snapshot anclado al instante congelado de decision |
| Tipo de orden | Solo FAK, con `max_price` como techo de precio | `place_market_order(side="BUY", amount=$2, max_spend=$2, max_price=precio, order_type="FAK")` -- NUNCA `place_limit_order`. Nunca GTC/GTD, nunca queda abierta |
| Cobertura | Ninguna | |
| Universo | BTC/ETH/SOL, 5 minutos | |
| **Parada automatica** | Tras el primer intento (cualquiera sea su resultado) | `STOP_AFTER_FIRST_ATTEMPT=True`, TEMPORAL -- el proceso se cierra solo, no queda corriendo indefinidamente |
| Envios reales por oportunidad | Hasta 3 (`MAX_LIVE_SUBMISSIONS_PER_MARKET`) | Ver seccion "Reintentos LIVE" abajo. Cada envio sigue contando individualmente contra `MAX_ORDERS`/capital/fallos consecutivos -- mientras `MAX_ORDERS=1` (temporal, fila de arriba), en la practica esto sigue limitado a 1 envio real total por corrida. |
| Slippage maximo vs. paper | $0.02 (`MAX_PAPER_PRICE_SLIPPAGE`) | Si el precio ejecutable del book EN VIVO para el monto completo supera `paper_expected_price + $0.02`, no se envia ninguna orden (no consume cupo de intentos) |

## Reintentos LIVE contra el book en vivo (2026-09-17)

Hasta esta version, el `max_price` de la orden LIVE salia del snapshot mas
fresco guardado en `data.db` por el collector (aunque re-consultado "ahora",
no anclado al instante de decision -- ver seccion siguiente). El unico
intento real completado (2026-09-16, ver mas abajo) no tuvo match, y no se
podia distinguir si fue por movimiento real del book o por vejez de la
foto guardada.

Cambio: cada envio REAL ahora re-consulta el order book del CLOB
DIRECTAMENTE (`live_micro_favorite_order_client.get_live_ask_levels()`,
lectura publica GET `/book?token_id=...`, sin credenciales) inmediatamente
antes de enviar -- nunca usa `orderbook_levels` de `data.db` como cotizacion
final de una orden LIVE (el snapshot de `data.db` se sigue usando solo para
el gate de staleness inicial y como referencia auditable en
`row["snapshot_id"]`/`depth_json`).

Por cada mercado candidato, en `mode="LIVE"`:

1. Se calcula el precio ejecutable del book EN VIVO para el monto completo
   (`$2`, recorriendo los niveles ask devueltos por el CLOB).
2. Si ese precio supera `paper_expected_price + $0.02`, **no se envia nada**
   -- no consume uno de los 3 intentos, y se detiene esta oportunidad por
   completo (no se sigue reintentando indefinidamente).
3. Si esta dentro de tolerancia, se envia FAK con `max_price = min(precio
   ejecutable en vivo + 1 tick, paper_expected_price + $0.02)`.
4. Se detiene inmediatamente en el primer resultado `FILLED`/`PARTIAL`.
5. Si el envio recibio una respuesta LIMPIA de la API sin match
   (`REJECTED` confirmado -- sabemos con certeza que no se lleno), se
   reintenta -- re-consultando el book en vivo desde cero -- hasta un
   maximo de 3 envios reales. Al 3er intento sin fill, se detiene y queda
   registrado `reject_reason` con "agotados los 3 intentos reales sin fill".
6. Si el envio termino en una EXCEPCION (timeout, conexion cortada,
   respuesta malformada -- `ERROR`, corregido 2026-09-17 tras la auditoria
   de `a87f8e5..4e48fdd`, hallazgo #10), el resultado real es DESCONOCIDO
   -- no sabemos si esa orden se envio o incluso se lleno del lado del
   exchange. Este caso **nunca reintenta automaticamente**, sin importar
   cuantos de los 3 intentos queden: reintentar ahi podria mandar una
   segunda orden real mientras la primera sigue con destino desconocido
   (doble gasto real). Se detiene la oportunidad, `reject_reason` queda con
   "resultado DESCONOCIDO tras excepcion... requiere revision manual", y
   `submissions[-1]["outcome_uncertain"]=True` en `api_response_json`.
   Antes de este fix, un `ERROR` se trataba igual que un `REJECTED`
   confirmado y SI reintentaba -- ver Tests, `test_live_exception_during_
   submission_never_retries_outcome_unknown`.

Cada uno de esos hasta-3 envios reales pasa por el kill switch
individualmente (`check_before_order`/`record_attempt`/
`record_execution_outcome`), igual que antes -- ningun envio real se salta
esa verificacion. `row["api_request_json"]`/`api_response_json"` en LIVE
guardan la lista completa de intentos (precio en vivo consultado, max_price
usado, respuesta cruda de cada uno), no solo el ultimo.

Tests: `tests/test_live_micro_favorite.py`, seccion "LIVE: book en vivo +
reintentos" -- cubren el uso del precio en vivo (no el de `data.db`), el
gate de slippage sin consumir intento, los 3 reintentos con parada al
agotarlos, la parada inmediata en el primer fill, que `MAX_ORDERS=1`
(temporal) sigue limitando todo a 1 envio real, y que un book en vivo no
disponible es fail-closed (no se envia nada).

## Verificacion del mecanismo de proteccion de precio real (2026-09-16)

Antes de este cambio, el ejecutor construia ordenes con
`place_limit_order(size=shares, price=...)`, rechazando (`SKIPPED_SIZE_INFEASIBLE`)
cualquier señal donde `minimum_order_size` (en shares) x precio superara $2.
Se verifico -- via lectura de `state/live_trades.jsonl` (15 fills LIVE
reales de `scripts/live_trader.py`) y via inspeccion del codigo fuente real
de `polymarket-client==0.10.0` (instalado en un venv desechable solo para
lectura, nunca en un proceso que corra live) -- que esa regla estaba mal
aplicada:

- **2 de los 15 fills reales tuvieron menos de 5 shares** (1.694914 @ $0.59
  y 2.439025 @ $0.41, `status='matched'`, tx hash real en cadena), en
  mercados que HOY siguen reportando `minimum_order_size: 5` via la API
  publica.
- El codigo fuente de `polymarket-client` (funcion
  `validate_market_order_params`/`prepare_market_order_draft_sync` en
  `polymarket/_internal/actions/orders/market.py`) **nunca** valida
  `minimum_order_size` para ordenes de mercado (`place_market_order`) -- ese
  campo solo aparece en modelos de datos de solo lectura, no en la
  construccion/firma de ninguna orden.
- `place_market_order` (0.10.0) SI soporta `max_spend` y `max_price` de
  forma nativa, y AMBOS quedan matematicamente codificados en
  `maker_amount`/`taker_amount` -- los campos que se firman via EIP-712
  (`_prepare_protected_market_order_draft` -> `_resolve_protected_market_order_price`
  usa `max_price` directo como el precio de la orden; `_resolve_buy_amount_for_fees`
  reduce el monto si `amount + fee_estimada > max_spend`). La proteccion es
  parte de la orden firmada, no solo un chequeo del lado cliente.

**Demostracion concreta** (offline, funciones puras del SDK real, sin red,
sin credenciales, sin `SecureClient`) para `amount=$2, max_spend=$2,
max_price=0.74` (el precio limite real observado en la corrida DRY_RUN
anterior, mercado SOL):

```
maker_amount = 1,960,000  (unidades x10^6 USDC)  =>  $1.960000 gastados como maximo
taker_amount = 2,648,700  (unidades enteras)      =>  2.6487 shares pedidas
precio implicito = maker_amount/taker_amount = 0.739986  (<= max_price = 0.74, exacto)
```

El monto se redujo solo de $2.00 a $1.96 -- automaticamente, por el propio
SDK, para que el gasto total (incluida la fee estimada) nunca supere los $2
pedidos. El precio nunca puede exceder 0.74. Ambos numeros estan **dentro**
de los campos que se firman, no en un chequeo previo separado.

## Comando exacto para reproducir la demostracion (solo lectura, sin credenciales)

```bash
cd /tmp/pm_sdk_inspect && python3 -m venv venv && venv/bin/pip install polymarket-client==0.10.0
# luego llamar validate_market_order_params/_resolve_protected_market_order_price/
# _resolve_buy_amount_for_fees/_compute_market_order_amounts directamente
# (funciones puras, sin ctx/red) -- ver el historial de esta conversacion para el script completo.
```

## Gate LIVE (independiente del de `live_micro/`)

```bash
LIVE_MICRO_FAVORITE=1
LIVE_MICRO_FAVORITE_CONFIRM=I_UNDERSTAND_REAL_MONEY    # frase EXACTA
LIVE_MICRO_FAVORITE_PRIVATE_KEY=...
LIVE_MICRO_FAVORITE_FUNDER=0x...
LIVE_MICRO_FAVORITE_SIGNATURE_TYPE=1   # default 1
```

Sin las cuatro primeras, `LIVE_MICRO_FAVORITE_ENABLED` es `False` y el
proceso corre en `DRY_RUN` pase lo que pase.

## Setup (venv exclusivo, ver README.md)

```bash
cd live_micro_favorite
python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

## Comando exacto: DRY_RUN

```bash
cd live_micro_favorite
nohup venv/bin/python3 run_live_micro_favorite.py \
    > live_micro_favorite.log 2>&1 &
echo $!
```

Verificar: `ps -p <PID> -o pid,etime,command` / `tail -f live_micro_favorite.log`
Detener: `kill <PID>`

## Comando exacto: LIVE (SOLO tras tu confirmacion explicita)

```bash
cd live_micro_favorite
cp .env.example .env   # completar con los valores reales, NUNCA versionar .env
set -a && source .env && set +a
nohup venv/bin/python3 run_live_micro_favorite.py \
    > live_micro_favorite.log 2>&1 &
echo $!
```

Al arrancar en LIVE imprime y loguea la direccion `LIVE_MICRO_FAVORITE_FUNDER`
y el balance real de USDC antes de operar.

## Wallet/saldo -- pendiente de tu eleccion

Igual criterio que `live_micro/`: no se asume ninguna wallet por defecto.
Podes reusar la misma que el otro ejecutor micro-LIVE (si ya la configuraste
ahi, setear aca los mismos valores de `LIVE_MICRO_FAVORITE_*`) o usar una
distinta. El arranque en LIVE imprime el balance real consultado en el
momento -- eso es lo que hay que confirmar antes de dejarlo correr.

## Registro por intento -- "paper y LIVE en paralelo"

Cada mercado que llega a su instante de decision genera UNA fila con AMBAS
cosas juntas, siempre:
- `paper_side`, `paper_expected_price`, `paper_fill_status`: lo que
  `decide_favorite()` (la misma funcion congelada del paper) calcula a
  stake $10 -- equivalente exacto a lo que el paper validator en vivo
  (PID 52466) registraria para esa estrategia en ese mercado.
- El resto de columnas: el intento DRY_RUN/LIVE de este ejecutor a escala
  $2 (precio limite, shares, fill, fee estimado/real, latencia, slippage,
  tx id, motivo de rechazo).

No se depende de la sincronizacion con el proceso del paper validator (que
podria escribir su fila antes o despues) -- `decide_favorite()` es una
funcion pura, asi que recalcularla aca da el mismo resultado exacto que el
paper, sin carrera de tiempos entre procesos.

## Fees estimados (DRY_RUN)

`fee_estimated_usd` usa la formula REAL verificada en
`collector/export_paper_validation.py`
(`estimated_taker_fee_usd()`, importada directo, no reimplementada):
`fee = shares * 0.07 * precio * (1 - precio)`, categoria crypto, solo taker.
Rotulada ESTIMADA -- nunca se confunde con `fee_real_usd` (solo se llena en
LIVE, si la API la informa).

## Tests

```bash
cd live_micro_favorite
venv/bin/python3 -m pytest tests/ -v
```

27 tests: aislamiento del bot viejo y del ejecutor momentum (7), kill
switch/limites puros (9), ciclo DRY_RUN de punta a punta (5), gate LIVE (3),
DB separada (1), varios (2).

## Limitaciones conocidas

- `get_market_meta()` es la unica llamada de red tambien en DRY_RUN
  (lectura publica, sin credenciales). `get_live_ask_levels()` (2026-09-17)
  es otra lectura publica sin credenciales, pero SOLO se llama en LIVE --
  DRY_RUN sigue sin tocar la red para precio/profundidad, usa exclusivamente
  el snapshot de `data.db` (mismo motor que el paper validator).
- El gate de slippage (`MAX_PAPER_PRICE_SLIPPAGE=$0.02`) y el reintento de
  hasta 3 envios reales (`MAX_LIVE_SUBMISSIONS_PER_MARKET`) todavia NO
  fueron probados contra un envio LIVE real -- solo contra tests unitarios
  con respuestas simuladas. El unico envio real completado hasta ahora
  (2026-09-16, seccion de abajo) fue con la logica VIEJA (snapshot de
  `data.db`, un solo intento). Falta una nueva autorizacion explicita para
  probar esta version contra la API real.
- `polymarket-client==0.10.0` **no esta instalado en `collector/venv`**
  (el venv que ejecuta `run_live_micro_favorite.py`) -- solo se instalo en
  un venv desechable separado para la inspeccion de codigo. Intentar LIVE
  hoy fallaria en `from polymarket import SecureClient` dentro de
  `make_client()` con `ModuleNotFoundError` -- una falla SEGURA (no puede
  mover plata sin el paquete), pero hay que instalarlo deliberadamente
  antes de cualquier intento LIVE real. Recomendacion: instalarlo en un
  venv PROPIO de `live_micro_favorite/` (no en `collector/venv`, que
  tambien usan el collector y el paper validator en vivo, para no arriesgar
  esos dos procesos con una dependencia nueva) -- pendiente de tu decision,
  no se hizo todavia.
- Geobloqueo de ejecucion real no verificado desde este Mac -- ver
  `live_micro/docs/LIVE_MICRO.md`.
