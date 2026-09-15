# Operación del collector de investigación

Este documento cubre `collector/` -- el sistema de recolección de datos de
solo lectura (trades del líder + estado del mercado). **No** cubre
`scripts/live_trader.py`, que sigue operando exactamente igual que antes,
sin ningún cambio.

## Instalación

```bash
cd norm1e69/collector
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # ajustar LEADER_WALLET si hace falta -- no hay secretos acá
```

## Arrancar una captura

```bash
source venv/bin/activate
set -a; source .env; set +a
python3 run_collector.py
```

Corre para siempre (todos los flujos: mercados, order book WS+REST,
trades públicos del mercado, precio del subyacente, trades del líder,
inventario, contexto). Ctrl+C o SIGTERM cierra limpio.

Para correr solo un subconjunto (útil para probar):
```bash
python3 run_collector.py --only=leader_trades,markets,underlying
```

## Arrancar 24h+ en la VPS de Germany, vía systemd

```bash
ssh -i ~/.ssh/oracle_vps_key root@178.105.143.153
mkdir -p /root/norm1e69 && cd /root/norm1e69
git clone https://github.com/giuseppemineo685-beep/norm1e69.git .
cd collector
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
cp .env.example .env
deactivate

cp norm1e69-collector.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now norm1e69-collector
```

## Verificar que está capturando

```bash
systemctl status norm1e69-collector
journalctl -u norm1e69-collector -f          # logs en vivo

# datos reales llegando:
cd /root/norm1e69/collector
source venv/bin/activate
python3 -c "
import db
db.init_db()
with db.connect() as conn:
    for t in ('leader_trades','market_trades','orderbook_snapshots','underlying_prices','markets'):
        print(t, conn.execute(f'SELECT count(*) c FROM {t}').fetchone()['c'])
"
python3 data_quality.py   # reporte completo de cobertura/calidad
```

Reinicio seguro (no duplica nada, gracias al UNIQUE constraint de cada tabla):
```bash
systemctl restart norm1e69-collector
```

## Exportar para análisis

```bash
python3 export_report.py ./export           # 7 CSVs
python3 export_report.py ./export --xlsx    # + trade_strategy_analysis.xlsx
```

## Query de verificación: últimos trades del líder + su contexto de mercado

```sql
SELECT lt.market_title, lt.outcome, lt.price, lt.shares, lt.source_timestamp_utc,
       tc.best_bid_up, tc.best_ask_up, tc.best_bid_down, tc.best_ask_down,
       tc.underlying_price, tc.combined_cost_to_pair
FROM leader_trades lt
JOIN trade_context tc ON tc.leader_trade_id = lt.id AND tc.offset_seconds = 0
ORDER BY lt.source_timestamp_utc DESC
LIMIT 5;
```

## Backfill histórico (una vez, o cuando se quiera repetir)

```bash
python3 backfill.py           # solo importa state/live_trades.jsonl (lado del lider)
python3 backfill.py --api     # + intenta paginar mas historia via data-api (best-effort)
```

## Preguntas guía -- ejemplos de query

Ver `docs/LIMITATIONS.md` para las 13 preguntas y ejemplos de SQL contra
`leader_trades` + `trade_context` + `leader_inventory_timeline`.
