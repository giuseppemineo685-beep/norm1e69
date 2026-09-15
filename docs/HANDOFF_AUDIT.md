# HANDOFF — Collector de datos Polymarket (norm1e69)

## OBJETIVO
Construir DOS datasets sincronizados, solo lectura, para (1) inferir la estrategia de
un trader líder y (2) backtestear una estrategia propia después.
PROHIBIDO: copy trading, ejecución de órdenes, simulación de cuenta propia, LIVE=1,
desplegar, borrar datos.

## REPO
URL:     https://github.com/giuseppemineo685-beep/norm1e69.git
main:    47a3facaf9022cce724e0fe8d75c4d691dde5492   (limpio, 8/8 tests si hay red)
rama WIP: audit-fixes-wip @ 861ea59  (2 de 14 puntos del audit, ROMPE TESTS A PROPÓSITO,
          no mergear tal cual — ver "PENDIENTE" abajo)

El ejecutor preexistente scripts/live_trader.py NO se toca ni se importa.

## MAPEO DE WALLETS (verificado con prueba de ida y vuelta)
LÍDER norm1e69:  0x41e2e1ccf1e4940029af02259a31c6b89b9fa354  (proxy wallet)
EJECUTOR funder: 0x25F745698CcE689188fBfbA7B8614981C028680B  (funder == proxy,
                 POLY_SIGNATURE_TYPE=1 → cuenta email/Magic)
La data-api resuelve `user` contra `proxyWallet`, NO contra el EOA firmante.

## ESQUEMA SQLITE (WAL) — collector/data.db
markets, leader_trades, market_trades, orderbook_snapshots, orderbook_levels,
underlying_prices, leader_inventory_timeline, trade_context, leader_poll_log,
collector_events

## ⚠️ HALLAZGO SIN RESOLVER — EL MÁS IMPORTANTE
Informe de 1 hora limpia (3.708 polls, 1,00/s): latencia de detección del trade
**p50=189s, p95=289s, p99=299s, max=303s** (excluyendo el lote de arranque).
El techo en ~300s coincide sospechosamente con la duración de una ventana de 5 min.

Un reporte ANTERIOR mío decía "~1,08s" — eso era una generalización incorrecta desde
UN SOLO trade trazado a mano. No es fiable. El número real y medido es el de arriba.

**No se investigó si el retraso es de la API (publica tarde) o nuestro. Sin resolver
esto, ninguna métrica de timing del dataset es interpretable.** Es la prioridad #1
recomendada antes de seguir con el audit de Codex.
Método sugerido: comparar el timestamp del trade contra el instante en que aparece
por primera vez en `data-api/trades` con muchas muestras, aislando si crece con el
tiempo (símbolo de caché/buffer del lado servidor) o es constante.

## HALLAZGOS EMPÍRICOS YA CONFIRMADOS
1. data-api.polymarket.com tiene CDN que cachea el feed de trades (x-cache HIT,
   age creciente 42→52s). `Cache-Control: no-cache` es IGNORADO. Se evita con un
   parámetro único por request (`_cb`). Validado A/B: no altera paginación, filtros
   ni contenido. clob/book NO está cacheado.
2. `offset` SÍ pagina correctamente en data-api (huecos se recuperan, no solo se alertan).
3. n_returned==100 NO implica pérdida (100 trades cubren ~440s vs poll de 1s).
4. El líder opera 5m Y 15m, más algo de esports. Slug epoch = INICIO de ventana.
5. 97,9% de eventos WS price_change repiten el mismo top-of-book (dedupe aplicado).
6. Duplicados (52 grupos): todos BACKFILL+BACKFILL, causados por token_id=NULL en el
   histórico importado (SQLite trata NULL como distinto en UNIQUE). Filas LIVE: 0 dup.
7. Verificación de muestra: 10/10 trades recientes del líder coinciden campo a campo
   (ts, mercado, outcome, side, precio, shares, txhash) con la API, y 10/10 tienen
   snapshot de order book 0,0–0,6s ANTES del trade. raw_payload == respuesta cruda.

## AUDIT DE CODEX SOBRE 47a3fac — 14 PUNTOS (2 empezados en audit-fixes-wip)
1. Timestamps de order book/subyacente/market-trades: tomar DESPUÉS de cada respuesta
   HTTP individual, no antes de lanzar las requests en paralelo. [ESQUEMA HECHO,
   falta aplicar en orderbook_collector.py/underlying_price_collector.py/market_trades_collector.py]
2. Guardar request_started_at por separado de la recepción. [ESQUEMA HECHO]
3. _executable_price() debe devolver NULL si la profundidad guardada no llena
   target_shares completo. Guardar shares disponibles y fill_ratio. [ESQUEMA HECHO,
   falta la lógica en trade_context.py]
4. VWAP ejecutable de AMBAS piernas (líder y contraria) para el MISMO tamaño objetivo,
   no solo el best ask de la contraria. [PENDIENTE]
5. Recalcular TODO el inventario cuando llega un trade viejo después de uno nuevo
   (actualmente inventory.py no reordena si el orden de llegada no es cronológico). [PENDIENTE]
6. Reconstruir las filas de trade_context dependientes cuando cambia el inventario. [PENDIENTE]
7. Separar paired_edge (matched*1 - matched_cost) de portfolio_floor_pnl
   (min(up,down) - (up_cost+down_cost)). [ESQUEMA HECHO, falta inventory.py]
8. Renombrar/redefinir time_to_hedge_second_leg_s: la primera compra del lado
   contrario no es necesariamente cobertura completa. Agregar tiempo hasta un
   umbral de cobertura explícito. [ESQUEMA HECHO (time_to_coverage_threshold_s),
   falta lógica]
9. Manejar SELL correctamente o excluirlos explícitamente del inventario,
   marcados. [ESQUEMA HECHO (n_sell_trades_excluded), falta lógica en inventory.py]
10. Edades de contexto separadas: libro UP, libro DOWN, subyacente (no una sola
    snapshot_age_s). [ESQUEMA HECHO, falta trade_context.py]
11. Documentar que los timestamps del líder tienen resolución de 1 segundo → el
    orden exacto dentro del mismo segundo es incierto. [PENDIENTE, solo docs]
12. Evitar que la caché de mercados de 10s se pierda el inicio de una ventana nueva
    (markets_collector._CACHE_TTL_S=10). [PENDIENTE]
13. No etiquetar la primera lectura de Binance como precio de apertura real.
    Guardar método y demora de observación. [ESQUEMA HECHO (open_price_method,
    open_price_observation_delay_s), falta poblarlo en underlying_price_collector.py]
14. Actualizar docs/LIMITATIONS.md, agregar pytest a requirements de desarrollo. [PENDIENTE]

Codex pidió explícitamente: tests de regresión que REPRODUZCAN cada punto (no solo
editar los tests existentes), correr el suite completo, reconstruir SOLO las tablas
derivadas (leader_inventory_timeline, trade_context) preservando las fuentes crudas,
correr una hora limpia nueva, pushear con hash para una segunda auditoría.

## ESTADO DEL EJECUTOR LIVE
DETENIDO. Runs 34885985327 y 34924965699 cancelados. live_trader.yml en
disabled_manually. Runner finland-vps busy=false. Posiciones al detenerlo: 0 abiertas,
$0 exposición, 102 residuales resueltas sin valor (realizedPnl −$173,76).
Reversible: gh workflow enable live_trader.yml && gh workflow run live_trader.yml
NO reactivar sin instrucción explícita del dueño.

## ESTADO DE LA RED
El acceso a Polymarket desde la máquina del dueño depende de su VPN (su red real es
suiza, Polymarket bloqueado por GESPA — las conexiones se RECHAZAN sin VPN). Se cae
intermitentemente. El collector sobrevive a estas caídas sin crashear ni duplicar
(reintenta, loguea el error, sigue). Verificar conectividad antes de correr tests que
pegan a la red real: `curl -s -o /dev/null -w "%{http_code}" https://data-api.polymarket.com`

## LIMITACIONES CONOCIDAS
- Subyacente: Binance spot como alternativa documentada a Chainlink (que es lo que
  Polymarket usa para resolver), no probado idéntico.
- maker_taker: NULL salvo que la API lo traiga explícito.
- Fees/rebates: no calculados.
- Order book histórico previo al collector: no existe, no se inventa
  (context_available=0, tolerancia 2,5s).
