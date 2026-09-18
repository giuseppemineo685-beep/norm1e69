from __future__ import annotations

import html as htmllib

from .. import http
from ..jd import html_to_text
from ..models import Job


def parse(payload: dict, company: str) -> list[Job]:
    jobs = []
    for j in payload.get("jobs", []):
        jobs.append(Job(
            source="greenhouse", company=company, title=htmllib.unescape(j.get("title", "")),
            url=j.get("absolute_url", ""), external_id=str(j.get("id", "")) or None,
            location=(j.get("location") or {}).get("name", ""),
            posted_at=(j.get("updated_at") or j.get("first_published") or "")[:10] or None,
            jd=html_to_text(htmllib.unescape(j.get("content", ""))),
            raw={"departments": [d.get("name") for d in j.get("departments", [])]},
        ))
    return jobs


def fetch(entry: dict) -> list[Job]:
    slug = entry["slug"]
    r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    r.raise_for_status()
    return parse(r.json(), entry["name"])
