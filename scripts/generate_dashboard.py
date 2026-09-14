#!/usr/bin/env python3
"""Genera docs/index.html - dashboard del bot que copia a norm1e69."""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "live_state.json"
LOG_PATH = ROOT / "state" / "live_trades.jsonl"
OUT_PATH = ROOT / "docs" / "index.html"

WALLET = "0x41e2e1ccf1e4940029af02259a31c6b89b9fa354"  # norm1e69


def esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load():
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
    events = []
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return state, events


def build(state, events):
    if state is None:
        return None
    paper_cash = state.get("paper_cash", 0.0)
    paper_start = state.get("paper_start_cash", 0.0)
    paper_open_cost = sum(p["cost"] for p in state.get("paper_positions", {}).values())
    paper_equity = paper_cash + paper_open_cost
    paper_ret = ((paper_equity - paper_start) / paper_start * 100) if paper_start else 0.0

    closes = [e for e in events if e.get("type") == "close_paper"]
    wins = sum(1 for c in closes if c.get("pnl", 0) >= 0)
    win_rate = (wins / len(closes) * 100) if closes else None

    delays = state.get("delays_measured", [])
    delay_stats = {}
    if delays:
        s = sorted(delays)
        delay_stats = {
            "avg": sum(delays) / len(delays),
            "median": s[len(s) // 2],
            "p90": s[int(len(s) * 0.9)] if len(s) > 1 else s[0],
            "max": max(delays),
        }

    hours_running = (time.time() - state.get("started_at", time.time())) / 3600

    return {
        "live": state.get("_live_flag", False),
        "paper_cash": paper_cash,
        "paper_start": paper_start,
        "paper_equity": paper_equity,
        "paper_ret": paper_ret,
        "paper_open_n": len(state.get("paper_positions", {})),
        "n_detected": state.get("n_detected", 0),
        "n_copied": state.get("n_copied", 0),
        "n_skipped_slippage": state.get("n_skipped_slippage", 0),
        "n_skipped_cap_seguridad": state.get("n_skipped_cap_seguridad", 0),
        "n_paper_skipped_cash": state.get("n_paper_skipped_cash", 0),
        "n_orders_failed": state.get("n_orders_failed", 0),
        "total_invested_live": state.get("total_invested_live", 0.0),
        "closes": len(closes),
        "win_rate": win_rate,
        "delay_stats": delay_stats,
        "hours_running": hours_running,
        "events": list(reversed(events)),  # todos, mas reciente primero
    }


def render_events(events):
    rows = []
    for e in events:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts"])) + " UTC"
        if e.get("type") == "close_paper":
            cls = "good" if e.get("pnl", 0) >= 0 else "bad"
            rows.append(f"""<tr>
              <td class="mono-sm">{esc(ts)}</td>
              <td>{esc(e.get('market_title','') or '')}</td>
              <td>&mdash;</td>
              <td class="num">{e.get('cost',0):.2f}</td>
              <td class="num">&mdash;</td>
              <td class="num">&mdash;</td>
              <td class="num">&mdash;</td>
              <td class="pill {cls}">cierre (${e.get('pnl',0):+.2f})</td>
            </tr>""")
            continue
        tag = "REAL" if e.get("live") else "papel"
        rows.append(f"""<tr>
          <td class="mono-sm">{esc(ts)}</td>
          <td>{esc(e.get('market_title','') or '')}</td>
          <td>{esc(e.get('outcome',''))}</td>
          <td class="num">{e.get('cost',0):.2f}</td>
          <td class="num">{e.get('ref_price',0):.3f}</td>
          <td class="num">{e.get('leader_price',0):.3f}</td>
          <td class="num">{e.get('delay_s',0):.1f}s</td>
          <td class="pill {'bad' if tag=='REAL' else 'pending'}">{tag}</td>
        </tr>""")
    return "\n".join(rows) if rows else '<tr><td colspan="8" class="muted">Todavía sin actividad</td></tr>'


def render(d):
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    if d is None:
        body = """<div class="card"><p class="muted">Todavía no arrancó (o no se publicó ningún estado todavía).</p></div>"""
        stat_strip = ""
        delay_card = ""
    else:
        mode_pill = ('<span class="pill bad"><span class="pill-dot"></span> LIVE &middot; plata real</span>'
                     if d["live"] else '<span class="pill accent"><span class="pill-dot"></span> papel &middot; sin plata real</span>')
        win_rate_txt = f"{d['win_rate']:.1f}%" if d["win_rate"] is not None else "—"
        ds = d["delay_stats"]
        delay_txt = (f"prom {ds['avg']:.1f}s · mediana {ds['median']:.1f}s · p90 {ds['p90']:.1f}s · máx {ds['max']:.1f}s"
                     if ds else "todavía sin mediciones")

        stat_strip = f"""
  <div class="stat-strip">
    <div class="stat-tile"><div class="label">Equity (papel)</div><div class="value {'good' if d['paper_equity']>=d['paper_start'] else 'bad'}">${d['paper_equity']:.2f}</div></div>
    <div class="stat-tile"><div class="label">Capital inicial</div><div class="value">${d['paper_start']:.2f}</div></div>
    <div class="stat-tile"><div class="label">Retorno</div><div class="value {'good' if d['paper_ret']>=0 else 'bad'}">{d['paper_ret']:+.2f}%</div></div>
    <div class="stat-tile"><div class="label">Win rate (cerradas)</div><div class="value">{win_rate_txt}</div></div>
    <div class="stat-tile"><div class="label">Horas corriendo</div><div class="value">{d['hours_running']:.1f}h</div></div>
  </div>

  <div class="banner">
    <span>&#9889;</span>
    <div>{mode_pill} &mdash; copia 1:1: mismo mercado, mismo lado, mismo monto en dólares que ella, sin filtro ni tope
    proporcional. El único tope es el capital disponible.</div>
  </div>

  <div class="card">
    <h2>Demora real medida</h2>
    <div class="sub">Segundos entre que ella compra y que nosotros entramos.</div>
    <div class="mono-sm">{delay_txt}</div>
  </div>

  <div class="card">
    <h2>Embudo</h2>
    <div class="table-scroll">
    <table>
      <tr><th>Detectados</th><th>Copiados</th><th>Bloqueados (precio roto)</th><th>Sin cash (papel)</th><th>Bloqueados por tope</th><th>Órdenes fallidas</th><th>Invertido real</th></tr>
      <tr>
        <td class="num">{d['n_detected']}</td>
        <td class="num">{d['n_copied']}</td>
        <td class="num">{d['n_skipped_slippage']}</td>
        <td class="num">{d['n_paper_skipped_cash']}</td>
        <td class="num">{d['n_skipped_cap_seguridad']}</td>
        <td class="num">{d['n_orders_failed']}</td>
        <td class="num">${d['total_invested_live']:.2f}</td>
      </tr>
    </table>
    </div>
  </div>
"""
        body = f"""
  <div class="card">
    <h2>Todos los trades ({len(d['events'])})</h2>
    <div class="sub">Mercado, lado, costo, precio al que entramos, precio de ella, demora en segundos. Más reciente primero.</div>
    <div class="table-scroll">
    <table>
      <tr><th>Hora</th><th>Mercado</th><th>Lado</th><th>Costo</th><th>Nuestro precio</th><th>Precio de ella</th><th>Demora</th><th>Tipo</th></tr>
      {render_events(d['events'])}
    </table>
    </div>
  </div>
"""

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copiando a norm1e69</title>
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
th {{ text-align: left; color: var(--ash); font-weight: 500; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; padding: 6px 8px; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--surface); }}
td {{ padding: 7px 8px; border-bottom: 1px solid var(--line); }}
td.num {{ font-family: 'IBM Plex Mono', monospace; text-align: right; font-variant-numeric: tabular-nums; }}
.table-scroll {{ overflow-x: auto; max-height: 70vh; overflow-y: auto; }}
.banner {{ display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: 10px; background: var(--accent-soft); border: 1px solid var(--accent); margin-bottom: 20px; font-size: 0.86rem; }}
footer {{ margin-top: 32px; color: var(--ash); font-size: 0.78rem; border-top: 1px solid var(--line); padding-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">

  <div class="masthead">
    <div>
      <h1>Copiando a norm1e69</h1>
      <span class="wallet-addr mono-sm">{WALLET[:8]}&hellip;{WALLET[-6:]}</span>
    </div>
    <div>
      <span class="updated">actualizado {generated_at} &middot; recarga automática en <span id="cd">20</span>s</span>
    </div>
  </div>
{stat_strip}
{body}

  <footer>
    Ejecutor propio conectado directo a la API de Polymarket, sin intermediarios ni fee.
    El sistema anterior (otra wallet) quedó archivado en <code class="mono-sm">archive/</code>.
    Generado por <code class="mono-sm">scripts/generate_dashboard.py</code>.
  </footer>

</div>
<script>
(function() {{
  var secs = 20;
  var el = document.getElementById('cd');
  setInterval(function() {{
    secs -= 1;
    if (secs <= 0) {{ location.reload(); return; }}
    if (el) el.textContent = secs;
  }}, 1000);
}})();
</script>
</body>
</html>
"""


def main():
    state, events = load()
    d = build(state, events)
    html = render(d)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html)
    if d:
        print(f"wrote {OUT_PATH} (equity=${d['paper_equity']:.2f}, {d['n_copied']} copiados, {len(d['events'])} eventos)")
    else:
        print(f"wrote {OUT_PATH} (sin estado todavia)")


if __name__ == "__main__":
    main()
