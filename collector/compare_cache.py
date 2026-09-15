"""
Prueba A/B del cache-busting contra el endpoint de trades.

Demuestra tres cosas, con datos, no con suposiciones:
  1. El parámetro `_cb` NO altera el contenido: para un mismo trade, todos los
     campos que devuelve el endpoint son idénticos con y sin el parámetro.
  2. NO altera la paginación ni los filtros: `offset` y `user` siguen
     comportándose igual (mismas páginas, mismo wallet, mismos tamaños).
  3. SÍ entrega los trades antes: se mide, por trade, el instante en que cada
     variante lo vio por primera vez.

Uso:  python3 compare_cache.py [minutos]     (por defecto 10)
"""
import statistics
import sys
import time

import polymarket_api as pm
from config import LEADER_WALLET

INTERVAL_S = 2.0


def _key(t):
    return (t["transactionHash"], t["timestamp"], t["asset"], t["size"])


def _norm(t):
    """Contenido comparable de un trade (sin campos volátiles de transporte)."""
    return {k: t[k] for k in sorted(t) if k != "_cb"}


def check_pagination_and_filters():
    print("=" * 74)
    print("1) ¿El parámetro altera paginación, filtros o contenido?")
    print("=" * 74)

    ok = True
    for offset in (0, 100, 200):
        plain = pm.get_leader_trades(LEADER_WALLET, limit=100, offset=offset, bust_cache=False)
        busted = pm.get_leader_trades(LEADER_WALLET, limit=100, offset=offset, bust_cache=True)
        kp, kb = {_key(t) for t in plain}, {_key(t) for t in busted}
        overlap = len(kp & kb)
        print(f"  offset={offset:<4d} sin_cb={len(plain):3d} trades   con_cb={len(busted):3d} trades   "
              f"solapamiento={overlap:3d}")
        if len(plain) != len(busted):
            print(f"      OJO: distinto tamaño de página")
            ok = False

        # mismo contenido para los trades que aparecen en ambas
        plain_by_key = {_key(t): t for t in plain}
        mismatches = 0
        for t in busted:
            other = plain_by_key.get(_key(t))
            if other is not None and _norm(t) != _norm(other):
                mismatches += 1
        print(f"      trades presentes en ambas con contenido distinto: {mismatches}")
        if mismatches:
            ok = False

    # el filtro por wallet sigue aplicando
    wallets = {t.get("proxyWallet") or t.get("user") or LEADER_WALLET
               for t in pm.get_leader_trades(LEADER_WALLET, limit=100, bust_cache=True)}
    print(f"  wallets distintos en la respuesta con _cb: {len(wallets)} "
          f"{'(filtro intacto)' if len(wallets) <= 1 else '<<< FILTRO ROTO'}")
    print(f"\n  => {'OK: el parámetro no altera nada del contenido' if ok else 'PROBLEMA detectado'}\n")
    return ok


def timing_race(minutes):
    print("=" * 74)
    print(f"2) Carrera de latencia durante {minutes} minutos")
    print("=" * 74)

    first_seen = {"plain": {}, "busted": {}}
    content = {"plain": {}, "busted": {}}
    cdn_ages, x_caches = [], {"plain": {}, "busted": {}}
    end = time.time() + minutes * 60
    rounds = 0

    while time.time() < end:
        rounds += 1
        for variant, bust in (("busted", True), ("plain", False)):
            try:
                trades, meta = pm.get_leader_trades(LEADER_WALLET, limit=100,
                                                     bust_cache=bust, with_meta=True)
            except Exception:
                continue
            seen_at = meta["api_received_at"]
            if variant == "plain" and meta["cdn_age_s"] is not None:
                cdn_ages.append(meta["cdn_age_s"])
            xc = meta["x_cache"] or "-"
            x_caches[variant][xc] = x_caches[variant].get(xc, 0) + 1
            for t in trades:
                k = _key(t)
                first_seen[variant].setdefault(k, seen_at)
                content[variant].setdefault(k, _norm(t))
        time.sleep(INTERVAL_S)

    print(f"  rondas: {rounds}")
    for v in ("plain", "busted"):
        print(f"  {v:7s}: {len(first_seen[v])} trades únicos vistos   x-cache={x_caches[v]}")
    if cdn_ages:
        print(f"  cdn age del endpoint SIN cache-busting: "
              f"p50={statistics.median(cdn_ages):.0f}s  max={max(cdn_ages):.0f}s")

    common = set(first_seen["plain"]) & set(first_seen["busted"])
    only_plain = set(first_seen["plain"]) - set(first_seen["busted"])
    only_busted = set(first_seen["busted"]) - set(first_seen["plain"])
    print(f"\n  trades en AMBAS variantes: {len(common)}")
    print(f"  solo en la normal: {len(only_plain)}   solo en la busted: {len(only_busted)}")
    print("     (las diferencias en los bordes son normales: cada variante ve una "
          "ventana ligeramente distinta al principio y al final)")

    # contenido idéntico para los trades comunes
    diffs = sum(1 for k in common if content["plain"][k] != content["busted"][k])
    print(f"  trades comunes con contenido DISTINTO: {diffs} "
          f"{'(idénticos)' if diffs == 0 else '<<< PROBLEMA'}")

    deltas = [first_seen["plain"][k] - first_seen["busted"][k] for k in common]
    if deltas:
        earlier = sum(1 for d in deltas if d > 0.5)
        same = sum(1 for d in deltas if abs(d) <= 0.5)
        later = sum(1 for d in deltas if d < -0.5)
        deltas.sort()
        def pct(p):
            return deltas[min(int(len(deltas) * p), len(deltas) - 1)]
        print(f"\n  ventaja del cache-busting (segundos que llegó antes):")
        print(f"    p50={pct(0.5):+.1f}s  p95={pct(0.95):+.1f}s  max={max(deltas):+.1f}s  "
              f"media={statistics.mean(deltas):+.1f}s")
        print(f"    llegó ANTES en {earlier}/{len(deltas)} trades, "
              f"igual en {same}, después en {later}")
    print()


if __name__ == "__main__":
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    check_pagination_and_filters()
    timing_race(minutes)
