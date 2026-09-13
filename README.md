# Polycool Strategy

Sistema propio de copy-trading en **papel** (sin plata real) de la wallet de
Polymarket `0x3048d65321be3497164cdfc2996f94f98a2e7537` (alias
"x-MoneyForWhiskas", antes "@justoneofmystrategies").

Construido para no depender de la app Polycool: sin su 1% de fee por trade,
y con el filtro de convicción que esa app no ofrece.

## Reglas (validadas por backtest sobre ~50h reales de datos)

- Solo copia si el trade individual de la wallet cuesta **≥$20** (su nivel
  de convicción real)
- Mirror **15%** de ese costo, con tope **$10** por operación nuestra
- Nunca opera por debajo de **$1** (mínimo real de Polymarket) ni sin
  efectivo suficiente
- Capital inicial en papel: **$600**

Con este capital, el backtest histórico nunca tocó el piso de $1 (mínimo
$1.63) — niveles menores ($300-500) sí llegaron a romperse o quedar al
borde por una mala racha temprana.

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

## Siguiente paso (no implementado todavía, a propósito)

Ejecución real con dinero — requiere credenciales de la API de Polymarket
del dueño y una decisión explícita de pasar a plata real, después de
validar esto en papel.
