import type { Job } from "@/lib/db";
import { decide } from "../actions";

const STATUS_LABEL: Record<string, string> = {
  pending: "pendiente", accepted: "aceptada, en cola", processing: "generando documentos",
  ready: "documentos listos", applied: "aplicada", discarded: "descartada", error: "error",
};

function scoreClass(s: number | null) {
  if (s == null) return "";
  return s >= 70 ? "good" : s >= 50 ? "warn" : "bad";
}

function sourceLabel(source: string) {
  const [kind, provider] = source.split(":");
  return kind === "gmail" ? `alerta ${provider}` : `pagina ${kind}`;
}

function fmtDate(iso: string | null | undefined) {
  if (!iso) return "";
  return iso.slice(0, 10);
}

export function JobCard({ job }: { job: Job }) {
  const r = job.reasons ?? {};
  const flags: string[] = [];
  if (r.seniority_ok === false) flags.push("nivel no encaja");
  if (r.language_ok === false) flags.push("idioma requerido");

  return (
    <article className="card">
      <div className="card-head">
        <div>
          <h2><a href={job.url} target="_blank" rel="noreferrer">{job.title}</a></h2>
          <div className="meta">
            <span><strong>{job.company}</strong></span>
            {job.location && <span>{job.location}</span>}
            <span>{sourceLabel(job.source)}</span>
            <span>vista {fmtDate(job.first_seen)}</span>
            {job.posted_at && <span>publicada {job.posted_at}</span>}
          </div>
        </div>
        <div className={`score ${scoreClass(job.score)}`}>
          <div className="v">{job.score ?? "–"}</div>
          <div className="l">encaje</div>
          <div className="bar"><i style={{ width: `${job.score ?? 0}%` }} /></div>
        </div>
      </div>

      {r.summary && <p className="summary">{r.summary}</p>}
      {(r.matching?.length || r.missing?.length || flags.length) ? (
        <div className="chips">
          {r.matching?.map((m) => <span key={"m" + m} className="chip good">{m}</span>)}
          {r.missing?.map((m) => <span key={"x" + m} className="chip bad">falta {m}</span>)}
          {flags.map((f) => <span key={f} className="chip warn">{f}</span>)}
        </div>
      ) : null}
      {job.error && <p className="muted" style={{ color: "var(--bad)" }}>{job.error}</p>}

      {job.jd ? (
        <details>
          <summary>Ver descripcion completa</summary>
          <div className="jd">{job.jd}</div>
        </details>
      ) : (
        <p className="muted">Sin descripcion disponible, abre la oferta en el enlace del titulo.</p>
      )}

      <div className="actions">
        {job.status === "pending" && (
          <>
            <form action={decide.bind(null, job.id, "accept")}>
              <button className="primary">Aplicar</button>
            </form>
            <form action={decide.bind(null, job.id, "discard")}>
              <button className="danger">Descartar</button>
            </form>
          </>
        )}
        {(job.status === "ready" || job.status === "applied") && job.drive_url && (
          <a className="link-btn" href={job.drive_url} target="_blank" rel="noreferrer">Carpeta en Drive</a>
        )}
        {job.status === "ready" && (
          <form action={decide.bind(null, job.id, "applied")}>
              <button className="primary">Marcar como aplicada</button>
            </form>
        )}
        {job.status === "error" && (
          <form action={decide.bind(null, job.id, "retry")}>
              <button>Reintentar</button>
            </form>
        )}
        {(job.status === "discarded" || job.status === "accepted" || job.status === "error") && (
          <form action={decide.bind(null, job.id, "reopen")}>
              <button>Volver a pendiente</button>
            </form>
        )}
        <span className={`chip ${job.status === "ready" || job.status === "applied" ? "accent" : ""}`}>
          {STATUS_LABEL[job.status] ?? job.status}
        </span>
        {r.changes?.length ? <span className="muted">cambios: {r.changes.join(", ")}</span> : null}
      </div>
    </article>
  );
}
