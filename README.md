# Polycool Strategy — copiando a norm1e69

Ejecutor propio que copia las operaciones de la wallet de Polymarket
`0x41e2e1ccf1e4940029af02259a31c6b89b9fa354` (**norm1e69**), conectado
directo a la API oficial de Polymarket (`py-clob-client`) — sin Polycool
ni su 1% de fee. Verificada como ganadora consistente: $87 → $100,610 en
39 días, curva oficial de Polymarket.

## Copia 1:1

Por cada trade suyo: mismo mercado, mismo lado, mismo monto en dólares.
Sin filtro de conviccion, sin mirror %, sin tope por mercado — ella hace
cientos de trades chicos por minuto, así que filtrar o recalcular por
mercado le haría perder operaciones al copiador, y dejaría de ser el mismo
resultado. El único piso es el mínimo real de Polymarket ($1). Sin freno
de capital por defecto — el backstop real es la plata disponible.

Aviso honesto: "exactamente igual" no es una garantía matemática — hay
demora real (detección + red) entre que ella compra y que nosotros
compramos, así que el precio puede moverse un poco. Es la máxima fidelidad
posible, no un resultado idéntico garantizado.

**Capital mínimo recomendado: $2,500.** Validado por backtest sobre un día
real completo (3,500 trades, 239 mercados): cubre el pico de exposición
simultánea (~$1,905) con margen, cero trades perdidos por falta de cash.

**Guardia anti-glitch, no filtro:** `MAX_SLIPPAGE` solo bloquea si el
precio que vemos está roto (fuera de rango real 0-1), no si simplemente se
movió. Con un valor de 0.25 (pensado como filtro, no como fusible) se
probó en real y bloqueaba el **33% de sus trades** (185 de 562, sin
ninguna otra causa - 0 por falta de cash, 0 órdenes fallidas) — contradice
"copiar 1:1". Subido a 0.97: prácticamente nunca actúa sobre un precio
real, solo sobre datos corruptos.

## Cómo corre

- `scripts/live_trader.py` — detecta sus trades, calcula la copia 1:1,
  simula en papel (siempre) y coloca órdenes reales si `LIVE=1`.
- `scripts/generate_dashboard.py` — genera `docs/index.html`: equity de
  papel, demora medida, y la tabla completa de todos los trades (mercado,
  lado, costo, nuestro precio, precio de ella, demora).
- `scripts/run_live_bounded.sh` — corre el bot + dashboard en loop acotado
  (~5h40m, para GitHub Actions), auto-publish a git cada 60s.
- `.github/workflows/live_trader.yml` — dispara `run_live_bounded.sh` cada
  6h automáticamente, 24/7, sin depender de ninguna PC prendida.
- `state/live_state.json` — estado (cash de papel, posiciones, contadores,
  demoras medidas).
- `state/live_trades.jsonl` — log append-only de cada trade y cierre.

## Seguridad — cómo se maneja la private key

**Nunca se pega en el chat ni en el repo.** El bot la lee de la variable
de entorno `POLY_PRIVATE_KEY`, configurada como *secret* de GitHub Actions
por el dueño directamente:

1. En GitHub: `Settings → Secrets and variables → Actions`
2. **Secrets** (nunca visibles, ni para mí): `POLY_PRIVATE_KEY` (la clave),
   `LIVE_TRADING_ENABLED` (poner `1` para operar de verdad — si no existe
   o vale otra cosa, el bot corre en dry-run/papel automáticamente, sin
   riesgo)
3. **Variables** (no sensibles, pueden ser públicas): `POLY_FUNDER` (la
   dirección que tiene los fondos en Polymarket), `POLY_SIGNATURE_TYPE`
   (`1` si la cuenta se creó con email/Magic wallet, `0` si es
   MetaMask/hardware wallet), `LIVE_MAX_TOTAL_CAPITAL` (opcional — tope
   duro de seguridad en dólares; sin configurar, no hay tope)

Por defecto (`LIVE` sin configurar) el bot corre en **papel**: detecta,
calcula el tamaño de cada orden, lo loguea y simula, pero no manda nada a
la blockchain. Recién coloca órdenes reales con `LIVE_TRADING_ENABLED=1`
puesto explícitamente — mismo script, mismo estado, sin cortar el
histórico ya acumulado.

Recomendado: usar una wallet separada con fondos limitados dedicada solo a
este bot, no la cuenta principal.

## Archivado

El sistema anterior (copiaba a otra wallet, "x-MoneyForWhiskas", con
lógica de mirror % + tope proporcional) quedó completamente archivado en
[`archive/`](archive/) — código, estado y el workflow que lo corría
(desactivado). Se dejó de usar porque la validación real mostró que
Polycool (por donde se operó esa wallet con plata real) es una caja negra
que no se comporta como ningún backtest externo puede predecir. Se
mantiene el historial por si sirve de referencia, no se sigue actualizando.
