"""
Read-only client for Polymarket's public Gamma + CLOB + data-api REST APIs,
plus underlying-price sources. No private key, no order placement anywhere
in this module -- everything here is either a public GET or a read-only
WebSocket subscription.
"""
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import GAMMA_BASE, CLOB_BASE, DATA_API_BASE, ORDERBOOK_DEPTH_LEVELS

_session = requests.Session()
_session.headers.update({"User-Agent": "norm1e69-research-collector/0.1"})


# Duraciones de ventana que opera el líder, medido sobre sus trades reales:
# 5min = 97% de los trades y 88% del volumen; 15min = el resto (BTC only,
# pero real). Los esports quedan fuera a propósito: 17 trades / $113 en la
# muestra, y no son mercados Up/Down de precio.
WINDOW_SPECS = (("5m", 300), ("15m", 900))


@dataclass
class Market:
    condition_id: str
    market_slug: str
    market_title: str
    asset_symbol: str
    token_up: str
    token_down: str
    start_ts: float | None
    end_ts: float | None
    closed: bool
    outcome: str | None          # 'Up' | 'Down' | None
    resolution_source: str | None
    window_minutes: int | None = None


def _get(url, timeout=10, **params):
    r = _session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _to_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _parse_market(m: dict, asset_symbol: str) -> Market | None:
    try:
        outcomes = json.loads(m["outcomes"])
        token_ids = json.loads(m["clobTokenIds"])
    except (KeyError, json.JSONDecodeError, TypeError):
        return None
    up_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "up"), 0)
    if len(outcomes) < 2:
        return None
    down_idx = 1 - up_idx

    outcome = None
    if m.get("closed"):
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            if prices:
                winner_idx = max(range(len(prices)), key=lambda i: float(prices[i]))
                outcome = "Up" if winner_idx == up_idx else "Down"
        except (ValueError, TypeError):
            pass

    return Market(
        condition_id=m.get("conditionId", ""),
        market_slug=m["slug"],
        market_title=m.get("question", m["slug"]),
        asset_symbol=asset_symbol.upper(),
        token_up=token_ids[up_idx],
        token_down=token_ids[down_idx],
        start_ts=_to_ts(m.get("startDate")),
        end_ts=_to_ts(m.get("endDate")),
        closed=bool(m.get("closed")),
        outcome=outcome,
        resolution_source=m.get("resolutionSource") or m.get("umaResolutionStatus"),
    )


def get_market_by_slug(slug: str, asset_symbol: str) -> Market | None:
    try:
        m = _get(f"{GAMMA_BASE}/markets/slug/{slug}")
    except requests.RequestException:
        return None
    if isinstance(m, list):
        m = m[0] if m else None
    if not m:
        return None
    return _parse_market(m, asset_symbol)


def _window_start_from_slug(slug: str) -> float | None:
    """El epoch del slug es el INICIO de la ventana (verificado: el slug
    `btc-updown-15m-1789466400` corresponde a 06:00:00 ET y su título dice
    "6:00AM-6:15AM ET"). El fin es inicio + duración. Se deriva de acá y no
    del `end_date_iso` del CLOB, que para estos mercados devuelve medianoche
    del día en vez del cierre real de la ventana."""
    try:
        return float(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def find_active_windows(asset: str) -> list[Market]:
    """Ventanas Up/Down vivas ahora mismo para un asset, en todas las
    duraciones que opera el líder (5m y 15m). El slug embebe un epoch en una
    grilla de la duración; no sabemos a priori si marca inicio o fin, así que
    se prueban vecinos."""
    now = time.time()
    found = []
    for label, grid in WINDOW_SPECS:
        base = int(now // grid) * grid
        for candidate_ts in (base, base + grid, base - grid, base + 2 * grid):
            slug = f"{asset}-updown-{label}-{candidate_ts}"
            m = get_market_by_slug(slug, asset)
            if not m or m.closed:
                continue
            start_ts = _window_start_from_slug(slug)
            if start_ts is None:
                continue
            end_ts = start_ts + grid
            if start_ts <= now < end_ts:
                m.start_ts, m.end_ts = start_ts, end_ts
                m.window_minutes = grid // 60
                found.append(m)
                break
    return found


def find_active_window(asset: str) -> Market | None:
    """Compat: solo la ventana de 5 minutos (la dominante: 97% de los trades
    del líder). Preferir find_active_windows() para cobertura completa."""
    return next((m for m in find_active_windows(asset) if m.window_minutes == 5), None)


def get_book(token_id: str) -> dict:
    return _get(f"{CLOB_BASE}/book", token_id=token_id)


def top_levels(levels: list, n=ORDERBOOK_DEPTH_LEVELS, reverse=False):
    """Sort a book's bid/ask level list by price (best first) and return the top n."""
    try:
        sorted_levels = sorted(levels, key=lambda l: float(l["price"]), reverse=reverse)
    except (KeyError, ValueError, TypeError):
        return []
    return sorted_levels[:n]


def get_leader_trades(wallet: str, limit: int = 100, offset: int = 0) -> list:
    params = {"user": wallet, "limit": limit}
    if offset:
        params["offset"] = offset
    return _get(f"{DATA_API_BASE}/trades", **params)


def get_market_trades(condition_id: str, limit: int = 100) -> list:
    return _get(f"{DATA_API_BASE}/trades", market=condition_id, limit=limit)


def get_market_by_condition_id(condition_id: str) -> Market | None:
    """Resuelve metadata de un mercado por condition_id vía el CLOB. Se usa
    para los mercados que el líder opera pero que no estábamos trackeando (los
    que ya habían cerrado antes de arrancar, o duraciones que no seguimos):
    sin esto, `seconds_since_market_open`/`seconds_to_market_close` quedaban en
    NULL para la mayoría de sus trades.

    El inicio/fin se derivan del epoch del slug (ver _window_start_from_slug),
    no de `end_date_iso`, que acá viene con medianoche del día."""
    try:
        d = _get(f"{CLOB_BASE}/markets/{condition_id}")
    except requests.RequestException:
        return None
    slug = d.get("market_slug") or ""
    tokens = d.get("tokens", [])
    up = next((t["token_id"] for t in tokens if (t.get("outcome") or "").lower() == "up"), None)
    down = next((t["token_id"] for t in tokens if (t.get("outcome") or "").lower() == "down"), None)

    start_ts = end_ts = None
    window_minutes = None
    asset_symbol = None
    m = re.match(r"(\w+)-updown-(\d+)m-(\d+)$", slug)
    if m:
        asset_symbol = m.group(1).upper()
        window_minutes = int(m.group(2))
        start_ts = float(m.group(3))
        end_ts = start_ts + window_minutes * 60

    winner = next(((t.get("outcome") or "").capitalize() for t in tokens if t.get("winner")), None)

    return Market(
        condition_id=condition_id,
        market_slug=slug or condition_id,
        market_title=d.get("question") or slug,
        asset_symbol=asset_symbol,
        token_up=up,
        token_down=down,
        start_ts=start_ts,
        end_ts=end_ts,
        closed=bool(d.get("closed")),
        outcome=winner,
        resolution_source=d.get("resolutionSource") or d.get("description"),
        window_minutes=window_minutes,
    )


def get_resolution(condition_id: str) -> tuple[str | None, str | None]:
    """Returns (winner, resolution_source) or (None, None) if not resolved yet."""
    try:
        d = _get(f"{CLOB_BASE}/markets/{condition_id}")
    except requests.RequestException:
        return None, None
    tokens = d.get("tokens", [])
    winner_tok = next((tk for tk in tokens if tk.get("winner")), None)
    if not winner_tok:
        return None, None
    return winner_tok.get("outcome"), d.get("resolutionSource")


# --- Underlying price ---
# Polymarket resolves these 5-min markets against a Chainlink BTC/USD-style
# feed (confirmed by reading a real market's Gamma metadata this session --
# resolution text explicitly names "Chainlink's BTC/USD data stream").
# Reading that exact on-chain feed requires a Polygon RPC + ABI decoding of
# the aggregator contract, which is out of scope for a first pass. We use
# Binance's public spot ticker instead -- fast (well under 1s), free, no
# auth, 1:1 symbol mapping -- and document this as a DOCUMENTED ALTERNATIVE,
# not a proven match to Chainlink's exact print, per docs/LIMITATIONS.md.
_BINANCE_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}


def get_underlying_price(asset: str) -> tuple[float, str] | None:
    symbol = _BINANCE_SYMBOLS.get(asset.lower())
    if not symbol:
        return None
    try:
        d = _get("https://api.binance.com/api/v3/ticker/price", symbol=symbol, timeout=5)
        return float(d["price"]), "binance_spot"
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None
