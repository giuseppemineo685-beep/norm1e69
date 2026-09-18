from __future__ import annotations

from .. import http
from ..jd import html_to_text
from ..models import Job


def parse(payload: dict, company: str) -> list[Job]:
    jobs = []
    for j in payload.get("jobs", []):
        loc = j.get("location", "") or ""
        if j.get("isRemote"):
            loc = f"{loc} (remote)".strip()
        jobs.append(Job(
            source="ashby", company=company, title=j.get("title", ""),
            url=j.get("jobUrl", ""), external_id=j.get("id"), location=loc,
            posted_at=(j.get("publishedAt") or "")[:10] or None,
            jd=html_to_text(j.get("descriptionHtml", "")) or (j.get("descriptionPlain") or ""),
            raw={"department": j.get("department"), "team": j.get("team")},
        ))
    return jobs


def fetch(entry: dict) -> list[Job]:
    r = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{entry['slug']}?includeCompensation=true")
    r.raise_for_status()
    return parse(r.json(), entry["name"])
