from __future__ import annotations

from .. import http
from ..jd import html_to_text
from ..models import Job


def parse(payload: dict, company: str, company_id: str) -> list[Job]:
    jobs = []
    for j in payload.get("content", []):
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
        if loc.get("remote"):
            loc_s = f"{loc_s} (remote)".strip()
        jid = j.get("id")
        jobs.append(Job(
            source="smartrecruiters", company=company, title=j.get("name", ""),
            url=f"https://jobs.smartrecruiters.com/{company_id}/{jid}",
            external_id=jid, location=loc_s,
            posted_at=(j.get("releasedDate") or "")[:10] or None,
            raw={"department": (j.get("department") or {}).get("label")},
        ))
    return jobs


def fetch(entry: dict) -> list[Job]:
    cid = entry["slug"]
    jobs: list[Job] = []
    offset = 0
    while True:
        r = http.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
                     params={"limit": 100, "offset": offset})
        r.raise_for_status()
        page = r.json()
        jobs.extend(parse(page, entry["name"], cid))
        offset += 100
        if offset >= int(page.get("totalFound", 0)) or not page.get("content"):
            break
    # Descriptions live behind a second call per posting.
    if entry.get("fetch_jd", True):
        for job in jobs:
            try:
                d = http.get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{job.external_id}").json()
                sections = (d.get("jobAd") or {}).get("sections") or {}
                job.jd = "\n\n".join(html_to_text(s.get("text", "")) for s in sections.values() if s.get("text"))
            except Exception:
                pass
    return jobs
