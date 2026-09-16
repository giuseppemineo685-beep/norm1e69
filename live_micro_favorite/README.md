# live_micro_favorite

Ejecutor micro-LIVE aislado para `POLYMARKET_FAVORITE_BASELINE`. Documentación
completa (arquitectura, límites de seguridad, DRY_RUN, LIVE, detención,
verificación del mecanismo real de protección de precio) en
[`docs/LIVE_MICRO_FAVORITE.md`](docs/LIVE_MICRO_FAVORITE.md).

**No reutiliza ni modifica `scripts/live_trader.py`, `collector/` ni el
paper validator.** LIVE nunca se activa por defecto — requiere dos
variables de entorno explícitas (ver `.env.example`).

## Setup: venv exclusivo (no compartir con `collector/venv`)

```bash
cd live_micro_favorite
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

El DRY_RUN funciona sin instalar nada (nunca importa `polymarket`), pero
usar este venv dedicado para todo (DRY_RUN y LIVE) mantiene la dependencia
del SDK real fuera de `collector/venv` — que también usan el collector y el
paper validator en vivo — sin arriesgar esos dos procesos con un paquete
nuevo.

## Credenciales (solo para LIVE)

```bash
cp .env.example .env      # .env NUNCA se versiona -- ver .gitignore
# completar .env con los valores reales
set -a && source .env && set +a
```

## Comandos

```bash
# DRY_RUN (seguro, default, no requiere credenciales ni el venv)
venv/bin/python3 run_live_micro_favorite.py

# Tests
venv/bin/python3 -m pytest tests/ -v

# LIVE -- SOLO tras confirmación explícita, ver docs/LIVE_MICRO_FAVORITE.md
set -a && source .env && set +a
venv/bin/python3 run_live_micro_favorite.py
```

Detener (cualquier modo): `kill <PID>`.
