# Diagnóstico de datos de mercado para estrategia y backtest

## Fecha y periodo analizado

- Fecha de auditoría: 2026-09-15.
- Zona horaria utilizada para los rangos: UTC.
- Corte principal de conteos: 2026-09-15 18:38:46 UTC.
- Periodo de `leader_trades`: 2026-09-14 13:20:13 a 2026-09-15 18:37:21 UTC.
- Periodo con order book y subyacente capturados: aproximadamente 2026-09-15 10:43:17 a 18:38:45 UTC.
- La base siguió creciendo durante la auditoría. Los conteos representan un corte y no un cierre definitivo.

## Alcance y restricciones

La auditoría se realizó sobre `collector/data.db` y el código del collector en modo de solo lectura. Se excluyeron de las métricas de contexto:

- startup batches;
- backfill e historial anterior al comienzo de la captura;
- mercados que no fueran Up/Down;
- activos distintos de BTC, ETH y SOL.

No se modificó `collector/data.db`, no se cambió el código del collector, no se ejecutaron órdenes y no se detuvo ni reinició el collector.

## Veredicto

**PARTIALLY READY**.

Los datos permiten analizar el comportamiento del trader, formular hipótesis y diseñar una draft strategy. Todavía no son adecuados para un backtest fiable porque parte del contexto histórico puede haber sido recibida después del instante que pretende representar. También faltan datos necesarios para simular de forma realista las dos piernas, las órdenes maker y los costes de ejecución.

Para el backtest, el estado actual debe considerarse **NOT READY** hasta aplicar y validar las correcciones mínimas de este informe.

## Conteos y rangos temporales

Corte principal: 2026-09-15 18:38:46 UTC.

| Tabla | Filas | Desde | Hasta |
|---|---:|---|---|
| `markets` | 709 | 2026-09-15 02:15:00 | 2026-09-15 18:45:00 |
| `leader_trades` | 5.729 | 2026-09-14 13:20:13 | 2026-09-15 18:37:21 |
| `leader_trades_v2` | 10.071 | 2026-09-15 02:22:00 | 2026-09-15 16:31:58 |
| `market_trades` | 34.138 | 2026-09-14 13:26:46 | 2026-09-15 18:36:39 |
| `orderbook_snapshots` | 1.151.652 | 2026-09-15 10:43:18 | 2026-09-15 18:38:45 |
| `orderbook_levels` | 9.019.483 | 2026-09-15 10:43:18 | 2026-09-15 18:38:45 |
| `underlying_prices` | 75.848 | 2026-09-15 10:43:17 | 2026-09-15 18:38:45 |
| `leader_inventory_timeline` | 5.686 | 2026-09-15 10:43:16 | 2026-09-15 18:37:28 |
| `trade_context` | 51.561 | n.a. | n.a. |
| `leader_poll_log` | 28.530 | 2026-09-15 10:43:16 | 2026-09-15 18:38:45 |
| `collector_events` | 75.259 | 2026-09-15 10:43:16 | 2026-09-15 18:38:37 |

Durante los ocho minutos posteriores al corte crecieron todas las tablas activas salvo `leader_trades_v2`:

| Tabla | Incremento observado |
|---|---:|
| `markets` | +9 |
| `leader_trades` | +68 |
| `leader_trades_v2` | 0 |
| `market_trades` | +723 |
| `orderbook_snapshots` | +28.560 |
| `orderbook_levels` | +225.008 |
| `underlying_prices` | +1.485 |
| `leader_inventory_timeline` | +68 |
| `trade_context` | +612 |
| `leader_poll_log` | +496 |
| `collector_events` | +9 |

`leader_trades_v2` corresponde al backfill histórico y permaneció estática. El resto de las tablas creció directamente o como derivación de la captura activa.

## Mercados y tokens

- 709 `condition_id`.
- 705 mercados cripto Up/Down.
- 1.410 tokens cripto: 705 Up y 705 Down.
- 4 mercados sin activo o ventana, correspondientes a mercados no relacionados.
- 0 mercados con el mismo token para Up y Down.
- 0 errores de mapeo entre `orderbook_snapshots.token_id` y el outcome registrado en `markets`.
- 0 errores entre el token del trade del líder y su outcome.

| Activo | 5 minutos | 15 minutos |
|---|---:|---:|
| BTC | 196 | 63 |
| ETH | 196 | 27 |
| SOL | 196 | 27 |

Se identificaron 43 operaciones del líder relacionadas con esports u outcomes que no son Up/Down. Deben excluirse de cualquier estrategia cripto Up/Down.

## Cobertura real de contexto

Filtro utilizado:

```sql
lt.collection_method = 'LIVE'
AND lt.is_startup_batch = 0
AND lt.outcome IN ('Up','Down')
AND lt.asset_symbol IN ('BTC','ETH','SOL')
```

Corte medido: 3.838 trades elegibles.

| Campo | Trades | Cobertura |
|---|---:|---:|
| Mercado, slug y token identificados | 3.838 | 100,00% |
| `seconds_to_market_close` | 3.838 | 100,00% |
| Libro Up y Down anterior | 3.647 | 95,02% |
| Profundidad anterior de ambos lados | 3.647 | 95,02% |
| Subyacente anterior | 3.720 | 96,93% |
| Precio ejecutable de la pierna propia | 2.919 | 76,06% |
| `combined_cost_to_pair` | 2.911 | 75,85% |

La cobertura de profundidad ejecutable es menor porque los eventos `WS_price_change` contienen best bid/ask, pero no niveles del libro.

### Ejecución para diferentes tamaños

Se seleccionaron únicamente snapshots que cumplían simultáneamente:

```text
event_timestamp <= trade_timestamp
received_timestamp <= trade_timestamp
```

| Tamaño objetivo | Ambos lados llenables |
|---:|---:|
| 5 shares | 77,14% |
| 10 shares | 77,11% |
| 50 shares | 77,01% |
| 100 shares | 76,73% |

Slippage por pierna condicionado a fill completo:

| Tamaño | Mediana | p95 | p99 | Máximo |
|---:|---:|---:|---:|---:|
| 5 | $0,0000 | $0,0000 | $0,0006 | $0,0551 |
| 10 | $0,0000 | $0,0050 | $0,0092 | $0,0750 |
| 50 | $0,0000 | $0,0170 | $0,0350 | $0,1545 |
| 100 | $0,0010 | $0,0275 | $0,0528 | $0,2212 |

Los datos brutos permiten calcular VWAP taker en parte del dataset. La tabla `trade_context` todavía no calcula ambas piernas para el mismo tamaño.

## Latencias

### Detección del trade

Definición: `api_received_at - source_timestamp_utc`.

| Métrica | Segundos |
|---|---:|
| p50 | 179,477 |
| p95 | 293,033 |
| p99 | 440,893 |
| Máxima | 795,263 |
| Valores negativos | 0 |

La latencia habitual está cerca de tres minutos. Los valores extremos superiores coinciden parcialmente con interrupciones de red.

### Procesamiento interno

Definición: `collector_processed_at - api_received_at`.

| Métrica | Segundos |
|---|---:|
| p50 | 0,007 |
| p95 | 1,122 |
| p99 | 7,118 |
| Máxima | 7,119 |
| Valores negativos | 0 |

El procesamiento interno no explica la demora principal. La mayor parte del retraso ocurre antes de recibir la operación desde la API.

### Order book anterior estricto

Se exigió que tanto el timestamp declarado del evento como la recepción local fueran anteriores al trade.

| Pierna | p50 | p95 | p99 | Máxima | Negativas |
|---|---:|---:|---:|---:|---:|
| Up | 0,180 s | 1,179 s | 2,179 s | 2,430 s | 0 |
| Down | 0,180 s | 1,179 s | 2,179 s | 2,430 s | 0 |

### Subyacente anterior

| Métrica | Segundos |
|---|---:|
| p50 | 0,185 |
| p95 | 1,189 |
| p99 | 2,188 |
| Máxima aceptada | 2,455 |
| Valores negativos | 0 |

## Auditoría anti-look-ahead

Los controles superficiales sobre `trade_context` pasaron:

- `snapshot_age_s < 0`: 0.
- Offsets positivos marcados como utilizables: 0.
- Offsets no positivos incorrectamente bloqueados: 0.
- Contextos aceptados con edad superior a 2,5 segundos: 0.

El control basado en recepción real no pasó. La selección actual comprueba que el timestamp declarado por el evento sea anterior, pero no exige que el collector ya hubiera recibido ese evento.

Resultados:

- Aproximadamente 838 trades seleccionan al menos un evento WS recibido después del trade.
- Representan aproximadamente el 22% del contexto reconstruible en el corte auditado.
- Retraso mediano de las piernas afectadas: 1,62 segundos.
- p95: 21,13 segundos.
- p99: 41,71 segundos.
- Máximo: 50,48 segundos.

Ejemplos individuales de mayor gravedad:

| Trade ID | Transaction hash | Retraso de recepción |
|---:|---|---:|
| 166877 | `0x041d2a48a84a7379c0b19742d07d2f47e172a157c0f878b1004da18df035284e` | 50,484 s |
| 166879 | `0x4277bdb22fe4d39174a43f9a49ebb8a2a6d5cf2b6c7bb9b1ca8066b377034571` | 49,248 s |
| 859180 | `0x13162b237330a376330c3299904a0b4b1c2c3c82ef3f99d3b6a71b0e033882db` | 44,885 s |
| 859181 | `0xb746ab7e64e1851f17d5f861631680818c6bac1835516d0ae09ec9cee72d2d35` | 44,388 s |
| 859185 | `0x6e1d38d9eab9e8d7fc1528bb74ec387634ebb7352ef54f2b6e9fbb170ba0ce72` | 43,102 s |
| 859183 | `0x11c44591569527a08123232c35b4e4ceb133ed78a1616e07817573c69fe8f90e` | 43,010 s |
| 859184 | `0x20dd4d96138cb7c7c5b331737bd9d9e0899a71aa9421f50b131c6a3d2df745b1` | 43,007 s |
| 859182 | `0x9bedb1ff31497dd4d231b0ec62f1d8d8a37820f31429ac293483e86c40e71faf` | 42,196 s |
| 166880 | `0xb7684aa197d7b5a956da2529b14639943fc6da0567c5e47d449b5ca94fd576f1` | 41,628 s |
| 859186 | `0x530e96b923f14817f2fd8cd98f3e3ebc4f5ea363e5727d93fa3f87d560878bba` | 40,735 s |

Una selección conservadora que exige simultáneamente timestamp de evento y recepción anteriores todavía encuentra ambos libros dentro de 2,5 segundos para aproximadamente el 94,4% de los trades. Corregir el look-ahead no eliminaría la mayor parte del dataset.

### Timestamp de Binance

En todas las filas observadas:

```text
source_timestamp_utc = received_at_utc
```

Sin embargo, el código toma este valor antes de lanzar o completar las solicitudes paralelas. Por tanto, representa una marca local previa a la respuesta y no una recepción posterior real. La base actual no permite reconstruir la duración de la petición.

## Calidad del order book

- Filas de niveles con precio nulo: 0.
- Filas de niveles con tamaño nulo: 0.
- Precios o tamaños no positivos en `orderbook_levels`: 0.
- Spreads negativos: 0.
- Spreads exactamente cero: 0.
- Snapshots sin bid: 26.762.
- Snapshots sin ask: 26.761.
- REST y WS `book` guardan aproximadamente 17 a 18 niveles por snapshot.
- `WS_price_change` conserva top-of-book, pero no profundidad, como corresponde al payload disponible.

Los valores de profundidad desconocidos se representan como `NULL`. Los ceros de profundidad en `trade_context` pretenden representar un libro completo con ese lado realmente vacío. Falta guardar el `snapshot_id` para demostrar esta procedencia en cada fila.

`context_quality` estaba vacío en todas las filas durante la auditoría. El proceso activo empezó antes de que el código disponible en disco comenzara a poblar ese campo.

## Huecos de captura

Frecuencias configuradas:

- Poll del líder: 1 segundo.
- Snapshot REST del order book: 1 segundo.
- Precio subyacente: 1 segundo.
- `market_trades`: 2 segundos.
- WebSocket: dirigido por eventos, sin frecuencia fija.

| Fuente | p50 | p95 | p99 | Máximo |
|---|---:|---:|---:|---:|
| Poll líder | 1,000 s | 1,010 s | 2,072 s | 1.936,483 s |
| REST global | 0,999 s | 1,949 s | 3,322 s | 5.807,460 s |
| Binance | 0,999 s | 1,891 s | 3,446 s | 3.248,110 s |

Por token REST:

- 6.458 gaps superiores a 2,5 segundos.
- 294 tokens con algún gap superior a 5 segundos.
- Gap máximo dentro de un token: 35,08 segundos.

Minutos completamente sin captura:

| Fuente | Minutos sin datos | Mayor bloque consecutivo |
|---|---:|---:|
| Order book REST | 96 | 96 |
| WebSocket | 96 | 96 |
| Binance BTC | 53 | 53 |
| Binance ETH | 53 | 53 |
| Binance SOL | 53 | 53 |
| Poll líder | 42 | 31 |

Gap principal:

```text
Order book: 2026-09-15 14:58:30 a 16:35:18 UTC, 5.807 segundos
Binance:    2026-09-15 14:58:30 a 15:52:38 UTC, 3.248 segundos
```

Eventos registrados al momento del corte:

- Reconexiones WebSocket: 288.
- Errores REST de order book: 68.212.
- Errores de `market_trades`: 6.229.

Los errores corresponden principalmente a conexiones rechazadas, timeouts y desconexiones WebSocket por `slow consumer`. No existe una secuencia monotónica completa para el WebSocket. `message_seq` suele ser un hash y no permite demostrar que se recibieron todos los eventos.

## Trazabilidad manual de 20 trades recientes

Los 20 trades revisados pudieron enlazarse con `leader_trades`, `markets`, `orderbook_snapshots`, `orderbook_levels` y `underlying_prices`. La selección del snapshot exigió que su evento y su recepción fueran anteriores al trade.

Condiciones de los tres mercados observados:

```text
ETH = 0xe8618ac3f277a8e86451c5fde3144c366eb2775263ce97e43f1315834e1a190f
SOL = 0x347d15393914aee43cbcadda418c882682e37814baf23203a0da07e43e597587
BTC = 0x3e30c85b49fb21aa6ebfc7c4ad315955efa80c02536464cd5e69701771421a45
```

`PC` significa `WS_price_change`. Los valores `NULL` de profundidad y VWAP indican que el snapshot solo contenía top-of-book.

| # | Activo/lado | Precio × shares | Hora UTC | Detección | Cierre | Snapshot/fuente | Edad OB | Bid/ask | Profundidad ask | VWAP | Subyacente/edad |
|---:|---|---:|---|---:|---:|---|---:|---|---:|---:|---|
| 1 | ETH Up | .11 × 10 | 18:42:22 | 3,57 s | 158 s | 1166020 PC | .587 s | .13/.14 | NULL | NULL | 2396,83 / .189 s |
| 2 | ETH Up | .12 × 10 | 18:42:22 | 3,57 s | 158 s | 1166020 PC | .587 s | .13/.14 | NULL | NULL | 2396,83 / .189 s |
| 3 | ETH Up | .12 × 10,833 | 18:42:22 | 3,57 s | 158 s | 1166020 PC | .587 s | .13/.14 | NULL | NULL | 2396,83 / .189 s |
| 4 | SOL Up | .03 × 33,33 | 18:42:21 | 4,57 s | 159 s | 1166010 PC | .025 s | .04/.05 | NULL | NULL | 98,54 / .188 s |
| 5 | BTC Up | .04 × 100 | 18:42:18 | 7,57 s | 162 s | 1165814 WS | .149 s | .05/.06 | 4841,94 | .0600 | 76017,06 / .186 s |
| 6 | BTC Up | .05 × 100 | 18:42:18 | 7,57 s | 162 s | 1165814 WS | .149 s | .05/.06 | 4841,94 | .0600 | 76017,06 / .186 s |
| 7 | BTC Up | .06 × 100 | 18:42:16 | 9,57 s | 164 s | 1165696 REST | .189 s | .03/.04 | 6654,53 | .0400 | 75943,13 / .188 s |
| 8 | SOL Down | .92 × 10,109 | 18:42:09 | 16,57 s | 171 s | 1165175 REST | .184 s | .95/.96 | 578,80 | .9651 | 98,50 / .185 s |
| 9 | ETH Down | .84 × 10 | 18:42:09 | 16,57 s | 171 s | 1165171 REST | .184 s | .87/.88 | 2940,79 | .8800 | 2395,09 / .185 s |
| 10 | ETH Down | .79 × 10 | 18:42:04 | 21,57 s | 176 s | 1164738 PC | .065 s | .85/.86 | NULL | NULL | 2396,90 / .185 s |
| 11 | SOL Down | .91 × 10 | 18:42:01 | 24,57 s | 179 s | 1164439 PC | .168 s | .88/.89 | NULL | NULL | 98,65 / .185 s |
| 12 | BTC Down | .83 × 88,26 | 18:41:58 | 27,57 s | 182 s | 1164259 REST | .082 s | .80/.81 | 4209,70 | .8170 | 76136,28 / .183 s |
| 13 | BTC Down | .82 × 100 | 18:41:58 | 27,57 s | 182 s | 1164259 REST | .082 s | .80/.81 | 4209,70 | .8185 | 76136,28 / .183 s |
| 14 | ETH Down | .73 × 10 | 18:41:52 | 33,57 s | 188 s | 1163821 REST | .188 s | .78/.79 | 2033,78 | .7900 | 2398,25 / .185 s |
| 15 | SOL Down | .87 × 10 | 18:41:49 | 36,57 s | 191 s | 1163555 REST | .185 s | .86/.87 | 991,94 | .8700 | 98,72 / .012 s |
| 16 | BTC Down | .80 × 100 | 18:41:49 | 36,57 s | 191 s | 1163533 PC | .007 s | .78/.79 | NULL | NULL | 76166,35 / .012 s |
| 17 | BTC Down | .81 × 100 | 18:41:49 | 36,57 s | 191 s | 1163533 PC | .007 s | .78/.79 | NULL | NULL | 76166,35 / .012 s |
| 18 | ETH Up | .18 × 10 | 18:41:46 | 39,57 s | 194 s | 1163240 PC | .802 s | .24/.25 | NULL | NULL | 2400,70 / 1.186 s |
| 19 | ETH Down | .83 × 10 | 18:41:45 | 40,57 s | 195 s | 1163201 REST | .187 s | .77/.78 | 2914,80 | .7800 | 2400,70 / .186 s |
| 20 | SOL Down | .91 × 10 | 18:41:43 | 42,57 s | 197 s | 1163075 REST | .184 s | .87/.91 | 1850,10 | .9100 | 98,66 / .185 s |

Transaction hashes, en el mismo orden:

```text
01 0xca6266786f007983661165f8450e8b428f527905b08bb664019915543e3359a7
02 0x23d8e4388be5875800630321a2aefd9de5ddbdb9581f9764de3464dc5f4fc5b7
03 0xa1e85ef299896b5220c7e4c50e8ab69ee7a7ee3137957752ce067176261a503c
04 0xe0d4d2f42a989fb4ca897c3450af94a1984e986531de43158542701737750d29
05 0x9b2cc8c1f53829bf710eb161212e3f3c0e26ad199cd8bec0f4a1b467dcb38532
06 0x2a634f91fd2cb26f32fa4ef706f0f4fa77e3c555e2ef506c703b68e3ea6463d4
07 0x5fa2fb144cb8f69e2494a44649ec831e03449271ff66a0f980f9bc47cd68c380
08 0x4790e59dd0ab2b2b9a737760d09612d6410c71354f246b76b62d78a3ddfcc6a3
09 0xc5c8d40b470a13a5e8a8496ed95e687f9b773583029d0aba0ccc7035c3322d61
10 0xa1e0411c7c336cf8d52fc5603eee01a85ab1d938dc0e3665d2e41fb7cd01cb8c
11 0xdc9c1f3a159bcd1612fa633e0ca7fdf4c53dc77bb01b572c448896faa40badac
12 0x5256c9d2fdae2f207df8149dc1ee8fb33ef94c8b6ed6f644494f7df8ecd0c32b
13 0x94f3c77c003f90f4d90d4e33927c87780d9dc896485531d37461087401fa973e
14 0x9ef12a7ba207a78d34310a86c07a020b9ca90788fef96fde2fb2962d9132869e
15 0x007d469763e6c50a72921c16c0e019ae5d6aec36abdf4b92d44da0e0c265cbf6
16 0x7687f3cdd6e1976df9230f8f37533fcde91f38897e6939175df014a98967c135
17 0xac42426eaa0095b01f0a60ae79e84bfc4d1adc29ca4b6f6144d63b13a78e7b19
18 0x943972e53ddb16cb573b964a846c1fb865f6550825dd35725e868c734db50df0
19 0x1f7eb9eaa57e14a063a07e2f4f6f5b7166c82fd761f8a3e6f2de72d84553ff1d
20 0x256b0f9f53ab0d689ae809a34968e095d546ac77f6e2830d54e7d38ec27abe1b
```

Nueve de los veinte trades tenían únicamente top-of-book. Los veinte tenían subyacente anterior.

## Problemas encontrados, ordenados por gravedad

### Críticos

1. `trade_context` puede utilizar snapshots WebSocket recibidos después del instante del trade.
2. Los timestamps REST y Binance se toman antes de completar la respuesta HTTP.
3. La detección del líder tiene p50 de aproximadamente 179 segundos y p95 de 293 segundos.

### Altos

4. La pierna contraria utiliza best ask, no VWAP ejecutable para el mismo tamaño.
5. No se guardan IDs ni edades separadas de los snapshots Up, Down y subyacente utilizados.
6. `maker_taker` es nulo en todos los trades cripto LIVE auditados.
7. Aproximadamente el 60% de los trades aparece más de medio centavo por debajo del ask anterior. Esto es compatible con órdenes maker o con ambigüedad temporal. Sin maker/taker ni posición en cola esos fills no pueden reproducirse.
8. Existen gaps globales de 53 a 96 minutos.
9. `market_trades` registra miles de errores y no tiene un control de continuidad equivalente al del líder.

### Medios

10. `context_quality` está vacío porque el proceso activo ejecuta código cargado antes de los cambios disponibles en disco.
11. Los eventos `WS_price_change` carecen de profundidad y solo deben usarse como contexto indicativo.
12. El subyacente es Binance spot y no la referencia exacta utilizada para resolver el mercado.
13. La primera lectura de Binance no demuestra el precio oficial de apertura.
14. El timestamp del líder tiene resolución de un segundo. El orden dentro del mismo segundo es incierto.
15. Faltan fees, rebates, latencia de orden, fill parcial y modelo de cola maker.

## Correcciones mínimas propuestas

1. Registrar `request_started_at` y `response_received_at` reales para cada solicitud.
2. Exigir simultáneamente `event_timestamp <= decision_timestamp` y `received_timestamp <= decision_timestamp`.
3. Guardar `snapshot_up_id`, `snapshot_down_id` y `underlying_price_id` en las filas derivadas.
4. Guardar edades separadas para Up, Down y subyacente.
5. Calcular VWAP y fill ratio de ambas piernas para el mismo tamaño objetivo.
6. Devolver `NULL` cuando el libro no pueda llenar el tamaño completo.
7. Separar explícitamente contexto `executable` de contexto `indicative`.
8. Marcar y excluir automáticamente ventanas afectadas por gaps.
9. Investigar la latencia de publicación del feed del líder.
10. Definir modelos separados para órdenes maker y taker.
11. Añadir fees y una hipótesis conservadora de latencia y slippage.
12. Reconstruir únicamente las tablas derivadas después de corregir la lógica.
13. Ejecutar una captura limpia nueva y repetir la auditoría antes del backtest.

## Limitaciones conocidas

- El acceso a Polymarket depende de VPN y sufrió interrupciones durante el periodo auditado.
- El order book histórico solo existe desde el inicio del collector; no puede reconstruirse para el backfill anterior.
- Binance spot es una aproximación y no se ha demostrado idéntico a la fuente de resolución.
- Los eventos `WS_price_change` no contienen niveles completos.
- No hay una secuencia WS monotónica que permita probar exhaustividad.
- `market_trades` devuelve ejecuciones observadas, pero no prueba continuidad cuando hay errores o límites de página.
- Los timestamps del líder tienen resolución de un segundo.
- `maker_taker` no está disponible.
- No se incluyen fees, rebates ni costes de infraestructura.
- El collector permaneció activo mientras se consultaba la base, por lo que los totales cambian con el tiempo.
- El diagnóstico valida disponibilidad y trazabilidad del dataset; no valida todavía una estrategia ni su rentabilidad.

## Consultas SQL utilizadas

Todas las consultas se ejecutaron con SQLite en modo de solo lectura.

### Inventario de tablas

```sql
SELECT 'markets' table_name, COUNT(*) rows,
       datetime(MIN(open_time_utc),'unixepoch') min_time,
       datetime(MAX(close_time_utc),'unixepoch') max_time
FROM markets
UNION ALL
SELECT 'leader_trades', COUNT(*),
       datetime(MIN(source_timestamp_utc),'unixepoch'),
       datetime(MAX(source_timestamp_utc),'unixepoch')
FROM leader_trades
UNION ALL
SELECT 'leader_trades_v2', COUNT(*),
       datetime(MIN(source_timestamp_utc),'unixepoch'),
       datetime(MAX(source_timestamp_utc),'unixepoch')
FROM leader_trades_v2
UNION ALL
SELECT 'market_trades', COUNT(*),
       datetime(MIN(source_timestamp_utc),'unixepoch'),
       datetime(MAX(source_timestamp_utc),'unixepoch')
FROM market_trades
UNION ALL
SELECT 'orderbook_snapshots', COUNT(*),
       datetime(MIN(received_at_utc_ms)/1000,'unixepoch'),
       datetime(MAX(received_at_utc_ms)/1000,'unixepoch')
FROM orderbook_snapshots
UNION ALL
SELECT 'underlying_prices', COUNT(*),
       datetime(MIN(received_at_utc),'unixepoch'),
       datetime(MAX(received_at_utc),'unixepoch')
FROM underlying_prices;
```

### Mercados y tokens

```sql
SELECT COUNT(*) condition_ids,
       COUNT(DISTINCT token_up_id) + COUNT(DISTINCT token_down_id) distinct_tokens,
       SUM(token_up_id IS NULL) missing_up,
       SUM(token_down_id IS NULL) missing_down,
       SUM(token_up_id=token_down_id) same_token
FROM markets;

SELECT COALESCE(asset_symbol,'(NULL)') asset,
       COALESCE(window_minutes,-1) window_minutes,
       COUNT(*) markets
FROM markets
GROUP BY 1,2
ORDER BY 1,2;
```

### Universo elegible y cobertura

```sql
WITH base AS (
  SELECT lt.*
  FROM leader_trades lt
  JOIN markets m USING(condition_id)
  WHERE lt.collection_method='LIVE'
    AND lt.is_startup_batch=0
    AND lt.outcome IN ('Up','Down')
    AND lt.asset_symbol IN ('BTC','ETH','SOL')
)
SELECT
  COUNT(*) eligible_trades,
  SUM(condition_id IS NOT NULL
      AND token_id IS NOT NULL
      AND market_slug IS NOT NULL) identified,
  SUM(seconds_to_market_close IS NOT NULL) has_time_remaining,
  SUM(EXISTS (
    SELECT 1 FROM trade_context tc
    WHERE tc.leader_trade_id=base.id
      AND tc.offset_seconds=0
      AND tc.context_available=1
  )) has_both_books,
  SUM(EXISTS (
    SELECT 1 FROM trade_context tc
    WHERE tc.leader_trade_id=base.id
      AND tc.offset_seconds=0
      AND tc.depth_ask_up IS NOT NULL
      AND tc.depth_ask_down IS NOT NULL
  )) has_depth,
  SUM(EXISTS (
    SELECT 1 FROM trade_context tc
    WHERE tc.leader_trade_id=base.id
      AND tc.offset_seconds=0
      AND tc.underlying_available=1
  )) has_underlying,
  SUM(EXISTS (
    SELECT 1 FROM trade_context tc
    WHERE tc.leader_trade_id=base.id
      AND tc.offset_seconds=0
      AND tc.executable_price_for_leader_size IS NOT NULL
  )) has_own_exec,
  SUM(EXISTS (
    SELECT 1 FROM trade_context tc
    WHERE tc.leader_trade_id=base.id
      AND tc.offset_seconds=0
      AND tc.combined_cost_to_pair IS NOT NULL
  )) has_combined
FROM base;
```

### Latencia de detección y procesamiento

```sql
SELECT
  detection_latency_ms/1000.0 AS detection_s,
  collector_processed_at-api_received_at AS processing_s
FROM leader_trades
WHERE collection_method='LIVE'
  AND is_startup_batch=0
  AND outcome IN ('Up','Down')
  AND asset_symbol IN ('BTC','ETH','SOL');
```

Los percentiles se calcularon sobre el resultado anterior sin modificar la base.

### Reglas básicas anti-look-ahead

```sql
SELECT
  SUM(offset_seconds<=0 AND usable_for_backtest<>1) bad_past_flag,
  SUM(offset_seconds>0 AND usable_for_backtest<>0) bad_future_flag,
  SUM(snapshot_age_s<0) negative_age,
  SUM(context_available=1 AND snapshot_age_s>2.5) available_too_old
FROM trade_context tc
JOIN leader_trades lt ON lt.id=tc.leader_trade_id
WHERE lt.collection_method='LIVE'
  AND lt.is_startup_batch=0;
```

### Selección actual que permite recepción futura

```sql
SELECT *
FROM orderbook_snapshots
WHERE condition_id=:condition_id
  AND token_id=:token_id
  AND COALESCE(
        source_timestamp_utc,
        received_at_utc_ms/1000.0
      ) <= :trade_timestamp
ORDER BY COALESCE(
           source_timestamp_utc,
           received_at_utc_ms/1000.0
         ) DESC
LIMIT 1;
```

### Selección conservadora recomendada

```sql
SELECT *
FROM orderbook_snapshots
WHERE condition_id=:condition_id
  AND token_id=:token_id
  AND COALESCE(
        source_timestamp_utc,
        received_at_utc_ms/1000.0
      ) <= :trade_timestamp
  AND received_at_utc_ms/1000.0 <= :trade_timestamp
ORDER BY received_at_utc_ms DESC
LIMIT 1;
```

### Enumeración de violaciones individuales

```sql
WITH base AS (
  SELECT lt.id trade_id,
         lt.transaction_hash,
         lt.source_timestamp_utc trade_ts,
         lt.condition_id,
         'Up' leg,
         m.token_up_id token_id
  FROM leader_trades lt
  JOIN markets m USING(condition_id)
  WHERE lt.collection_method='LIVE'
    AND lt.is_startup_batch=0
    AND lt.outcome IN ('Up','Down')
    AND lt.asset_symbol IN ('BTC','ETH','SOL')
  UNION ALL
  SELECT lt.id, lt.transaction_hash, lt.source_timestamp_utc,
         lt.condition_id, 'Down', m.token_down_id
  FROM leader_trades lt
  JOIN markets m USING(condition_id)
  WHERE lt.collection_method='LIVE'
    AND lt.is_startup_batch=0
    AND lt.outcome IN ('Up','Down')
    AND lt.asset_symbol IN ('BTC','ETH','SOL')
),
chosen AS (
  SELECT b.*,
         (
           SELECT os.id
           FROM orderbook_snapshots os
           WHERE os.condition_id=b.condition_id
             AND os.token_id=b.token_id
             AND COALESCE(
                   os.source_timestamp_utc,
                   os.received_at_utc_ms/1000.0
                 ) <= b.trade_ts
           ORDER BY COALESCE(
                      os.source_timestamp_utc,
                      os.received_at_utc_ms/1000.0
                    ) DESC
           LIMIT 1
         ) snapshot_id
  FROM base b
)
SELECT c.trade_id,
       c.leg,
       c.transaction_hash,
       c.snapshot_id,
       os.source,
       os.received_at_utc_ms/1000.0-c.trade_ts AS received_after_trade_s
FROM chosen c
JOIN orderbook_snapshots os ON os.id=c.snapshot_id
WHERE os.received_at_utc_ms/1000.0 > c.trade_ts
ORDER BY received_after_trade_s DESC;
```

### Mapeo Up/Down

```sql
SELECT COUNT(*) mapping_errors
FROM orderbook_snapshots os
JOIN markets m USING(condition_id)
WHERE (os.outcome='Up' AND os.token_id<>m.token_up_id)
   OR (os.outcome='Down' AND os.token_id<>m.token_down_id);
```

### Gaps REST por token

```sql
WITH observations AS (
  SELECT token_id,
         received_at_utc_ms/1000.0 ts,
         LAG(received_at_utc_ms/1000.0)
           OVER (
             PARTITION BY token_id
             ORDER BY received_at_utc_ms
           ) previous_ts
  FROM orderbook_snapshots
  WHERE source='REST'
)
SELECT token_id,
       MAX(ts-previous_ts) maximum_gap,
       SUM(ts-previous_ts>2.5) gaps_over_2_5_seconds
FROM observations
GROUP BY token_id;
```

### Calidad de niveles

```sql
SELECT COUNT(*) levels,
       SUM(price IS NULL) null_price,
       SUM(size IS NULL) null_size,
       SUM(price<=0) nonpositive_price,
       SUM(size<=0) nonpositive_size
FROM orderbook_levels;

SELECT
  SUM(depth_ask_up IS NULL) unknown_up,
  SUM(depth_ask_down IS NULL) unknown_down,
  SUM(depth_ask_up=0) known_empty_up,
  SUM(depth_ask_down=0) known_empty_down
FROM trade_context tc
JOIN leader_trades lt ON lt.id=tc.leader_trade_id
WHERE lt.collection_method='LIVE'
  AND lt.is_startup_batch=0
  AND tc.offset_seconds=0;
```

### Trazabilidad de los 20 trades recientes

```sql
SELECT lt.*,
       m.token_up_id,
       m.token_down_id
FROM leader_trades lt
JOIN markets m USING(condition_id)
WHERE lt.collection_method='LIVE'
  AND lt.is_startup_batch=0
  AND lt.outcome IN ('Up','Down')
  AND lt.asset_symbol IN ('BTC','ETH','SOL')
ORDER BY lt.source_timestamp_utc DESC, lt.id DESC
LIMIT 20;
```

Para cada trade se ejecutó la selección conservadora de snapshot mostrada anteriormente, seguida de:

```sql
SELECT price, size
FROM orderbook_levels
WHERE snapshot_id=:snapshot_id
  AND side='ask'
ORDER BY level;

SELECT *
FROM underlying_prices
WHERE asset_symbol=:asset_symbol
  AND received_at_utc<=:trade_timestamp
  AND COALESCE(source_timestamp_utc,received_at_utc)<=:trade_timestamp
ORDER BY received_at_utc DESC
LIMIT 1;
```

El VWAP se calculó recorriendo los niveles ask hasta completar el tamaño objetivo. Si los niveles no completaban el tamaño, el precio ejecutable se trató como desconocido.
