"""Fallback for career pages without an API: collect links matching a pattern.

config example:
  - name: Swiss Re
    type: html
    url: https://careers.swissre.com/search/?q=zurich
    link_pattern: "/job/"          # substring or regex the job links contain
    title_selector: null           # optional CSS selector inside the link
    location_selector: null

If the page renders postings only with JavaScript this source returns nothing;
run that company through Claude in Chrome or an alert instead.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import http
from ..models import Job


def parse(html: str, entry: dict) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(entry.get("link_pattern", "/job"))
    base = entry["url"]
    seen: set[str] = set()
    jobs = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if not pat.search(href) or href in seen:
            continue
        title = a.get_text(" ", strip=True)
        if entry.get("title_selector"):
            t = a.select_one(entry["title_selector"])
            title = t.get_text(" ", strip=True) if t else title
        if not title or len(title) < 3:
            continue
        loc = ""
        if entry.get("location_selector"):
            parent = a.find_parent(entry.get("row_tag", "li")) or a.parent
            l = parent.select_one(entry["location_selector"]) if parent else None
            loc = l.get_text(" ", strip=True) if l else ""
        seen.add(href)
        jobs.append(Job(source="html", company=entry["name"], title=title, url=href, location=loc))
    return jobs


def fetch(entry: dict) -> list[Job]:
    r = http.get(entry["url"])
    r.raise_for_status()
    return parse(r.text, entry)
