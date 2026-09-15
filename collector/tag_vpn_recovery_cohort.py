"""
Marca los mercados de la validacion forward anteriores a la recuperacion de
la VPN como PRE_VPN_RECOVERY -- NO los borra, solo los excluye de las
metricas oficiales (export_paper_validation.py filtra por esta columna).

Evidencia de la caida (collector/data.db, solo lectura):
  - errores de 'orderbook'/'market_trades' desde 2026-09-15 20:44:37 UTC
  - ultimo reconnect de WS: 2026-09-15 21:28:55 UTC (VPN_RECOVERY_TS)
  - cero errores ni gaps reales despues de ese punto
  - el mercado 21:25-21:30 UTC ya estaba EN CURSO durante la recuperacion
    (no es un mercado "completo" post-recovery)
  - primer mercado de 5min COMPLETO posterior: 21:30:00 UTC (OFFICIAL_COHORT_START_TS)

Este script NO toca collector/data.db (solo lo consulto para documentar la
evidencia en el commit/PAPER_VALIDATION.md) y NO reinicia run_paper_validation.py
-- la columna nueva tiene DEFAULT 'OFFICIAL', asi que el proceso que ya esta
corriendo sigue insertando filas nuevas correctamente sin necesitar cambios
de codigo ni reinicio (todo mercado nuevo abre despues de la recuperacion,
asi que el default ya es el correcto).
"""
import sqlite3
from datetime import datetime, timezone

import paper_validation_db as pdb

VPN_OUTAGE_START_TS = 1789505077.854968    # primer error registrado (orderbook), 20:44:37 UTC
VPN_RECOVERY_TS = 1789507735.1096609       # ultimo reconnect WS, 21:28:55 UTC
OFFICIAL_COHORT_START_TS = 1789507800.0    # 21:30:00 UTC -- primer mercado de 5min COMPLETO post-recovery


def fmt(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run():
    pdb.init_db()
    with pdb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(paper_markets)").fetchall()}
        if "validation_cohort" not in cols:
            conn.execute(
                "ALTER TABLE paper_markets ADD COLUMN validation_cohort TEXT NOT NULL DEFAULT 'OFFICIAL'"
            )
            print("columna validation_cohort agregada (DEFAULT 'OFFICIAL', seguro para el proceso vivo)")
        else:
            print("columna validation_cohort ya existia")

        pdb.set_meta_if_absent("vpn_outage_start_ts", repr(VPN_OUTAGE_START_TS))
        pdb.set_meta_if_absent("vpn_recovery_ts", repr(VPN_RECOVERY_TS))
        pdb.set_meta_if_absent("official_cohort_start_ts", repr(OFFICIAL_COHORT_START_TS))

        cur = conn.execute(
            "UPDATE paper_markets SET validation_cohort='PRE_VPN_RECOVERY' WHERE open_time_utc < ?",
            (OFFICIAL_COHORT_START_TS,),
        )
        n_pre = cur.rowcount
        cur2 = conn.execute(
            "UPDATE paper_markets SET validation_cohort='OFFICIAL' WHERE open_time_utc >= ? AND validation_cohort != 'OFFICIAL'",
            (OFFICIAL_COHORT_START_TS,),
        )
        n_official_fixed = cur2.rowcount

        totals = conn.execute(
            "SELECT validation_cohort, count(*) c FROM paper_markets GROUP BY validation_cohort"
        ).fetchall()

    print(f"\nVPN outage: {fmt(VPN_OUTAGE_START_TS)} -> recuperacion {fmt(VPN_RECOVERY_TS)}")
    print(f"cohorte oficial arranca en: {fmt(OFFICIAL_COHORT_START_TS)} (primer mercado de 5min completo post-recovery)")
    print(f"\nmercados marcados PRE_VPN_RECOVERY (excluidos de metricas oficiales, NO borrados): {n_pre}")
    print(f"mercados confirmados/corregidos como OFFICIAL: {n_official_fixed}")
    print("\ndistribucion final:")
    for r in totals:
        print(f"  {r['validation_cohort']}: {r['c']}")


if __name__ == "__main__":
    run()
