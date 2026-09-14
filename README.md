# Polycool Strategy

Sistema propio de copy-trading en **papel** (sin plata real) de la wallet de
Polymarket `0x3048d65321be3497164cdfc2996f94f98a2e7537` (alias
"x-MoneyForWhiskas", antes "@justoneofmystrategies").

Construido para no depender de la app Polycool: sin su 1% de fee por trade,
y con el filtro de convicción que esa app no ofrece.

## Reglas (validadas por backtest sobre ~50h reales de datos)

- Solo copia si el trade individual de la wallet cuesta **≥$20** (su nivel
  de convicción real)
- Mirror **15%** del costo TOTAL que ella lleva invertido en un mercado
  (ambos lados, Up+Down, juntos), con un tope de **$20 al total del
  mercado** que preserva su proporción real entre los dos lados
- Nunca opera por debajo de **$1** (mínimo real de Polymarket) ni sin
  efectivo suficiente
- El precio de entrada de cada posición es un promedio ponderado de todos
  los incrementos que se le fueron agregando
- Capital inicial en papel: **$600**

Con este capital, el backtest histórico nunca tocó el piso de $1 (mínimo
$1.63) — niveles menores ($300-500) sí llegaron a romperse o quedar al
borde por una mala racha temprana.

### Bug real encontrado y corregido (2026-09-14)

La cuenta real (con $200, misma configuración) perdió ~20% en ~1h.
Investigando, encontramos que el tope de $10 se estaba aplicando **por
lado (Up/Down) de forma independiente**, no al total del mercado. Cuando
ella tenía una convicción muy asimétrica (ej. 77%/23% o 87%/13% entre Up y
Down), ambos lados podían superar individualmente el tope de $10 y
terminábamos comprando **$10 en cada lado = 50/50**, sin importar cuál era
su lado dominante real. Esto aplana su señal direccional en cualquier
mercado donde su volumen total es grande.

Arreglo: el tope ahora aplica al TOTAL de ambos lados del mercado, y la
plata se reparte entre Up/Down preservando exactamente su proporción real.

**Validación con datos reales — la trampa de la muestra chica:** al
probar el arreglo contra los 612 trades reales de la hora exacta del
incidente (10:22–11:28, solo 14 mercados), el resultado dio **negativo**
(-10% a -20%), peor que la lógica vieja. Antes de asumir que el arreglo
estaba mal, lo probamos contra 24h reales completas (288 mercados
resueltos, resolución verificada en `clob.polymarket.com`):

| | Su cuenta real | Copia (tope $10) | Copia (tope $20) |
|---|---|---|---|
| PnL sobre 24h | +2.35% a +3.33% | +1.84% | +2.58% |

Sobre la muestra representativa, la lógica corregida **sí reproduce la
misma dirección que su cuenta real** (positivo, comprimido por el tope,
como se espera). El resultado negativo de la hora del incidente fue ruido
de muestra chica: esa hora tenía un solo mercado gigante (~$1,158 en
apuestas de ella) que ganó — sin tope casi todo el capital iba ahí; con
tope, ese capital se reparte hacia otros mercados más chicos de la misma
hora que, por azar, perdieron. Con solo 14 mercados un solo resultado
domina todo. Aparte de eso, la demora real de copia (precio del mercado
en el momento en que detectamos el trade, no el precio que ella consiguió)
cuesta genuinamente ~2-3 puntos porcentuales — eso sí es real, no ruido, y
ya está reflejado en el modelo.

Conclusión: el arreglo queda en pie, validado sobre datos reales
representativos, pero **ninguna ventana de una sola hora alcanza como
prueba** — de ahí que el sistema siga en papel, acumulando snapshots cada
10 minutos, antes de considerar otra vez plata real.

## Cómo mide la demora

No asume un número de segundos fijo. Cada vez que detecta un trade nuevo de
la wallet, mide el delay real (su timestamp vs el instante de detección) y
usa el precio real vigente en el mercado en ESE momento como precio de
llenado — así la demora y el slippage quedan reflejados con datos reales.

## Estructura

- `scripts/papertrader.py` — el bot: detecta, filtra, simula la copia,
  registra estado y log.
- `scripts/generate_dashboard.py` — genera `docs/index.html`.
- `scripts/run_bounded.sh` — corre ambos en loop acotado (~5h40m, para
  GitHub Actions) con auto-publish a git cada 60s.
- `.github/workflows/papertrader.yml` — dispara `run_bounded.sh` cada 6h
  automáticamente, para que corra 24/7 sin depender de ninguna PC prendida.
- `state/paper_state.json` — estado (cash, posiciones abiertas, contadores).
- `state/paper_trades.jsonl` — log append-only de cada apertura/cierre.

## Bot en vivo (plata real) — copiando a norm1e69

Después de cerrar el experimento con Polycool (caja negra que no se pudo
validar por backtest — ver sección anterior), se construyó un ejecutor
propio que habla directo con la API de Polymarket (`py-clob-client`), sin
intermediarios ni fee del 1%. Copia a **norm1e69**
(`0x41e2e1ccf1e4940029af02259a31c6b89b9fa354`), verificada como ganadora
consistente ($87 → $100,610 en 39 días, curva oficial de Polymarket).

Misma lógica de proporción por mercado que el paper trader, pero
recalibrada a su escala (sus trades son mucho más chicos: mediana ~$5-7,
no ~$50-100):

- `LEADER_MIN_TRADE = $5` (filtra solo ruido — con ella el tamaño del
  trade no correlaciona con mejor resultado, a diferencia de la wallet
  original, validado sobre 239 mercados reales del mismo día)
- `MIRROR_PCT = 15%`, `MAX_MARKET_TOTAL = $20` por mercado
- Backtest sobre 239 mercados reales resueltos (2026-09-14, sin modelo de
  demora): **+4.15% a +5.51%**, consistente con su edge real.

### Seguridad — cómo se maneja la private key

**Nunca se pega en el chat ni en el repo.** El bot (`scripts/live_trader.py`)
la lee de la variable de entorno `POLY_PRIVATE_KEY`, configurada como
*secret* de GitHub Actions por el dueño directamente:

1. En GitHub: `Settings → Secrets and variables → Actions`
2. **Secrets** (nunca visibles, ni para mí): `POLY_PRIVATE_KEY` (la clave),
   `LIVE_TRADING_ENABLED` (poner `1` para operar de verdad — si no existe
   o vale otra cosa, el bot corre en dry-run automáticamente, sin riesgo)
3. **Variables** (no sensibles, pueden ser públicas): `POLY_FUNDER` (la
   dirección que tiene los fondos en Polymarket), `POLY_SIGNATURE_TYPE`
   (`1` si la cuenta se creó con email/Magic wallet, `0` si es
   MetaMask/hardware wallet), `LIVE_MAX_TOTAL_CAPITAL` (tope duro de
   seguridad en dólares, además del tope por mercado — default $100)

Por defecto (`LIVE` sin configurar) el bot corre en **dry-run**: detecta,
calcula el tamaño de cada orden, lo loguea, pero no manda nada a la
blockchain. Recién coloca órdenes reales con `LIVE_TRADING_ENABLED=1`
puesto explícitamente. Antes de eso, correr varias horas en dry-run y
revisar `state/live_trades.jsonl` para confirmar que el tamaño y momento
de cada orden simulada tiene sentido.

Recomendado: usar una wallet separada con fondos limitados dedicada solo a
este bot, no la cuenta principal — así un bug tiene un techo de pérdida
bajo, no el total de la cuenta.
