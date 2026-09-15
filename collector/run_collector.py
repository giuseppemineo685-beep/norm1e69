"""
Orquestador: un solo proceso, un hilo por flujo (más un hilo para el
cliente WebSocket, que corre su propio event loop asyncio). SIGTERM cierra
limpio -- cada escritura ya es una transacción corta y atómica (WAL), así
que no hay estado a medio escribir que perder; SIGTERM solo necesita
loguear el cierre y salir sin dejar el proceso colgado.

Uso:
    python3 run_collector.py                      # todos los flujos
    python3 run_collector.py --only leader_trades,markets,underlying
"""
import signal
import sys
import threading
import time

import backfill
import db
import inventory
import leader_trades_collector
import market_trades_collector
import markets_collector
import orderbook_collector
import trade_context
import underlying_price_collector
from config import LEADER_WALLET

COMPONENTS = {
    "markets": markets_collector.run,
    "leader_trades": leader_trades_collector.run,
    "market_trades": market_trades_collector.run,
    "underlying": underlying_price_collector.run,
    "orderbook_rest": orderbook_collector.run_rest,
    "orderbook_ws": orderbook_collector.run_ws_blocking,
    "inventory": inventory.run,
    "trade_context": trade_context.run,
}


def _wrap(name, fn):
    def target():
        while True:
            try:
                fn()
                return  # fn() only returns on a deliberate, non-looping exit
            except Exception as e:
                db.log_event(name, "crashed", {"error": str(e)})
                print(f"[{name}] crashed, restarting in 5s: {e}", file=sys.stderr, flush=True)
                time.sleep(5)
    return target


def main():
    db.init_db()
    only = None
    for arg in sys.argv[1:]:
        if arg.startswith("--only"):
            only = set(arg.split("=", 1)[1].split(",")) if "=" in arg else None

    print(f"norm1e69 research collector iniciando -- LEADER_WALLET={LEADER_WALLET}")
    print("SOLO LECTURA: dos datasets (trades del lider + estado del mercado). "
          "Nada de copia, ejecucion ni LIVE en este proceso.")

    backfill.import_live_trades_jsonl()

    names = only or COMPONENTS.keys()
    threads = []
    for name in names:
        t = threading.Thread(target=_wrap(name, COMPONENTS[name]), name=name, daemon=True)
        t.start()
        threads.append(t)
        db.log_event(name, "thread_started")

    def _on_sigterm(signum, frame):
        db.log_event("run_collector", "stop", {"signal": signum})
        print("SIGTERM/SIGINT recibido, cerrando (las escrituras ya son transacciones atomicas, "
              "no hay estado parcial que perder)...", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    while True:
        time.sleep(30)
        alive = sum(t.is_alive() for t in threads)
        if alive < len(threads):
            print(f"[health] {alive}/{len(threads)} hilos vivos", flush=True)


if __name__ == "__main__":
    main()
