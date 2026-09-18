import Link from "next/link";
import { backendName } from "@/lib/db";

export function Masthead({ current, subtitle }: { current: "list" | "dashboard"; subtitle?: string }) {
  return (
    <header className="masthead">
      <div>
        <h1>Job hunt</h1>
        <div className="muted">{subtitle ?? `fuente de datos ${backendName()}`}</div>
      </div>
      <nav>
        <Link href="/" aria-current={current === "list" ? "page" : undefined}>Ofertas</Link>
        <Link href="/dashboard" aria-current={current === "dashboard" ? "page" : undefined}>Dashboard</Link>
      </nav>
    </header>
  );
}
