# Validación forward en paper trading

Simulación exclusivamente. No hay, ni puede haber, ejecución real desde este
componente — ver la sección Seguridad más abajo, que no es solo una promesa
en prosa, está verificada por tests.

## Objetivo

Comparar tres estrategias autónomas (ninguna copia ni observa al líder) sobre
**exactamente el mismo universo de mercados nuevos** — mercados BTC/ETH/SOL de
5 minutos que abren *después* de que arrancó este validador, nunca los que ya
se usaron para calibrar/backtestear V1/V2/V3.

## Arquitectura

```
run_collector.py (proceso existente, PID 38099)
        │ escribe en tiempo real
        ▼
collector/data.db  ──(solo lectura, mode=ro)──▶  run_paper_validation.py
                                                          │ escribe
                                                          ▼
                                              collector/paper_validation.db
                                                          │
                                                          ▼
                                          export_paper_validation.py → paper_validation.xlsx
```

`run_paper_validation.py` **nunca recolecta datos por sí mismo** — no hace
ninguna request HTTP, no importa `polymarket_api` ni ningún cliente de red.
Consume lo que el collector en vivo ya está escribiendo en `data.db`, en modo
read-only a nivel de *driver* sqlite (`file:...?mode=ro`), no solo por
convención de código: un `INSERT` contra esa conexión falla con
`sqlite3.OperationalError`, verificado en
`tests/test_paper_validation.py::test_readonly_uri_connection_actually_rejects_writes`.

Toda escritura va a `collector/paper_validation.db`, un archivo nuevo y
completamente separado de `data.db`.

## Reglas congeladas (no se recalibran con los resultados de esta validación)

### A. MOMENTUM_PURE
- Mercados BTC/ETH/SOL de 5 minutos.
- Decisión exactamente 60s después de abrir la ventana.
- Señal: distancia porcentual del subyacente respecto al precio de apertura.
- Opera solo si `|distancia| >= 0.02%` (umbral congelado de V1, nunca
  retocado en esta fase).
- Compra UP si la distancia es positiva, DOWN si es negativa.
- Stake fijo $10, recorriendo profundidad real (nunca asume fill al best ask).
- Mantiene hasta resolución. Nunca compra la pierna contraria.

### B. MOMENTUM_PARTIAL_HEDGE
- Misma entrada que MOMENTUM_PURE.
- Después de la entrada, monitorea continuamente la pierna contraria.
- Ejecuta una **única** cobertura de **exactamente el 25%** de las shares
  iniciales, solo si **las 4 condiciones se cumplen simultáneamente**:
  1. precio ejecutable (recorriendo profundidad) de la pierna contraria `<= $0.10`;
  2. coste medio de la 1ra pierna + precio ejecutable contrario `<= $0.90`;
  3. hay profundidad suficiente para llenar el 25% **completo** (exige FULL,
     nunca PARTIAL — si la profundidad no alcanza, no se cubre, no se
     inventa un fill parcial "porque algo es mejor que nada");
  4. quedan `>= 60s` para el cierre del mercado.
- Si no se cumplen las 4, no cubre — la posición queda 100% direccional.
- Nunca vende, nunca reintenta después del primer intento fallido.

### C. POLYMARKET_FAVORITE_BASELINE
- Decisión a los mismos 60s.
- Compra el lado con **mayor probabilidad implícita** (precio/mid más alto).

  **Nota de interpretación:** el enunciado original decía *"menor best ask /
  mayor probabilidad implícita"*, dos criterios que son opuestos bajo el
  mecanismo real de precios de Polymarket (precio ≈ probabilidad, así que
  mayor precio = mayor probabilidad implícita = favorito; menor precio es el
  *underdog*, no el favorito). Se resolvió usando "mayor probabilidad
  implícita", consistente con cómo se definió "favorito" en absolutamente
  toda la sesión previa (V1, V2 y V3 usan la misma regla). Si esta lectura es
  incorrecta, es trivial cambiar un signo de comparación en
  `decide_favorite()`.

- Stake $10, misma ejecución/profundidad/disponibilidad que las otras dos.
  Mantiene hasta resolución.

## Ejecución simulada

- Profundidad real siempre (`orderbook_levels`, recorrida nivel a nivel) —
  nunca se asume fill al best ask.
- Cada intento queda marcado `FULL`, `PARTIAL` o `SKIPPED`, con motivo
  explícito (`skip_or_partial_reason`) cuando no es FULL.
- **Fail-closed**: si falta cualquier dato (subyacente, order book,
  profundidad), la fila queda `SKIPPED` — nunca se inventa un precio.
- Anti-lookahead: cada snapshot usado exige `received_at_utc_ms` (cuándo
  realmente llegó al collector) `<=` el instante de decisión, la misma regla
  conservadora que `trade_context.py`/V1/V2/V3. El monitoreo de cobertura usa
  el instante real de reloj de pared en que corre cada chequeo — no puede ver
  el futuro porque corre hacia adelante en tiempo real, nunca en modo backtest.

## Esquema de `paper_validation.db`

| Tabla | Contenido |
|---|---|
| `paper_markets` | Un mercado por fila: activo, tokens, apertura/cierre, instante de decisión. |
| `paper_decisions` | Una fila por (mercado, estrategia): señal, lado, precio ejecutable, shares, capital, FULL/PARTIAL/SKIPPED y motivo. |
| `paper_hedge_checks` | Cada chequeo de las 4 condiciones de cobertura (se cubra o no), para trazabilidad completa. |
| `paper_hedge_fills` | La cobertura efectivamente ejecutada (a lo sumo una por decisión). |
| `paper_resolutions` | P&L bruto por (mercado, estrategia) una vez se conoce el ganador. |
| `paper_meta` | `validator_started_at` — el corte de "datos completamente nuevos", fijado la primera vez que corre y nunca reescrito. |
| `paper_events` | Log de arranque/errores/hitos, igual estilo que `collector_events`. |

## Seguridad

- **Prohibido** cualquier import de un módulo de ejecución de órdenes,
  cliente CLOB/wallet, o `scripts/live_trader` — verificado por
  `test_no_trading_imports_or_calls_in_paper_validation_files` (escaneo
  estático de patrones prohibidos, ignorando docstrings/comentarios para no
  generar falsos positivos sobre prosa que *explica* qué no se hace).
- **Prohibido** cualquier import de librería de red (`requests`, `httpx`,
  `websockets`, etc.) — verificado por
  `test_no_network_library_imported_anywhere_in_paper_files`.
- **Prohibido** leer `.env`, `PRIVATE_KEY`, `POLY_PRIVATE_KEY` o cualquier
  credencial — verificado por el mismo escaneo.
- `collector/data.db` se abre exclusivamente en modo `mode=ro` — verificado
  a nivel de driver sqlite (no solo por convención), y por
  `test_run_paper_validation_never_calls_data_conn_without_mode_ro`.
- `collector/paper_validation.db` es un archivo nuevo y separado —
  verificado por `test_paper_db_path_is_separate_from_collector_data_db`.
- `scripts/live_trader.py` no se toca ni se referencia en ningún punto.
- LIVE nunca se activa desde ningún archivo de esta fase.

## Operación

Arrancar (proceso separado, en background):

```bash
cd collector
nohup python3 run_paper_validation.py > paper_validation.log 2>&1 &
echo $!   # PID del proceso
```

Verificar que sigue vivo:

```bash
ps -p <PID> -o pid,etime,command
tail -f collector/paper_validation.log
```

Detener:

```bash
kill <PID>
```

Regenerar el Excel en cualquier momento (funciona incluso con cero
resoluciones todavía):

```bash
python3 export_paper_validation.py
```

## Incidente: caída de VPN al arranque (2026-09-15)

El collector perdió acceso a Polymarket entre **20:44:37 UTC** y **21:28:55
UTC** (errores de `orderbook`/`market_trades`, reconexiones de WS) — la
caída se solapó con el arranque del paper validator (cutoff 21:14:36 UTC),
así que los primeros 9 mercados descubiertos ocurrieron total o
parcialmente durante la interrupción.

Se agregó la columna `paper_markets.validation_cohort` (`tag_vpn_recovery_cohort.py`,
migración aditiva, `ALTER TABLE ... ADD COLUMN ... DEFAULT 'OFFICIAL'` —
segura para un proceso ya corriendo, sin necesidad de reiniciarlo):

- **PRE_VPN_RECOVERY**: mercados con `open_time_utc` anterior al primer
  mercado de 5 minutos **completo** posterior a la recuperación
  (`21:30:00 UTC` — el mercado 21:25-21:30 ya estaba en curso cuando volvió
  el acceso, así que no cuenta como "completo post-recovery"). 9 mercados.
- **OFFICIAL**: todo lo demás, incluidas todas las filas nuevas que el
  proceso siga insertando de acá en adelante (el `DEFAULT` de la columna ya
  resuelve esto sin tocar código).

Nada se borró. `export_paper_validation.py`: la pestaña **Signals** y
**Resolutions** muestran ambas cohortes (con la columna visible); la
pestaña **Strategy Summary** (P&L/ROI/drawdown oficiales) filtra
exclusivamente a `OFFICIAL`.

## Pestañas de reporte añadidas (2026-09-16, capa de export únicamente)

- **Strategy Summary**: además del P&L/ROI bruto, ahora muestra
  `pnl_after_fees_usd`/`roi_after_fees_pct` — una ESTIMACIÓN separada
  (`OFFICIAL_FEE_RATE_ASSUMPTION` en `export_paper_validation.py`, hoy 0%
  porque el CLOB de Polymarket no cobra fee de trading estándar en órdenes
  market/limit según documentación pública, no verificado en vivo). Nunca
  toca `paper_decisions`/`paper_resolutions`. Nota explícita: las 3
  estrategias son experimentos independientes, **nunca sumar sus filas
  como rendimiento de una cartera combinada**.
- **Momentum vs Favorite**: MOMENTUM_PURE vs POLYMARKET_FAVORITE_BASELINE
  restringido a la intersección exacta de `condition_id` donde ambas tienen
  resolución OFFICIAL (no sus universos completos por separado, que
  difieren porque momentum exige señal y favorito no). Incluye desglose
  BTC/ETH/SOL y detalle mercado por mercado.
- Ningún cambio a `run_paper_validation.py` — reglas congeladas, proceso
  vivo sin reiniciar.

## Limitaciones conocidas

- Un solo checkpoint de validación forward — cuantos más mercados nuevos
  pasen por el validador antes de mirar `Resolutions`, más confiable es la
  comparación. Con pocas resoluciones, tratar todo como evidencia preliminar.
- El chequeo de cobertura corre cada `POLL_INTERVAL_S=5s`, no tick a tick —
  puede perderse una ventana de oportunidad más corta que eso.
- Igual que en V1/V2/V3: sin fees, sin rebates, sin redención real
  on-chain, sin modelar riesgo de que el book se mueva entre la decisión
  simulada y una orden real.
