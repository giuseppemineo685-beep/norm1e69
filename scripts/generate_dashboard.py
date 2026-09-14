#!/usr/bin/env python3
"""Genera docs/index.html - dashboard del paper trader de Polycool Strategy."""
import json
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "paper_state.json"
LOG_PATH = ROOT / "state" / "paper_trades.jsonl"
SNAPSHOT_PATH = ROOT / "state" / "performance_snapshots.jsonl"
LIVE_STATE_PATH = ROOT / "state" / "live_state.json"
LIVE_LOG_PATH = ROOT / "state" / "live_trades.jsonl"
OUT_PATH = ROOT / "docs" / "index.html"

WALLET = "0x3048d65321be3497164cdfc2996f94f98a2e7537"
OWNER_WALLET = "0xb3B50facc6189C01A98ED909B807CFBA8A3951C4"
REAL_TRADING_STARTED_AT = 1789381327  # 2026-09-14 ~10:22 UTC - cerrado, ver banner
LIVE_TRADER_WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"  # norm1e69


def esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load():
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {
        "cash": 600.0, "start_cash": 600.0, "open_positions": [], "started_at": time.time(),
        "n_detected": 0, "n_copied": 0, "n_skipped_conviction": 0, "n_skipped_min": 0,
        "n_skipped_cash": 0, "delays_measured": [],
    }
    events = []
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    snapshots = []
    if SNAPSHOT_PATH.exists():
        for line in SNAPSHOT_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    snapshots.append(json.loads(line))
                except Exception:
                    pass
    return state, events, snapshots


def load_live():
    live_state = json.loads(LIVE_STATE_PATH.read_text()) if LIVE_STATE_PATH.exists() else None
    live_events = []
    if LIVE_LOG_PATH.exists():
        for line in LIVE_LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    live_events.append(json.loads(line))
                except Exception:
                    pass
    return live_state, live_events


def build(state, events, snapshots, live_state=None, live_events=None):
    closes = [e for e in events if e["type"] == "close"]
    wins = sum(1 for c in closes if c["correct"])
    total_pnl = sum(c["pnl"] for c in closes)
    total_cost_closed = sum(c["cost"] for c in closes)

    delays = state.get("delays_measured", [])
    delay_stats = {}
    if delays:
        delay_stats = {
            "avg": statistics.mean(delays),
            "median": statistics.median(delays),
            "p90": sorted(delays)[int(len(delays) * 0.9)] if len(delays) > 1 else delays[0],
            "max": max(delays),
        }

    equity = state["cash"] + sum(p["cost"] for p in state["open_positions"])
    hours_running = (time.time() - state.get("started_at", time.time())) / 3600

    return {
        "cash": state["cash"],
        "start_cash": state["start_cash"],
        "equity": equity,
        "open_n": len(state["open_positions"]),
        "n_detected": state.get("n_detected", 0),
        "n_copied": state.get("n_copied", 0),
        "n_skipped_conviction": state.get("n_skipped_conviction", 0),
        "n_skipped_min": state.get("n_skipped_min", 0),
        "n_skipped_cash": state.get("n_skipped_cash", 0),
        "closes": len(closes),
        "wins": wins,
        "win_rate": (wins / len(closes) * 100) if closes else None,
        "total_pnl": total_pnl,
        "total_cost_closed": total_cost_closed,
        "delay_stats": delay_stats,
        "hours_running": hours_running,
        "recent": list(reversed(events))[:40],
        "open_positions": state["open_positions"][-20:],
        "snapshots": list(reversed(snapshots))[:48],  # ultimas ~8h a 10min c/u
        "n_skipped_slippage": state.get("n_skipped_slippage", 0),
        "real_trading_started_at": REAL_TRADING_STARTED_AT,
        "owner_wallet": OWNER_WALLET,
        "live_state": live_state,
        "live_recent": list(reversed(live_events or []))[:25],
    }


def render_snapshots(snapshots):
    if not snapshots:
        return '<p class="muted">Todavía no hay snapshots (se registran cada 10 minutos).</p>'
    rows = []
    for s in snapshots:
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["ts"])) + " UTC"
        real_flag = " 🟢" if s["ts"] >= REAL_TRADING_STARTED_AT else ""
        rows.append(f"""<tr>
          <td class="mono-sm">{esc(ts)}{real_flag}</td>
          <td class="num">{s['equity']:.2f}</td>
          <td class="num">{s['cash']:.2f}</td>
          <td class="num">{s['open_positions']}</td>
          <td class="num">{s['n_copied']}</td>
        </tr>""")
    return f"""<table><tr><th>Hora</th><th>Equity</th><th>Cash</th><th>Abiertas</th><th>Copiadas (total)</th></tr>{''.join(rows)}</table>"""


def render_recent(recent):
    rows = []
    for e in recent:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts"])) + " UTC"
        if e["type"] == "open":
            rows.append(f"""<tr>
              <td class="mono-sm">{esc(ts)}</td>
              <td>{esc(e.get('market_title','') or '')}</td>
              <td>{esc(e['outcome'])}</td>
              <td class="num">{e['cost']:.2f}</td>
              <td class="num">{e['price_paid']:.3f}</td>
              <td class="num">{e['delay_s']:.1f}s</td>
              <td class="pill pending">abierta</td>
            </tr>""")
        else:
            cls = "good" if e["correct"] else "bad"
            label = "ganó" if e["correct"] else "perdió"
            rows.append(f"""<tr>
              <td class="mono-sm">{esc(ts)}</td>
              <td>{esc(e.get('market_title','') or '')}</td>
              <td>{esc(e['outcome'])}</td>
              <td class="num">{e['cost']:.2f}</td>
              <td class="num">{e['price_paid']:.3f}</td>
              <td class="num">—</td>
              <td class="pill {cls}">{label} (${e['pnl']:+.2f})</td>
            </tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="7" class="muted">Todavía sin actividad</td></tr>'


def render_open(positions):
    if not positions:
        return '<p class="muted">Sin posiciones abiertas ahora mismo.</p>'
    rows = []
    for p in positions:
        rows.append(f"""<tr>
          <td>{esc(p.get('market_title','') or '')}</td>
          <td>{esc(p['outcome'])}</td>
          <td class="num">{p['cost']:.2f}</td>
          <td class="num">{p['price_paid']:.3f}</td>
          <td class="num">{p['delay_s']:.1f}s</td>
        </tr>""")
    return f"""<table><tr><th>Mercado</th><th>Lado</th><th>Costo</th><th>Precio</th><th>Demora</th></tr>{''.join(rows)}</table>"""


def render_live_card(d):
    ls = d.get("live_state")
    if not ls:
        return """<div class="card">
      <h2>Bot en vivo &mdash; norm1e69</h2>
      <p class="muted">Todavía no arrancó (o no se publicó ningún estado todavía).</p>
    </div>"""
    live_on = ls.get("total_invested_live") is not None
    mode_pill = ('<span class="pill bad"><span class="pill-dot"></span> LIVE (plata real)</span>'
                 if ls.get("_live_flag") else '<span class="pill accent"><span class="pill-dot"></span> dry-run (sin plata real)</span>')
    rows = []
    for e in d.get("live_recent", []):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts"])) + " UTC"
        tag = "REAL" if e.get("live") else "dry-run"
        rows.append(f"""<tr>
          <td class="mono-sm">{esc(ts)}</td>
          <td>{esc(e.get('market_title','') or '')}</td>
          <td>{esc(e.get('outcome',''))}</td>
          <td class="num">{e.get('cost',0):.2f}</td>
          <td class="pill {'bad' if tag=='REAL' else 'pending'}">{tag}</td>
        </tr>""")
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="5" class="muted">Sin actividad todavía</td></tr>'
    return f"""<div class="card">
      <h2>Bot en vivo &mdash; norm1e69</h2>
      <div class="sub">Ejecutor propio conectado directo a la API de Polymarket (sin Polycool). Wallet copiada:
      <code class="mono-sm">{LIVE_TRADER_WALLET[:8]}&hellip;{LIVE_TRADER_WALLET[-6:]}</code>. {mode_pill}</div>
      <div class="table-scroll">
      <table>
        <tr><th>Detectados</th><th>Copiados</th><th>Bajo mínimo</th><th>Bloqueados por slippage</th><th>Bloqueados por tope de seguridad</th><th>Órdenes fallidas</th><th>Invertido total</th></tr>
        <tr>
          <td class="num">{ls.get('n_detected',0)}</td>
          <td class="num">{ls.get('n_copied',0)}</td>
          <td class="num">{ls.get('n_skipped_min',0)}</td>
          <td class="num">{ls.get('n_skipped_slippage',0)}</td>
          <td class="num">{ls.get('n_skipped_cap_seguridad',0)}</td>
          <td class="num">{ls.get('n_orders_failed',0)}</td>
          <td class="num">${ls.get('total_invested_live',0):.2f}</td>
        </tr>
      </table>
      </div>
      <div class="sub" style="margin-top:14px;">Actividad reciente del bot en vivo</div>
      <div class="table-scroll">
      <table>
        <tr><th>Hora</th><th>Mercado</th><th>Lado</th><th>Costo</th><th>Tipo</th></tr>
        {rows_html}
      </table>
      </div>
    </div>"""


def render(d):
    win_rate_txt = f"{d['win_rate']:.1f}%" if d["win_rate"] is not None else "—"
    ds = d["delay_stats"]
    delay_txt = (
        f"prom {ds['avg']:.1f}s · mediana {ds['median']:.1f}s · p90 {ds['p90']:.1f}s · máx {ds['max']:.1f}s"
        if ds else "todavía sin mediciones"
    )
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polycool Strategy</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root {{
  --paper: #f6f3ec; --ink: #1b1d22; --ash: #6b6455; --line: #e2ddd0; --surface: #ffffff;
  --accent: #2f6fb0; --accent-soft: rgba(47,111,176,0.12);
  --good: #2f8a5b; --good-soft: rgba(47,138,91,0.12);
  --bad: #b8404f; --bad-soft: rgba(184,64,79,0.12);
  --pending: #8a8272; --pending-soft: rgba(138,130,114,0.14);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --paper: #14161a; --ink: #ece7db; --ash: #a39a86; --line: #2c2d33; --surface: #1d1e24;
    --accent: #6fa8dc; --accent-soft: rgba(111,168,220,0.16);
    --good: #4cb583; --good-soft: rgba(76,181,131,0.14);
    --bad: #e0687a; --bad-soft: rgba(224,104,122,0.14);
    --pending: #9a9282; --pending-soft: rgba(154,146,130,0.16);
  }}
}}
:root[data-theme="dark"] {{
  --paper: #14161a; --ink: #ece7db; --ash: #a39a86; --line: #2c2d33; --surface: #1d1e24;
  --accent: #6fa8dc; --accent-soft: rgba(111,168,220,0.16);
  --good: #4cb583; --good-soft: rgba(76,181,131,0.14);
  --bad: #e0687a; --bad-soft: rgba(224,104,122,0.14);
  --pending: #9a9282; --pending-soft: rgba(154,146,130,0.16);
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--paper); color: var(--ink); font-family: 'IBM Plex Sans', system-ui, sans-serif; font-size: 15px; line-height: 1.5; }}
.mono-sm {{ font-family: 'IBM Plex Mono', ui-monospace, monospace; font-variant-numeric: tabular-nums; font-size: 0.85em; }}
h1, h2 {{ font-family: 'Fraunces', Georgia, serif; text-wrap: balance; margin: 0; }}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 28px 20px 60px; }}
.masthead {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; padding-bottom: 20px; border-bottom: 1px solid var(--line); margin-bottom: 24px; }}
.masthead h1 {{ font-size: 1.65rem; font-weight: 600; }}
.wallet-addr {{ color: var(--ash); font-size: 0.8rem; }}
.pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 500; white-space: nowrap; }}
.pill.good {{ background: var(--good-soft); color: var(--good); }}
.pill.bad {{ background: var(--bad-soft); color: var(--bad); }}
.pill.pending {{ background: var(--pending-soft); color: var(--pending); }}
.pill.accent {{ background: var(--accent-soft); color: var(--accent); }}
.pill-dot {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.updated {{ color: var(--ash); font-size: 0.78rem; }}
.stat-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1px; background: var(--line); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin-bottom: 24px; }}
.stat-tile {{ background: var(--surface); padding: 16px 18px; }}
.stat-tile .label {{ color: var(--ash); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }}
.stat-tile .value {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.5rem; font-weight: 600; margin-top: 4px; }}
.stat-tile .value.good {{ color: var(--good); }}
.stat-tile .value.bad {{ color: var(--bad); }}
.card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 20px 22px; margin-bottom: 20px; }}
.card h2 {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 4px; }}
.card .sub {{ color: var(--ash); font-size: 0.82rem; margin-bottom: 16px; }}
.muted {{ color: var(--ash); font-size: 0.85rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th {{ text-align: left; color: var(--ash); font-weight: 500; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; padding: 6px 8px; border-bottom: 1px solid var(--line); }}
td {{ padding: 7px 8px; border-bottom: 1px solid var(--line); }}
td.num {{ font-family: 'IBM Plex Mono', monospace; text-align: right; font-variant-numeric: tabular-nums; }}
.table-scroll {{ overflow-x: auto; }}
.banner {{ display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: 10px; background: var(--accent-soft); border: 1px solid var(--accent); margin-bottom: 20px; font-size: 0.86rem; }}
.banner b {{ color: var(--accent); }}
footer {{ margin-top: 32px; color: var(--ash); font-size: 0.78rem; border-top: 1px solid var(--line); padding-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="masthead">
    <div>
      <h1>Polycool Strategy</h1>
      <span class="wallet-addr mono-sm">copiando {WALLET[:8]}&hellip;{WALLET[-6:]} &middot; alias &ldquo;x-MoneyForWhiskas&rdquo;</span>
    </div>
    <div>
      <span class="pill accent"><span class="pill-dot"></span> papel</span>
      <span class="updated">actualizado {generated_at}</span>
    </div>
  </div>

  <div class="banner">
    <span>&#128203;</span>
    <div><b>100% papel, cero plata real.</b> Simula copiar solo las operaciones de esta wallet con costo &ge;$20,
    mirror 15%, tope $10/trade, capital inicial $600. Sin el 1% de fee de Polycool. La demora y el precio de llenado
    se miden en tiempo real, no se asumen.</div>
  </div>

  <div class="stat-strip">
    <div class="stat-tile"><div class="label">Cash + en posiciones</div><div class="value {'good' if d['equity']>=d['start_cash'] else 'bad'}">${d['equity']:.2f}</div></div>
    <div class="stat-tile"><div class="label">Capital inicial</div><div class="value">${d['start_cash']:.2f}</div></div>
    <div class="stat-tile"><div class="label">Retorno</div><div class="value {'good' if d['equity']>=d['start_cash'] else 'bad'}">{(d['equity']-d['start_cash'])/d['start_cash']*100:+.2f}%</div></div>
    <div class="stat-tile"><div class="label">Win rate (cerradas)</div><div class="value">{win_rate_txt}</div></div>
    <div class="stat-tile"><div class="label">Horas corriendo</div><div class="value">{d['hours_running']:.1f}h</div></div>
  </div>

  <div class="card">
    <h2>Demora real medida</h2>
    <div class="sub">Segundos entre que ella compra y que nuestro monitor la detecta (no asumido, medido en cada trade).</div>
    <div class="mono-sm">{delay_txt}</div>
  </div>

  <div class="card">
    <h2>Embudo de filtrado</h2>
    <div class="sub">De cada trade suyo detectado, cuántos pasan el filtro de convicción y el mínimo de Polymarket.</div>
    <div class="table-scroll">
    <table>
      <tr><th>Detectados</th><th>Copiados</th><th>Bajo $20 (sin convicción)</th><th>Bajo $1 (mínimo Polymarket)</th><th>Sin efectivo</th></tr>
      <tr>
        <td class="num">{d['n_detected']}</td>
        <td class="num">{d['n_copied']}</td>
        <td class="num">{d['n_skipped_conviction']}</td>
        <td class="num">{d['n_skipped_min']}</td>
        <td class="num">{d['n_skipped_cash']}</td>
      </tr>
    </table>
    </div>
  </div>

  <div class="banner">
    <span>&#128181;</span>
    <div><b>Plata real arrancó el 2026-09-14 ~10:22 UTC</b> (marcado con 🟢 en la tabla de abajo) &mdash;
    $200 en Polycool, wallet <code class="mono-sm">{esc(d['owner_wallet'][:8])}&hellip;{esc(d['owner_wallet'][-6:])}</code>,
    misma config que este sistema en papel (15% / $10 max / $20 mín. líder / guardia 10¢).
    Este dashboard sigue como referencia para comparar contra el rendimiento real.</div>
  </div>

  <div class="card">
    <h2>Performance cada 10 minutos</h2>
    <div class="sub">Snapshot de equity (cash + posiciones abiertas), más reciente primero. 🟢 = después de arrancar con plata real.</div>
    <div class="table-scroll">
    {render_snapshots(d['snapshots'])}
    </div>
  </div>

  <div class="card">
    <h2>Posiciones abiertas ({d['open_n']})</h2>
    {render_open(d['open_positions'])}
  </div>

  {render_live_card(d)}

  <div class="card">
    <h2>Actividad reciente</h2>
    <div class="sub">Últimos 40 eventos (aperturas y cierres), más reciente primero.</div>
    <div class="table-scroll">
    <table>
      <tr><th>Hora</th><th>Mercado</th><th>Lado</th><th>Costo</th><th>Precio</th><th>Demora</th><th>Estado</th></tr>
      {render_recent(d['recent'])}
    </table>
    </div>
  </div>

  <footer>
    Nuestro propio sistema de copy-trading en papel &mdash; no es Polycool, no cobra 1% por trade.
    Generado por <code class="mono-sm">scripts/generate_dashboard.py</code>.
  </footer>

</div>
</body>
</html>
"""


def main():
    state, events, snapshots = load()
    live_state, live_events = load_live()
    d = build(state, events, snapshots, live_state, live_events)
    html = render(d)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html)
    print(f"wrote {OUT_PATH} (equity=${d['equity']:.2f}, {d['n_copied']} copiados)")


if __name__ == "__main__":
    main()
