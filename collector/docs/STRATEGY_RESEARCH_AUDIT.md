# Auditoría metodológica de la investigación V1/V2/V3 y plan para Strategy V4

Rama: `fable-strategy-research` (creada desde `market-data-validation`, que es
superconjunto de `leader-history-backfill`). Auditoría hecha **leyendo el
código**, sin acceso a `data.db`, `paper_validation.db`, procesos locales ni
`.env`. Ningún número del handoff se toma como verdad: se audita **cómo se
calculó**. Los números en sí quedan `CANNOT VERIFY WITHOUT DATA` hasta que
corra el analizador local (`collector/trader_strategy_research.py`).

Leyenda de veredictos:
- **SUPPORTED BY CODE** — la lógica implementada sostiene la afirmación.
- **METHODOLOGICALLY WEAK** — la lógica existe pero tiene un sesgo o una
  limitación que debilita la conclusión.
- **INVALID** — el código hace algo distinto de lo que la conclusión asume.
- **CANNOT VERIFY WITHOUT DATA** — depende de valores que solo están en las
  bases locales o en la API.

---

## 1. Inventario de código por rama

| Rama | Contenido relevante |
|---|---|
| `main` @ `10720cb` | collectors (`leader_trades_collector`, `orderbook_collector`, `underlying_price_collector`, `markets_collector`), `backfill.py` (viejo), `inventory.py`, `trade_context.py`, `db.py`, `docs/HANDOFF_AUDIT.md` (14 puntos de Codex). |
| `leader-history-backfill` @ `2b0b14f` | `leader_history_backfill.py` (ledger crudo + canonicalización), `export_leader_history_excel.py` (P&L por mercado/categoría), tablas `backfill_*`, `leader_trades_raw`, `leader_trades_v2`, `leader_trade_raw_links`. |
| `market-data-validation` @ `c48a6a2` | todo lo anterior + `build_strategy_dataset.py`, `inventory_pair_analysis.py`, `backtest_momentum_v1.py`, `audit_strategy_v1.py`, `backtest_v2_inventory.py`, `backtest_v3_inventory.py`, `reconcile_v1_v2_findings.py`, `rebuild_trade_context.py`, paper validator (`run_paper_validation.py`, `paper_validation_db.py`, exports), `reports/market_data_diagnostic.md`. |
| `live-micro-favorite` @ `87699f0` | lo anterior + `live_micro_favorite/` (ejecutor LIVE). **No se toca en esta rama.** |
| `audit-fixes-wip` @ `861ea59` | solo esquema (columnas nuevas en `db.py`, `get_book_with_meta`) para los 14 puntos; sin lógica, rompe tests a propósito. |

---

## 2. Hallazgos por componente (con referencia al código)

### 2.1 Ingesta de fills del líder

- **LIVE** (`leader_trades_collector.py`): poll 1/s a `data-api/trades?user=`, inserta en `leader_trades` con `collection_method='LIVE'`, `is_startup_batch` marca el primer poll (historia previa). `api_received_at` = recepción real de la respuesta (`_get_with_meta`, `polymarket_api.py:59-60`). UNIQUE `(wallet, tx_hash, ts, token_id, shares)`.
- **Backfill viejo** (`backfill.py`): importa `state/live_trades.jsonl` con `token_id=NULL` y `side='BUY'` fijo (`backfill.py:68`), y pagina `data-api` con una heurística `hashes <= seen_hashes` (`backfill.py:127`). Como la UNIQUE incluye `token_id`, las filas con `token_id NULL` **no colisionan** con las LIVE del mismo fill → duplicados BACKFILL+LIVE/BACKFILL+BACKFILL (los "52 grupos" del `HANDOFF_AUDIT.md`). **SUPPORTED BY CODE** que el mecanismo produce duplicados.
- **Backfill v2** (`leader_history_backfill.py`): ledger crudo append-only + `canonicalize()` multiset (multiplicidad = máximo visto **dentro de una misma respuesta**, `:303-315`). Distingue observación repetida de fill legítimo repetido (`dedup_ambiguous`). Detecta el límite duro `offset>10000` (`_documented_offset_limit`). **SUPPORTED BY CODE**. Es la fuente canónica correcta del histórico.
- **Solape histórico+LIVE**: solo `export_leader_history_excel._combined_unique_trades` lo resuelve (clave `(tx, round(ts), price, shares, side)` multiset, `:342-368`). `inventory.py`, `trade_context.py` y `build_strategy_dataset.py` **no** lo usan: leen `leader_trades` crudo (LIVE + BACKFILL viejo + startup).

### 2.2 `inventory.py` → `leader_inventory_timeline`

- **Ignora `side`**: `inventory.py:73-78` suma `shares` a `up_shares`/`down_shares` sin mirar si es BUY o SELL. Un SELL se contabiliza como compra. **INVALID** para cualquier mercado con SELL (punto 9 del audit de Codex, reconocido como pendiente).
- Lee `leader_trades` completo (`:33-37`): incluye BACKFILL viejo con `token_id NULL` (duplicados) y startup batch. **INVALID** para los mercados afectados.
- Llegadas fuera de orden: recalcula el estado desde cero pero solo **inserta** filas faltantes (`INSERT OR IGNORE` + `if t["id"] in already: continue`, `:80-81`); las filas ya escritas para trades posteriores no se corrigen. **METHODOLOGICALLY WEAK** (punto 5 de Codex).
- `guaranteed_profit = matched − matched·(vwap_up+vwap_down)` (`:94-95`): atribución **VWAP histórica**, no coste simultáneo ejecutable. Como contabilidad de la porción emparejada es correcta; como "arbitraje" es engañosa (el propio docstring lo reconoce). **WEAK** si se interpreta como estrategia.
- `time_to_hedge_second_leg_s` = primer trade del lado contrario, no cobertura (`:97-99`). **WEAK** (punto 8).

### 2.3 `trade_context.py`

- Anti-look-ahead: desde `c8813ee` exige `event_ts <= t` **y** `received_ts <= t` (`_nearest_snapshot`, `:52-61`). **SUPPORTED BY CODE**. Que la tabla real se haya reconstruido con esta regla (`rebuild_all`) **CANNOT VERIFY** (`reconcile_v1_v2_findings.py:3` corre contra `/tmp/rebuild_test.db`, una copia).
- `_executable_price` (`:100-123`): el docstring dice "returns None if there isn't enough depth" pero el código **devuelve el VWAP parcial** de lo que sí se llena. Subestima el coste para tamaños grandes. **INVALID** para afirmaciones sensibles al tamaño (punto 3 de Codex).
- `opposite_leg_price = best_ask_other` (`:215`): top-of-book, no ejecutable para el mismo tamaño. `combined_cost_to_pair` = VWAP parcial propio + best ask contrario → **sesgado a la baja**. **WEAK/INVALID** para contar "pares < $1" (punto 4).
- Subyacente: `underlying_price_collector.py:66` guarda `source_timestamp_utc = received_at_utc = now` tomado **antes** de lanzar las 3 requests en paralelo (`:46-48`). El dato se registra como disponible hasta ~400 ms antes de estarlo. **WEAK** (punto 1/2). Lo mismo para el REST del order book (`orderbook_collector.py:69,94`: un `now` para 12 requests).
- `usable_for_strategy_learning` excluye BACKFILL, startup y el hueco de 96,8 min (`:235-238`). **SUPPORTED BY CODE**.
- Timestamp del líder con resolución de 1 s: el orden dentro del segundo se decide por `id` de inserción (`ORDER BY source_timestamp_utc, id`). **WEAK** para identificar la primera pierna cuando UP y DOWN caen en el mismo segundo (punto 14).

### 2.4 `build_strategy_dataset.py` + `inventory_pair_analysis.py` (los "3.358 trades limpios")

- El dataset limpio es, por construcción, solo trades del líder con contexto (`WHERE tc0.usable_for_strategy_learning = 1`). Correcto para describir al líder; **INVALID** como universo para evaluar una estrategia (selección de mercados operados por él).
- `classify_and_reconstruct` reconstruye el inventario **solo con los trades limpios** (`inventory_pair_analysis.py:46-55`). Si el primer fill real del mercado quedó fuera (sin contexto, en hueco, startup), el primer fill limpio se etiqueta `A_FIRST_LEG` sin serlo, y todos los `up_before/down_before` están mal. **INVALID** para FIRST_LEG/SURPLUS/IMBALANCE en esos mercados.
- Vuelve a **ignorar `side`** (`:105-108`): un SELL suma inventario. **INVALID** en mercados con SELL.
- Usa `combined_cost_to_pair` (sesgado, 2.3) para `IMBALANCE_REDUCING_BUY` y sus buckets de coste. **WEAK**.
- No usa el winner para clasificar (`:11-16`). **SUPPORTED BY CODE**.

### 2.5 `export_leader_history_excel._per_market_pnl` (P&L del líder, categorías `equilibrado_vwap_suma_lt_1`, etc.)

- Dedupe histórico+LIVE correcto (2.1). Trata SELL como reducción de shares y suma proceeds (`:403-416`). Separa pendientes (`pending_surplus_exposure_usd`). **SUPPORTED BY CODE** en esos puntos.
- **BUG de P&L con SELL**: `gross_pnl_estimated = paired + surplus_pnl + sell_proceeds_total` (`:450`), donde `paired`/`surplus` usan el coste de las shares **netas**. El coste de las shares vendidas no se resta en ningún lado, pero los proceeds sí se suman. Ejemplo: BUY 10 Up @0.5 (5 $), SELL 10 Up @0.6 (6 $) → net 0 → `gross = 0 + 0 + 6 = 6`, real = 1. **INVALID**: el P&L bruto del líder queda **sobreestimado** en `Σ shares_vendidas × vwap` para cada mercado con SELL. (`Data Quality.sell_count` dirá cuánto pesa.)
- Categorías VWAP<1: contabilidad **SUPPORTED**; inferencia "arbitraje simultáneo" **INVALID** (el propio handoff ya concluyó que los pares se armaron en instantes distintos).
- `_ensure_market_resolution` hace **red y escribe** en `markets` (`:317-336`). El "export" no es de solo lectura.
- ROI del líder con sizing variable vs ROI de estrategias a $10 fijo: **no comparables** (capital desigual). **WEAK** si se comparan.

### 2.6 Strategy V1 (`backtest_momentum_v1.py`, `audit_strategy_v1.py`)

- Universo: todos los mercados BTC/ETH/SOL 5 min resueltos con datos, no solo los del líder. **SUPPORTED BY CODE**.
- Anti-look-ahead con ambas condiciones. **SUPPORTED**. Subyacente con el sesgo de 2.3. **WEAK**.
- Split cronológico 60/40; umbral elegido en TRAIN por `max gross_pnl` con n≥20; TEST evaluado con umbral congelado. **SUPPORTED** como procedimiento. **WEAK** porque: (a) no hay VALIDATION — el mismo TEST se reutiliza luego en V2, V3 y para diseñar las reglas del paper; (b) un solo día (~16 h, `export_strategy_v1_excel.py:258`); (c) 63 trades en TEST; (d) el patrón se descubrió mirando al líder (reconocido en `:270-287`, donde además se observa que la baseline **sin umbral** iguala o supera a V1).
- `gather_candidates` descarta mercados sin book del lado señalado en vez de contarlos como no-trade (`:97-100`); el embudo de `audit_strategy_v1` lo cuantifica después. **WEAK** menor.
- `simulate` asume fill al best_ask; `audit_strategy_v1.walk_executable` recalcula con profundidad real (FULL/PARTIAL/SKIPPED). **SUPPORTED**.
- `market_open_reference_price` = primera lectura de Binance tras detectar la ventana (`underlying_price_collector.py:19-38`), no el open oficial; y el umbral 0,02 % está en el orden del ruido de muestreo. **WEAK** (punto 13).
- Sin fees, sin slippage, sin latencia de orden. **WEAK**.

### 2.7 Strategy V2/V3 (`backtest_v2_inventory.py`, `backtest_v3_inventory.py`)

- Universo completo, split 60/40, calibración en TRAIN, motor único con profundidad real, monitoreo cada 5 s con snapshots ≤ t. **SUPPORTED BY CODE**.
- `V2B_FROZEN_THRESHOLD_PAIR = 1.05` (`backtest_v3_inventory.py:20`): "completar el par" a coste combinado 1,05 **fija una pérdida** en la porción emparejada. Que la calibración lo eligiera indica que maximizó P&L de TRAIN por otra vía (cubrir mercados que iban a perder). **METHODOLOGICALLY WEAK / sobreajuste**.
- V3 con 0,99 "fijo": el valor se fijó **después** de ver V2 y se evaluó sobre el mismo TEST. **WEAK** (reutilización de TEST).
- `aggregate` sin fees ni slippage. **WEAK**.

### 2.8 `reconcile_v1_v2_findings.py`

- Separa explícitamente las tres nociones de coste (VWAP histórico / marginal top-of-book / ejecutable de dos piernas, `:75-82`). **SUPPORTED**. Pero opera sobre el dataset limpio (2.4) y su `section4` (peak vs final) ignora SELL. **WEAK**.
- `section5` (z-test de FIRST_LEG vs 50 %): estadístico correcto, muestra de primeras piernas contaminada por 2.4. **WEAK**.

### 2.9 Paper validator (`run_paper_validation.py`, exports)

- Forward en tiempo real, fail-closed, `mode=ro` sobre `data.db`, anti-look-ahead por construcción, hedge check cada 5 s, P&L de resolución incluye la cobertura (`resolve_pending`, `:280-290`). **SUPPORTED BY CODE**.
- `MOMENTUM_PARTIAL_HEDGE` exige `opp_exec <= 0.10` **y** `combined <= 0.90` (`:47-48`): casi nunca se cumple → por construcción es ≈ `MOMENTUM_PURE`. Que "no mejore a Momentum" es estructuralmente esperado, no evidencia sobre cubrir en general. **WEAK** como conclusión general.
- Fee: `export_paper_validation.py` usa `0.07·p·(1−p)` (taker crypto); `docs/PAPER_VALIDATION.md:189` todavía dice 0 %. Documentación desactualizada; la tasa **CANNOT VERIFY** (externa). Maker/taker del líder: `NULL` en todos los fills auditados.
- Cohorte `PRE_VPN_RECOVERY` etiquetada sin reiniciar el proceso. **SUPPORTED**.
- Muestra (horas/mercados) **CANNOT VERIFY**; con ~40 entradas por estrategia cualquier diferencia de ROI de pocos puntos es ruido. **WEAK**.

### 2.10 Collectors y calidad de datos (`reports/market_data_diagnostic.md`)

El diagnóstico ya documentaba: 22 % de contextos con look-ahead de recepción (corregido después), timestamps REST/Binance previos a la respuesta, huecos de 53–96 min, `maker_taker` nulo, ~60 % de fills del líder por debajo del ask previo (compatible con maker), 288 reconexiones WS, 68 k errores REST. Estos hechos sostienen que **los fills del líder no son reproducibles como taker** con los datos actuales. **SUPPORTED BY CODE** que la instrumentación necesaria (`request_started_at`, `get_book_with_meta`) solo existe como esquema en `audit-fixes-wip`.

---

## 3. Checklist de sesgos pedido

| Sesgo | Dónde aplica | Veredicto |
|---|---|---|
| Look-ahead | order book: corregido (`trade_context`, V1–V3, paper). Subyacente y REST: timestamp de inicio de request. Ties de 1 s del líder. | SUPPORTED (book) / WEAK (subyacente, ties) |
| Solo mercados operados por el líder | análisis descriptivos (2.4, 2.5, 2.8) sí; V1–V3 y paper usan universo completo. V1 descubierto mirando al líder. | INVALID para evaluar estrategia con 2.4 / SUPPORTED en V1–V3 / WEAK (origen del patrón) |
| Duplicados histórico+LIVE | resuelto en `_combined_unique_trades` y `canonicalize`; **no** en `inventory.py`, `trade_context`, dataset limpio. | SUPPORTED / INVALID según componente |
| Observaciones vs fills | `canonicalize` multiset por respuesta. `backfill.py` viejo heurístico. | SUPPORTED / WEAK (viejo, excluido) |
| VWAP histórico vs coste simultáneo | `guaranteed_profit`, categorías VWAP<1, buckets de `combined_cost_to_pair`. | WEAK (confundidos; solo `reconcile` los separa) |
| Order books antiguos | tolerancia 2,5 s en todos. `WS_price_change` sin profundidad → `indicative`. | SUPPORTED |
| Datos recibidos tras la decisión | book: corregido. Subyacente: no (≤ ~0,4 s). | SUPPORTED / WEAK |
| Universo incompleto | solo ventanas mientras el collector estuvo vivo; 15 min excluidos; huecos. | WEAK (documentado) |
| Huecos de VPN | cohorte paper; embudo V1 cuenta `hueco_vpn`; `open_reference_price` sembrado tras el hueco. | SUPPORTED / WEAK |
| Resolución futura para clasificar | `inventory_pair_analysis` no la usa; `traded_side_won` solo descriptivo. | SUPPORTED |
| Fees incorrectas | V1–V3: ninguna. Paper: 0,07 asumido. Líder: maker/taker desconocido. | WEAK / CANNOT VERIFY |
| Slippage / latencia de orden | ninguno más allá del book. | WEAK |
| Profundidad insuficiente | V1-audit/V2/paper: FULL/PARTIAL/SKIPPED. `trade_context`: VWAP parcial silencioso. | SUPPORTED / INVALID |
| Distinto conjunto de `condition_id` | V3 `common_markets`, paper "mismos condition_id". | SUPPORTED |
| Capital desigual | estrategias $10 fijo vs líder variable; `reconcile.section4` compara ROI a capital equivalente. | WEAK si se comparan / SUPPORTED en section4 |
| Optimizar y evaluar en la misma muestra | TRAIN/TEST sí; sin VALIDATION; TEST reutilizado V1→V2→V3→paper; 0,99 y 1,05 elegidos con TEST a la vista. | WEAK |
| Muestra pequeña | 1 día, 63 trades TEST, ~40 entradas paper. | WEAK |
| P&L con BUY/SELL | `inventory.py` y dataset limpio ignoran SELL; `_per_market_pnl` omite el coste de lo vendido. | INVALID |
| Pendientes tratados como cero | `_per_market_pnl` los separa; paper resuelve solo con winner. | SUPPORTED |
| FIRST_LEG / SURPLUS / IMBALANCE mal clasificados | reconstrucción desde el subconjunto limpio + SELL ignorado + ties. | INVALID en mercados afectados |

---

## 4. Conclusiones previas, clasificadas

| # | Conclusión (handoff / docs) | Veredicto | Motivo |
|---|---|---|---|
| C1 | ~10.071 trades históricos + LIVE, deduplicados | SUPPORTED (mecanismo) / CANNOT VERIFY (cifra) | `canonicalize` multiset; el conteo está en la DB. |
| C2 | Histórico limitado por `offset` 10.000 (~14 h) | SUPPORTED / CANNOT VERIFY (horas) | `_documented_offset_limit`. |
| C3 | "El arbitraje simultáneo UP+DOWN no era toda la estrategia; los pares VWAP<1 se armaron en instantes distintos" | METHODOLOGICALLY WEAK | Dirección plausible, pero la evidencia usa `combined_cost_to_pair` sesgado a la baja, inventario del subconjunto limpio y SELL como BUY. Hay que rehacerlo con coste ejecutable de dos piernas (el analizador lo hace). |
| C4 | "Cerrar el gap desplegó más capital sin mejorar ROI" | WEAK | Descriptivo, subconjunto limpio, sin SELL, sin contrafactual. |
| C5 | "FIRST_LEG del líder sin patrón claro momentum/barato (z≈0)" | WEAK | Test correcto, muestra de primeras piernas contaminada (2.4). |
| C6 | "PARTIAL_HEDGE no mejora a Momentum" | SUPPORTED (estructural) / WEAK (general) | Por construcción casi nunca cubre. |
| C7 | "Momentum y Favorite se alternan; ninguna estable" | CANNOT VERIFY / WEAK | Comparación en mismos `condition_id` correcta; n pequeño. |
| C8 | "En paper, Favorite algo mejor post-fee" | CANNOT VERIFY / WEAK | Fee asumida; n pequeño. |
| C9 | "Backtest estricto 4 h: −8,41 % / −9,11 % / −6,99 %; 52/144 elegibles" | CANNOT VERIFY | El código que exige cobertura continua hasta `open+240s` **no está en ninguna rama**; no reproducible. |
| C10 | "SOL positivo en subgrupos" | WEAK | Subgrupo pequeño, comparaciones múltiples. |
| C11 | "V1 T=0,02 % elegido en TRAIN, 63 trades TEST" | SUPPORTED (procedimiento) / WEAK (evidencia) | Un día, TEST reutilizado, baseline sin umbral ≈ V1, open ref sesgado. |
| C12 | "V2 umbral de par 1,05 congelado" | WEAK / sobreajuste | Completar par >1 fija pérdida; elegido por P&L de TRAIN. |
| C13 | "V3 0,99 fijo: sin pares en TEST" | SUPPORTED (regla) / CANNOT VERIFY (resultado) | Umbral fijado tras ver V2. |
| C14 | "Latencia de detección del líder p50 ≈ 179–189 s" | CANNOT VERIFY (cifra) | Irrelevante para inferir su comportamiento (se usa su `source_timestamp`); fatal para copiar. |
| C15 | "22 % de contextos con look-ahead → corregido" | SUPPORTED (fix) / CANNOT VERIFY (que se reconstruyó `data.db`) | El analizador recalcula contexto por sí mismo. |
| C16 | "9/364 mercados afectados por sueño; fail-closed funcionó" | SUPPORTED (mecanismo) / CANNOT VERIFY (cifras) | |
| C17 | "Fee taker 0,07·p·(1−p) verificada" | CANNOT VERIFY | Externa; docs internas inconsistentes (0 % vs 0,07). |
| C18 | "52 grupos duplicados BACKFILL por `token_id NULL`" | SUPPORTED BY CODE | Y `inventory.py` los consume → `leader_inventory_timeline` INVALID en esos mercados. |
| C19 | P&L bruto del líder por mercado/categoría (`leader_history.xlsx`) | INVALID en mercados con SELL | Coste de lo vendido omitido (2.5). |

---

## 5. Data map (fuentes canónicas y columnas que selecciona el analizador)

**Histórico del líder (canónico):** `leader_trades_v2` con `origin='DATA_API_HISTORY' AND leader_wallet=?` → `id, transaction_hash, condition_id, token_id, outcome, side, price, shares, source_timestamp_utc, dedup_ambiguous`. Trazabilidad a crudo vía `leader_trade_raw_links → leader_trades_raw → backfill_pages` (no se recorre en el análisis, se conserva `source_row_id`).

**LIVE del líder (canónico):** `leader_trades` con `collection_method='LIVE' AND is_startup_batch=0` → mismas columnas + `api_received_at`.

**Excluidos:** `leader_trades` `BACKFILL` (jsonl + `backfill.py --api`), `is_startup_batch=1`. Se reporta cuántas de esas filas **no** quedan cubiertas por la unión canónica (huérfanas).

**Eliminación del solape:** clave natural `(tx_hash, round(ts), condition_id, outcome, side, round(price,6), round(shares,6))`, multiset: el histórico fija la multiplicidad; LIVE sólo aporta lo no cubierto. Estadísticas: `hist_rows, live_rows, live_matched_to_history, live_only, hist_only, hist_dedup_ambiguous`.

**Timestamps disponibles:** líder `source_timestamp_utc` (1 s, de la API), `api_received_at` (recepción real, LIVE). Book `source_timestamp_utc` (WS: del mensaje; REST: `now` previo a las requests) y `received_at_utc_ms` (WS: recepción real; REST: `now` previo). Subyacente `received_at_utc` (= inicio de request). Mercados `open_time_utc/close_time_utc` (del slug), `resolution_time_utc` (cuando el collector lo vio).

**Order book:** `orderbook_snapshots` (`best_bid/best_ask`, `source` ∈ {REST, WS, WS_price_change}) + `orderbook_levels` (solo REST y WS `book`). Regla: `event_ts <= t AND received_ts <= t`, tolerancia 2,5 s; sin niveles = `NO_DEPTH`.

**Subyacente:** `underlying_prices.price, market_open_reference_price, distance_from_open_pct` con `received_at_utc <= t − pad` (`pad` 0,5 s por defecto).

**Mercados/resolución:** `markets.winner` sólo si `resolution_time_utc <= corte` (o NULL, filas antiguas). Universo de estrategia: `asset ∈ {BTC,ETH,SOL} AND window_minutes=5 AND winner ∈ {Up,Down} AND close_time_utc <= corte`.

**Inventario:** **no** se lee `leader_inventory_timeline`; se reconstruye.

**Paper:** `paper_markets` (cohorte `OFFICIAL`), `paper_decisions`, `paper_hedge_fills`, `paper_resolutions`. `paper_hedge_checks` **sólo `COUNT(*)`**.

**Huecos/logs:** `collector_events` (`gap/reconnect/error/start/stop`) + delta >30 s entre snapshots REST consecutivos.

---

## 6. El analizador `collector/trader_strategy_research.py`

Garantías (todas con test): sólo `mode=ro` + `PRAGMA query_only`; sin `init_db`, `ALTER`, `INSERT`, `UPDATE`, `DELETE`; sin imports de red ni de trading ni de `db.py`; sin `.env`; corte único `REPORT_CUTOFF_TS` (`--cutoff-ts` para reproducir exactamente); unión canónica multiset con trazabilidad por fill; anti-look-ahead estricto; inventario propio BUY+SELL (SELL a coste medio, P&L realizado, oversell marcado); pendientes separados.

Por mercado (`trader_market_sequences.csv`): primera/segunda pierna, tiempo entre piernas, cambios de lado, shares y coste por lado, VWAP, matched/surplus/coverage, VWAP combinado, coste **simultáneo ejecutable** de ambas piernas para el tamaño del fill (mediana y mínimo por mercado), distancia del subyacente y spread en la primera pierna, tiempo restante, resolución y descomposición del P&L: `paired (VWAP)` + `residual` + `realized_sell` = `gross` (identidad verificada fila a fila), `portfolio_floor`, fees taker **estimadas** (tasa asumida, `--fee-rate`), exposición pendiente.

Comportamiento (`hypothesis_tests.csv`): barato vs favorito, con/contra momentum a varios umbrales, ambos lados, coverage final, reducir vs ensanchar desbalance, precio <0,5, uso de SELL, coste simultáneo <1 al reducir desbalance, regímenes de |distancia| (terciles), correlaciones de sizing (Spearman), tiempos (cuantiles); por BTC/ETH/SOL. Sólo las hipótesis con nulo 50/50 natural llevan p-valor (binomial exacto) e IC de Wilson; el resto se etiqueta DESCRIPTIVE. Árbol de decisión depth-2 (stdlib) sobre la primera pierna con split cronológico y accuracy vs mayoría.

Candidatas (`strategy_candidates.csv`): universo completo, split cronológico 50/25/25, decisión `open+60s`, $10 caminando profundidad, fees por fill, slippage opcional (1 tick), drawdown, bootstrap del ROI neto (IC 95 %) en TEST, por asset/día y por "mercado operado por el líder sí/no". Baselines: no operar, favorito, underdog, momentum (0,02 congelado), contra-momentum, partial hedge (reglas del paper), construcción de pares (0,99 fijo, y umbral calibrado en TRAIN **elegido en VALIDATION**), momentum con umbral elegido en VALIDATION. Se registra el número de evaluaciones sobre TEST para leer los IC con esa multiplicidad. Comparación con el paper forward (cohorte OFFICIAL, mismos `condition_id`).

**No propone una V4.** Las candidatas son baselines y familias parametrizadas para que la elección se haga con los resultados locales.

---

## 7. Ejecución local (Mac)

```bash
cd /Users/giuseppemineo/Documents/norm1e69
git fetch origin fable-strategy-research
git worktree add ../norm1e69-research fable-strategy-research   # no toca tu checkout actual
cd ../norm1e69-research/collector

# 1) tests (bases sinteticas, ~1 s)
python3 -m pytest tests/test_trader_strategy_research.py -q

# 2) snapshot consistente de las bases vivas (solo lectura sobre el origen; no reinicia nada)
mkdir -p export/research
sqlite3 /Users/giuseppemineo/Documents/norm1e69/collector/data.db ".backup export/research/data_snapshot.db"
sqlite3 /Users/giuseppemineo/Documents/norm1e69/collector/paper_validation.db ".backup export/research/paper_snapshot.db"

# 3) analizador (imprime REPORT_CUTOFF_TS: guardalo para reproducir con --cutoff-ts)
python3 trader_strategy_research.py \
  --collector-db export/research/data_snapshot.db \
  --paper-db export/research/paper_snapshot.db \
  --output-dir export/research
```

Alternativa sin snapshot: pasar directamente `data.db`/`paper_validation.db` (se abren `mode=ro`; con WAL activo puede requerir permisos de lectura sobre `-wal`/`-shm`). Para una primera pasada rápida: `--skip-candidates`. Sensibilidad a fees: `--fee-rate 0`. Duración esperada con ~13 k fills y ~3–4 k mercados: minutos, no horas.

Nada de esto toca `live_micro_favorite/`, `scripts/live_trader.py`, `.env` ni procesos.

## 8. Archivos a devolver (compactos)

1. `strategy_research.md`
2. `strategy_research_data_map.md`
3. `trader_behavior_summary.csv`
4. `hypothesis_tests.csv`
5. `strategy_candidates.csv`
6. `strategy_candidates_calibration.csv`
7. `strategy_candidates_by_asset_day.csv`
8. `trader_market_sequences.csv` (una fila por mercado)
9. `strategy_research.xlsx`
10. La salida de consola del analizador (cutoff, conteos, avisos) y de pytest.

`trader_fills_canonical.csv` (una fila por fill, ~1e4) sólo si hace falta para una pregunta concreta.

## 9. Lo que sigue pendiente hasta tener resultados

- Cuantificar C3/C4/C5 con inventario completo (BUY+SELL) y coste simultáneo ejecutable.
- Decidir si el líder es reproducible como taker (60 % de fills bajo el ask previo, maker/taker NULL): si no lo es, **ninguna** estrategia que copie sus precios es evaluable con estos datos.
- Elegir familia y parámetros de V4 sobre VALIDATION y confirmarla **una vez** en TEST, luego forward en paper — nunca al revés.
