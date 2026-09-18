"""Workday's public job board JSON (the same call the career page itself makes).

config example:
  - name: UBS
    type: workday
    tenant: ubs
    site: External
    host: ubs.wd3.myworkdayjobs.com
    search: "strategy"        # optional free-text
"""
from __future__ import annotations

from .. import http
from ..jd import html_to_text
from ..models import Job


def parse(payload: dict, company: str, host: str, site: str) -> list[Job]:
    jobs = []
    for j in payload.get("jobPostings", []):
        path = j.get("externalPath", "")
        jobs.append(Job(
            source="workday", company=company, title=j.get("title", ""),
            url=f"https://{host}/{site}{path}" if path else "",
            external_id=j.get("bulletFields", [None])[0] if j.get("bulletFields") else None,
            location=j.get("locationsText", ""),
            posted_at=None,
            raw={"posted": j.get("postedOn")},
        ))
    return jobs


def fetch(entry: dict) -> list[Job]:
    tenant, site, host = entry["tenant"], entry.get("site", "External"), entry["host"]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs: list[Job] = []
    offset = 0
    while True:
        body = {"appliedFacets": entry.get("facets", {}), "limit": 20, "offset": offset,
                "searchText": entry.get("search", "")}
        r = http.post_json(url, body, headers={"Accept": "application/json"})
        r.raise_for_status()
        page = r.json()
        jobs.extend(parse(page, entry["name"], host, site))
        offset += 20
        if offset >= int(page.get("total", 0)) or not page.get("jobPostings"):
            break
    if entry.get("fetch_jd", True):
        for job in jobs:
            try:
                path = job.url.split(f"/{site}", 1)[1]
                d = http.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                             headers={"Accept": "application/json"}).json()
                info = d.get("jobPostingInfo") or {}
                job.jd = html_to_text(info.get("jobDescription", ""))
                job.posted_at = (info.get("startDate") or "")[:10] or None
            except Exception:
                pass
    return jobs
