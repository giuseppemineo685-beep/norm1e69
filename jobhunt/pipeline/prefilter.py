"""Cheap keyword filter applied before spending API calls on scoring."""
from __future__ import annotations

from .config import Search
from .models import Job

REMOTE_WORDS = ("remote", "home office", "homeoffice", "teletrabajo", "anywhere")


def passes(job: Job, search: Search) -> tuple[bool, str]:
    """Return (ok, reason). Matching is case-insensitive substring."""
    title = job.title.lower()
    text = f"{title}\n{job.jd.lower()}"
    loc = job.location.lower()

    for kw in search.exclude:
        if kw in title:
            return False, f"excluded keyword in title: {kw}"

    if search.include and not any(kw in text for kw in search.include):
        return False, "no include keyword matched"

    if search.locations and loc:
        loc_ok = any(l in loc for l in search.locations)
        remote = search.remote_ok and any(w in loc for w in REMOTE_WORDS)
        if not (loc_ok or remote):
            return False, f"location not wanted: {job.location}"
    return True, ""
