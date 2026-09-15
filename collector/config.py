"""
Configuration for the research collector -- two read-only datasets
(leader trades + market state), completely separate from
scripts/live_trader.py. No trading, no copy simulation, no LIVE path
exists anywhere in this package.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # .../norm1e69/collector
REPO_ROOT = ROOT.parent                          # .../norm1e69

# --- Leader wallet (one active per run, never mixed) ---
KNOWN_WALLETS = {
    "norm1e69": "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354",
    "x-moneyforwhiskas": "0x3048d65321be3497164cdfc2996f94f98a2e7537",
}
LEADER_WALLET = os.environ.get("LEADER_WALLET") or KNOWN_WALLETS["norm1e69"]

# --- Assets tracked ---
ASSETS = ["btc", "eth", "sol"]

# --- APIs ---
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# --- Polling / collection cadence ---
LEADER_POLL_INTERVAL_SEC = 1        # same cadence live_trader.py already validated
MARKET_TRADES_POLL_INTERVAL_SEC = 2
ORDERBOOK_SNAPSHOT_INTERVAL_SEC = 1  # REST fallback, independent of the WS feed
UNDERLYING_PRICE_POLL_INTERVAL_SEC = 1
RESOLVE_CHECK_INTERVAL_SEC = 15
ORDERBOOK_DEPTH_LEVELS = 10

# --- Storage ---
DB_PATH = str(REPO_ROOT / "collector" / "data.db")

# --- Timezone for human-readable columns ---
TIMEZONE_ET = "America/New_York"

# --- Recuperación de huecos ---
# Si entre dos polls el líder hizo más trades de los que entran en una página
# (100), el trade más viejo de la página nueva queda MÁS NUEVO que lo último
# guardado: eso es un hueco real. Se pagina hacia atrás con `offset` (verificado
# que funciona en data-api) hasta cruzar la marca de agua. Este tope evita que
# un hueco enorme (ej. tras una caída larga) dispare cientos de requests.
MAX_GAP_FILL_PAGES = 20
