"""
Reconstruye trade_context desde cero contra un DB_PATH dado, en un proceso
Python NUEVO (import fresco de trade_context.py) -- no requiere tocar ni
reiniciar run_collector.py. Sirve para:
  1. Probar la logica nueva (regla conservadora, snapshot_up/down_id,
     usable_for_strategy_learning) sobre una copia/snapshot, sin tocar
     collector/data.db.
  2. Eventualmente, corregir trade_context sobre la propia data.db en
     caliente: trade_context es 100% derivada (nunca contiene datos
     capturados), así que recalcularla desde un proceso externo es seguro
     mientras el collector siga corriendo -- SOLO arregla las filas ya
     existentes. Las filas NUEVAS que escriba el hilo trade_context.run()
     de run_collector.py seguiran usando el codigo que ese proceso cargo en
     memoria hasta que se reinicie (ver docs sobre el reinicio controlado).

Uso:
    python3 rebuild_trade_context.py --db-path /ruta/a/copia.db
"""
import argparse
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", required=True, help="Nunca usar collector/data.db sin autorizacion explicita")
    args = p.parse_args()

    import config
    config.DB_PATH = args.db_path
    import db
    db.DB_PATH = args.db_path
    db.reset_connection()

    import trade_context
    db.init_db()
    print(f"reconstruyendo trade_context sobre {args.db_path} ...")
    trade_context.rebuild_all()


if __name__ == "__main__":
    sys.exit(main())
