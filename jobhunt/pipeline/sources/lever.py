from __future__ import annotations

from .. import http
from ..jd import html_to_text
from ..models import Job


def parse(payload: list, company: str) -> list[Job]:
    jobs = []
    for j in payload:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        if j.get("workplaceType") == "remote":
            loc = f"{loc} (remote)".strip()
        jobs.append(Job(
            source="lever", company=company, title=j.get("text", ""),
            url=j.get("hostedUrl", ""), external_id=j.get("id"), location=loc,
            posted_at=None, jd=html_to_text(j.get("descriptionBody", "") or j.get("description", "")),
            raw={"team": cats.get("team"), "commitment": cats.get("commitment")},
        ))
    return jobs


def fetch(entry: dict) -> list[Job]:
    r = http.get(f"https://api.lever.co/v0/postings/{entry['slug']}?mode=json")
    r.raise_for_status()
    return parse(r.json(), entry["name"])
