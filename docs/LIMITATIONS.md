# Limitaciones conocidas

- **Límite de 100 trades sin cursor** (`data-api.polymarket.com/trades?user=...&limit=100`):
  si el líder hace más de 100 trades en 1 segundo (el intervalo de poll),
  los más viejos de ese lote nunca aparecen y se pierden para siempre. No
  hay paginación por cursor documentada en este endpoint. Se cuantifica
  (no se resuelve): `leader_poll_log.n_returned`, con alerta en
  `data_quality.py` si viene en 100 varias veces seguidas.
- **`maker_taker`**: solo se guarda si el payload de la API lo trae
  explícito. Si no, queda `NULL` -- nunca se infiere ni se inventa.
- **Fees/rebates**: no se calculan. La API pública no los expone de forma
  confiable para reconstruirlos con evidencia real.
- **Precio del subyacente**: Polymarket resuelve estos mercados contra un
  feed estilo Chainlink (confirmado leyendo los metadatos de un market real
  vía Gamma API). Este collector usa **Binance spot** (`api.binance.com`)
  como fuente documentada -- rápida, pública, sin auth -- pero **no está
  probado que coincida al centavo con el print exacto de Chainlink en el
  instante de resolución**. `underlying_prices.source` siempre indica de
  dónde vino cada lectura.
- **Order book histórico previo a este collector**: no existe y no se
  inventa. `trade_context.context_available=0` marca explícitamente
  cualquier offset sin un snapshot real lo bastante cercano (tolerancia
  2.5s).
- **Contexto post-trade (+1..+10s)**: sirve para evaluar la consecuencia
  de una decisión ya tomada. Estas filas quedan `usable_for_backtest=0` en
  el propio esquema -- usarlas como feature de entrada de un backtest
  introduce look-ahead bias.
- **WebSocket vs REST**: el WS da baja latencia pero puede tener gaps
  (reconexión, mensajes perdidos); el snapshot REST cada 1s es el respaldo
  que garantiza cobertura mínima incluso si el WS se cae. `data_quality.py`
  reporta reconexiones/gaps del componente `orderbook_ws`, no los oculta.
- **NTP**: se asume que la VPS ya tiene reloj sincronizado
  (`systemd-timesyncd` estándar en Oracle Cloud/Ubuntu). No se mide
  activamente el desfasaje reloj-local vs. timestamp-de-fuente por evento
  en esta primera versión más allá de lo que ya se puede inferir
  comparando `received_at_utc` vs `source_timestamp_utc`.
- **Backfill vía API** (`backfill.py --api`): best-effort con `offset` --
  si la API pública no soporta paginación real en este endpoint (no está
  documentado que lo haga), se detecta y se detiene sin loop infinito, sin
  garantizar cuánta historia adicional realmente recupera.

## Preguntas guía -- ejemplos de SQL

**¿Compra el lado más barato o el más probable?**
```sql
SELECT lt.outcome, tc.best_ask_up, tc.best_ask_down, lt.price
FROM leader_trades lt JOIN trade_context tc
  ON tc.leader_trade_id = lt.id AND tc.offset_seconds = -1
ORDER BY lt.source_timestamp_utc;
```

**¿Equilibra shares, capital o exposición? (evolución de cobertura)**
```sql
SELECT after_trade_id, up_shares, down_shares, coverage_ratio, surplus_side, surplus_shares
FROM leader_inventory_timeline WHERE condition_id = ? ORDER BY after_trade_id;
```

**¿Cuánto tarda en cubrir la segunda pierna?**
```sql
SELECT DISTINCT condition_id, time_to_hedge_second_leg_s
FROM leader_inventory_timeline WHERE time_to_hedge_second_leg_s IS NOT NULL;
```

**¿La regla cambia entre BTC/ETH/SOL?**
```sql
SELECT asset_symbol, avg(seconds_since_market_open) avg_entry_time, count(*) n
FROM leader_trades GROUP BY asset_symbol;
```
