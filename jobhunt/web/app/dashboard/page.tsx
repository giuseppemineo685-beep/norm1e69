import { computeStats, db } from "@/lib/db";
import { Masthead } from "../components/Masthead";

export const dynamic = "force-dynamic";

function Bars({ data }: { data: { day: string; count: number }[] }) {
  if (!data.length) return <p className="muted">Sin datos todavia.</p>;
  const w = 600, h = 140, pad = 22, top = 16;
  const max = Math.max(1, ...data.map((d) => d.count));
  const bw = (w - pad) / data.length;
  return (
    <svg viewBox={`0 0 ${w} ${h + top + 20}`} width="100%" role="img" aria-label="Ofertas nuevas por dia">
      {data.map((d, i) => {
        const bh = Math.round((d.count / max) * h);
        const x = pad + i * bw;
        return (
          <g key={d.day}>
            <rect x={x + 2} y={top + h - bh} width={Math.max(2, bw - 4)} height={bh} fill="var(--accent)" rx="2" />
            <text x={x + bw / 2} y={top + h - bh - 4} textAnchor="middle">{d.count}</text>
            <text x={x + bw / 2} y={top + h + 14} textAnchor="middle">{d.day.slice(5)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export default async function Dashboard() {
  const backend = await db();
  const [jobs, runs] = await Promise.all([backend.listJobs(), backend.listRuns(12)]);
  const s = computeStats(jobs);
  const seen = jobs.length - (s.byStatus.filtered ?? 0);
  const maxSrc = Math.max(1, ...s.bySource.map((x) => x.total));

  return (
    <main className="wrap">
      <Masthead current="dashboard" />
      <div className="stat-strip">
        <div className="stat-tile"><div className="label">Ofertas vistas</div><div className="value">{seen}</div></div>
        <div className="stat-tile"><div className="label">Filtradas</div><div className="value">{s.byStatus.filtered ?? 0}</div></div>
        <div className="stat-tile"><div className="label">Pendientes</div><div className="value">{s.byStatus.pending ?? 0}</div></div>
        <div className="stat-tile"><div className="label">Aplicadas</div><div className="value">{s.byStatus.applied ?? 0}</div></div>
        <div className="stat-tile"><div className="label">Tasa de aceptacion</div><div className="value">{s.acceptanceRate == null ? "–" : `${s.acceptanceRate}%`}</div></div>
        <div className="stat-tile"><div className="label">Encaje medio</div><div className="value">{s.avgScore ?? "–"}</div></div>
      </div>

      <div className="card">
        <h3>Ofertas nuevas por dia</h3>
        <Bars data={s.perDay} />
      </div>

      <div className="grid2">
        <div className="card">
          <h3>Por fuente</h3>
          <table>
            <thead><tr><th>Fuente</th><th>Total</th><th>Pendientes</th><th>Aceptadas</th></tr></thead>
            <tbody>
              {s.bySource.map((r) => (
                <tr key={r.source}>
                  <td><div className="hbar"><i style={{ width: `${(100 * r.total) / maxSrc}%`, minWidth: 4 }} />{r.source}</div></td>
                  <td className="num">{r.total}</td><td className="num">{r.pending}</td><td className="num">{r.accepted}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h3>Empresas con mas ofertas utiles</h3>
          <table>
            <tbody>
              {s.topCompanies.map((c) => (
                <tr key={c.company}><td>{c.company}</td><td className="num">{c.count}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>Ultimas ejecuciones</h3>
        <table>
          <thead><tr><th>Cuando</th><th>Tipo</th><th>Fuente</th><th>Encontradas</th><th>Nuevas</th><th>Error</th></tr></thead>
          <tbody>
            {runs.map((r, i) => (
              <tr key={i}>
                <td className="mono">{r.at.slice(0, 16).replace("T", " ")}</td>
                <td>{r.kind}</td><td>{r.source}</td>
                <td className="num">{r.found}</td><td className="num">{r.new}</td>
                <td style={{ color: r.error ? "var(--bad)" : undefined }}>{r.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
