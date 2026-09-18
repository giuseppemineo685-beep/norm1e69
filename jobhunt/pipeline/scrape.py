"""Daily run: fetch every company page and every alert email, dedupe, prefilter, score.

    python -m pipeline.scrape            # everything
    python -m pipeline.scrape --no-score # skip the API, just collect
    python -m pipeline.scrape --only greenhouse,gmail
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_config
from .models import Job
from .prefilter import passes
from .sources import fetch_company
from .store import store_from_env

log = logging.getLogger("scrape")


def collect(cfg, only: set[str] | None, store) -> list[Job]:
    found: list[Job] = []
    for entry in cfg.companies:
        kind = entry.get("type", "html")
        if only and kind not in only:
            continue
        try:
            jobs = fetch_company(entry)
            log.info("%-28s %-15s %4d postings", entry.get("name"), kind, len(jobs))
            store.log_run("scrape", f"{kind}:{entry.get('name')}", len(jobs), 0)
            found.extend(jobs)
        except Exception as e:  # one broken page must not stop the run
            log.warning("%s failed: %s", entry.get("name"), e)
            store.log_run("scrape", f"{kind}:{entry.get('name')}", 0, 0, error=str(e)[:300])

    if (not only or "gmail" in only) and cfg.alerts.get("enabled", True) and cfg.alerts.get("query"):
        try:
            from .google import Google
            from .sources import gmail_alerts
            g = Google()
            jobs, n = gmail_alerts.fetch(g.gmail, cfg.alerts["query"], int(cfg.alerts.get("max_messages", 50)))
            log.info("gmail alerts: %d messages, %d postings", n, len(jobs))
            store.log_run("scrape", "gmail", len(jobs), 0)
            found.extend(jobs)
        except Exception as e:
            log.warning("gmail alerts failed: %s", e)
            store.log_run("scrape", "gmail", 0, 0, error=str(e)[:300])
    return found


def dedupe(jobs: list[Job]) -> list[Job]:
    out: dict[str, Job] = {}
    for j in jobs:
        if not j.url or not j.title:
            continue
        prev = out.get(j.id)
        if prev is None or (not prev.jd and j.jd):
            out[j.id] = j
    return list(out.values())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-score", action="store_true", help="collect only, no API calls")
    ap.add_argument("--only", help="comma list of source types to run")
    ap.add_argument("--limit-score", type=int, default=int(os.environ.get("SCORE_LIMIT", "60")),
                    help="max postings scored per run (cost guard)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    store = store_from_env()
    only = set(args.only.split(",")) if args.only else None

    jobs = dedupe(collect(cfg, only, store))
    known = store.get_ids()
    new = [j for j in jobs if j.id not in known]
    store.touch_seen(j.id for j in jobs if j.id in known)
    log.info("%d postings online, %d new", len(jobs), len(new))

    # Prefilter, and fetch the description for the ones that survive it without one.
    from .jd import fetch_jd
    to_score: list[Job] = []
    for j in new:
        ok, why = passes(j, cfg.search)
        if not ok:
            j.status, j.reasons = "filtered", {"summary": why}
            continue
        if not j.jd:
            j.jd = fetch_jd(j.url)
            ok, why = passes(j, cfg.search)   # a second pass now that the JD is known
            if not ok:
                j.status, j.reasons = "filtered", {"summary": why}
                continue
        to_score.append(j)
    store.upsert(new)
    log.info("%d pass the prefilter", len(to_score))

    if args.no_score or not to_score:
        store.log_run("scrape", "total", len(jobs), len(new))
        return 0

    from .llm import score_job
    scored = 0
    for j in to_score[: args.limit_score]:
        try:
            fit = score_job(j, cfg.profile)
            j.score = fit.score
            j.reasons = fit.model_dump()
            j.status = "pending" if fit.score >= cfg.min_score else "filtered"
            scored += 1
        except Exception as e:
            log.warning("scoring %s / %s failed: %s", j.company, j.title, e)
            j.status, j.error = "pending", f"score failed: {e}"[:300]
        store.update(j.id, status=j.status, score=j.score, reasons=j.reasons, error=j.error)
    # Anything past the cost guard stays "new" and gets scored next run.
    store.log_run("scrape", "total", len(jobs), len(new))
    log.info("scored %d, %d pending for review", scored,
             sum(1 for j in to_score if j.status == "pending"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
