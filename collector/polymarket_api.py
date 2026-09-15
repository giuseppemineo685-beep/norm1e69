"""
Read-only client for Polymarket's public Gamma + CLOB + data-api REST APIs,
plus underlying-price sources. No private key, no order placement anywhere
in this module -- everything here is either a public GET or a read-only
WebSocket subscription.
"""
import json
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import GAMMA_BASE, CLOB_BASE, DATA_API_BASE, ORDERBOOK_DEPTH_LEVELS

_session = requests.Session()
_session.headers.update({"User-Agent": "norm1e69-research-collector/0.1"})


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


def find_active_window(asset: str) -> Market | None:
    """Find the currently-live 5-minute Up/Down window for one asset by
    probing nearby slugs (slug embeds an epoch on a 300s grid; we don't
    know a priori if it marks window start or end, so we try neighbors)."""
    now = time.time()
    base = int(now // 300) * 300
    for candidate_ts in (base, base + 300, base - 300, base + 600):
        slug = f"{asset}-updown-5m-{candidate_ts}"
        m = get_market_by_slug(slug, asset)
        if m and m.start_ts and m.end_ts and m.start_ts <= now < m.end_ts and not m.closed:
            return m
    return None


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
