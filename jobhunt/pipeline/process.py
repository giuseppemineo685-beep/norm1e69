"""Turn accepted postings into a Drive folder with CV, cover letter and JD, plus a tracker row.

    python -m pipeline.process
    python -m pipeline.process --dry-run   # writes the documents to data/out/ instead of Drive
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

from .config import load_config
from .llm import tailor_documents
from .models import Job, now_iso
from .store import store_from_env

log = logging.getLogger("process")


def process_one(job: Job, cfg, store, google, dry_run: bool) -> None:
    store.update(job.id, status="processing", error=None)
    if not job.jd:
        # Alert emails carry no description; try the posting page once more.
        from .jd import fetch_jd
        job.jd = fetch_jd(job.url)
        if job.jd:
            store.update(job.id, jd=job.jd)
    docs = tailor_documents(job, cfg.profile, cfg.cv_template, cfg.cover_letter_template, cfg.writing_rules)
    jd_text = f"{job.title}\n{job.company}\n{job.location}\n{job.url}\n\n{job.jd}"

    if dry_run:
        out = Path("data/out") / f"{job.company}_{date.today().isoformat()}_{job.id}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "CV.md").write_text(docs.cv_markdown, "utf-8")
        (out / "Cover_letter.md").write_text(docs.cover_letter_markdown, "utf-8")
        (out / "Job_description.txt").write_text(jd_text, "utf-8")
        store.update(job.id, status="ready", drive_url=str(out.resolve()),
                     reasons={**job.reasons, "changes": docs.changes})
        log.info("dry run: %s", out)
        return

    from .google import folder_name, TRACKER_HEADER
    root = os.environ["DRIVE_ROOT_FOLDER_ID"]
    sheet = os.environ.get("TRACKER_SHEET_ID")
    folder_id, folder_url = google.create_folder(folder_name(job.company), root)
    cv_url = google.upload_markdown_as_doc("CV", docs.cv_markdown, folder_id)
    cl_url = google.upload_markdown_as_doc("Cover letter", docs.cover_letter_markdown, folder_id)
    google.upload_text("Job description.txt", jd_text, folder_id)
    google.upload_text("CV.md", docs.cv_markdown, folder_id, "text/markdown")
    google.upload_text("Cover letter.md", docs.cover_letter_markdown, folder_id, "text/markdown")

    if sheet:
        google.ensure_header(sheet, TRACKER_HEADER)
        google.append_row(sheet, [
            date.today().isoformat(), job.company, job.title, job.location, job.score or "",
            "ready", job.url, folder_url, cv_url, cl_url, job.source,
            (job.reasons or {}).get("summary", ""),
        ])
    store.update(job.id, status="ready", drive_url=folder_url,
                 reasons={**job.reasons, "changes": docs.changes, "cv_url": cv_url, "cover_letter_url": cl_url})
    log.info("ready: %s / %s -> %s", job.company, job.title, folder_url)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    store = store_from_env()
    accepted = store.list_by_status("accepted")[: args.limit]
    if not accepted:
        log.info("nothing accepted")
        return 0
    google = None
    if not args.dry_run:
        from .google import Google
        google = Google()
    failures = 0
    for job in accepted:
        try:
            process_one(job, cfg, store, google, args.dry_run)
        except Exception as e:
            failures += 1
            log.exception("failed %s / %s", job.company, job.title)
            store.update(job.id, status="error", error=str(e)[:500])
    store.log_run("process", "total", len(accepted), len(accepted) - failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
