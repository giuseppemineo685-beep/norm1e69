/**
 * Data layer shared by every page. Two backends:
 *  - Supabase (when SUPABASE_URL + SUPABASE_SERVICE_KEY are set): the production path.
 *  - Local JSON file (otherwise): same file the Python pipeline writes in local mode.
 * The record shape mirrors pipeline/models.py.
 */
import { promises as fs } from "fs";
import path from "path";

export type Status =
  | "new" | "filtered" | "pending" | "accepted" | "discarded"
  | "processing" | "ready" | "applied" | "error";

export type Reasons = {
  matching?: string[];
  missing?: string[];
  summary?: string;
  seniority_ok?: boolean;
  language_ok?: boolean;
  changes?: string[];
  cv_url?: string;
  cover_letter_url?: string;
};

export type Job = {
  id: string;
  source: string;
  company: string;
  title: string;
  url: string;
  location: string;
  posted_at: string | null;
  jd: string;
  status: Status;
  score: number | null;
  reasons: Reasons;
  first_seen: string;
  last_seen: string;
  decided_at: string | null;
  drive_url: string | null;
  error: string | null;
};

export type Run = { at: string; kind: string; source: string; found: number; new: number; error: string };

export const DECIDABLE: Status[] = ["pending", "accepted", "discarded", "ready", "applied", "error", "processing"];

interface Backend {
  listJobs(status?: Status): Promise<Job[]>;
  updateJob(id: string, patch: Partial<Job>): Promise<void>;
  listRuns(limit: number): Promise<Run[]>;
}

// ---------------------------------------------------------------- local JSON
type LocalFile = { jobs: Record<string, Job>; runs: Run[] };

async function localPath(): Promise<string> {
  const configured = process.env.JOBS_FILE;
  if (configured) return path.resolve(configured);
  const real = path.resolve(process.cwd(), "../data/jobs.json");
  try { await fs.access(real); return real; } catch { /* fall through */ }
  return path.resolve(process.cwd(), "../data/sample_jobs.json");
}

const local: Backend = {
  async listJobs(status) {
    const raw = JSON.parse(await fs.readFile(await localPath(), "utf8")) as LocalFile;
    const all = Object.values(raw.jobs).map(normalise);
    const out = status ? all.filter((j) => j.status === status) : all;
    return out.sort(byRecency);
  },
  async updateJob(id, patch) {
    const p = await localPath();
    const raw = JSON.parse(await fs.readFile(p, "utf8")) as LocalFile;
    if (!raw.jobs[id]) throw new Error(`unknown job ${id}`);
    Object.assign(raw.jobs[id], patch);
    await fs.writeFile(p, JSON.stringify(raw, null, 1), "utf8");
  },
  async listRuns(limit) {
    const raw = JSON.parse(await fs.readFile(await localPath(), "utf8")) as LocalFile;
    return [...(raw.runs ?? [])].sort((a, b) => b.at.localeCompare(a.at)).slice(0, limit);
  },
};

// ------------------------------------------------------------------ supabase
async function supabaseBackend(): Promise<Backend> {
  const { createClient } = await import("@supabase/supabase-js");
  const client = createClient(process.env.SUPABASE_URL!, process.env.SUPABASE_SERVICE_KEY!, {
    auth: { persistSession: false },
  });
  return {
    async listJobs(status) {
      let q = client.from("jobs").select("*").order("first_seen", { ascending: false }).limit(2000);
      if (status) q = q.eq("status", status);
      const { data, error } = await q;
      if (error) throw new Error(error.message);
      return (data as Job[]).map(normalise).sort(byRecency);
    },
    async updateJob(id, patch) {
      const { error } = await client.from("jobs").update(patch).eq("id", id);
      if (error) throw new Error(error.message);
    },
    async listRuns(limit) {
      const { data, error } = await client.from("runs").select("*").order("at", { ascending: false }).limit(limit);
      if (error) throw new Error(error.message);
      return data as Run[];
    },
  };
}

export async function db(): Promise<Backend> {
  if (process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_KEY) return supabaseBackend();
  return local;
}

export function backendName(): string {
  return process.env.SUPABASE_URL ? "supabase" : "local json";
}

// ------------------------------------------------------------------- helpers
function normalise(j: Job): Job {
  return { ...j, reasons: j.reasons ?? {}, location: j.location ?? "", jd: j.jd ?? "" };
}

function byRecency(a: Job, b: Job): number {
  const sa = a.score ?? -1, sb = b.score ?? -1;
  if (a.status === "pending" && b.status === "pending" && sa !== sb) return sb - sa;
  return (b.first_seen ?? "").localeCompare(a.first_seen ?? "");
}

export type Stats = {
  byStatus: Record<string, number>;
  perDay: { day: string; count: number }[];
  bySource: { source: string; total: number; pending: number; accepted: number }[];
  topCompanies: { company: string; count: number }[];
  acceptanceRate: number | null;
  avgScore: number | null;
};

export function computeStats(jobs: Job[]): Stats {
  const byStatus: Record<string, number> = {};
  const perDayMap = new Map<string, number>();
  const bySourceMap = new Map<string, { total: number; pending: number; accepted: number }>();
  const companies = new Map<string, number>();
  let scoreSum = 0, scoreN = 0;
  for (const j of jobs) {
    byStatus[j.status] = (byStatus[j.status] ?? 0) + 1;
    const day = (j.first_seen ?? "").slice(0, 10);
    if (day) perDayMap.set(day, (perDayMap.get(day) ?? 0) + 1);
    const src = j.source.split(":")[0];
    const s = bySourceMap.get(src) ?? { total: 0, pending: 0, accepted: 0 };
    s.total++;
    if (j.status === "pending") s.pending++;
    if (["accepted", "processing", "ready", "applied"].includes(j.status)) s.accepted++;
    bySourceMap.set(src, s);
    if (j.status !== "filtered") companies.set(j.company, (companies.get(j.company) ?? 0) + 1);
    if (j.score != null) { scoreSum += j.score; scoreN++; }
  }
  const decided = (byStatus.accepted ?? 0) + (byStatus.processing ?? 0) + (byStatus.ready ?? 0) + (byStatus.applied ?? 0);
  const rejected = byStatus.discarded ?? 0;
  const perDay = [...perDayMap.entries()].sort(([a], [b]) => a.localeCompare(b)).slice(-14)
    .map(([day, count]) => ({ day, count }));
  return {
    byStatus,
    perDay,
    bySource: [...bySourceMap.entries()].map(([source, v]) => ({ source, ...v })).sort((a, b) => b.total - a.total),
    topCompanies: [...companies.entries()].map(([company, count]) => ({ company, count }))
      .sort((a, b) => b.count - a.count).slice(0, 8),
    acceptanceRate: decided + rejected ? Math.round((100 * decided) / (decided + rejected)) : null,
    avgScore: scoreN ? Math.round(scoreSum / scoreN) : null,
  };
}
