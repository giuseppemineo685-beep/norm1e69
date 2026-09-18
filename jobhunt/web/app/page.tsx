import Link from "next/link";
import { db, type Status } from "@/lib/db";
import { JobCard } from "./components/JobCard";
import { Masthead } from "./components/Masthead";

export const dynamic = "force-dynamic";

const TABS: { key: string; label: string; statuses: Status[] }[] = [
  { key: "pending", label: "Pendientes", statuses: ["pending"] },
  { key: "queue", label: "En cola", statuses: ["accepted", "processing", "error"] },
  { key: "ready", label: "Listas", statuses: ["ready"] },
  { key: "applied", label: "Aplicadas", statuses: ["applied"] },
  { key: "discarded", label: "Descartadas", statuses: ["discarded"] },
];

export default async function Home({ searchParams }: { searchParams: Promise<{ tab?: string }> }) {
  const { tab = "pending" } = await searchParams;
  const all = await (await db()).listJobs();
  const active = TABS.find((t) => t.key === tab) ?? TABS[0];
  const visible = all.filter((j) => active.statuses.includes(j.status));
  const counts = Object.fromEntries(TABS.map((t) => [t.key, all.filter((j) => t.statuses.includes(j.status)).length]));
  const today = new Date().toISOString().slice(0, 10);
  const newToday = all.filter((j) => j.status === "pending" && j.first_seen?.startsWith(today)).length;

  return (
    <main className="wrap">
      <Masthead current="list" subtitle={`${counts.pending} pendientes de decision, ${newToday} nuevas hoy`} />
      <nav className="tabs">
        {TABS.map((t) => (
          <Link key={t.key} href={`/?tab=${t.key}`} aria-current={t.key === active.key ? "page" : undefined}>
            {t.label}<span className="n">{counts[t.key]}</span>
          </Link>
        ))}
      </nav>
      {visible.length === 0 ? (
        <div className="empty">Nada en esta lista. El rastreo corre cada manana y las nuevas ofertas aparecen en Pendientes.</div>
      ) : (
        visible.map((j) => <JobCard key={j.id} job={j} />)
      )}
    </main>
  );
}
