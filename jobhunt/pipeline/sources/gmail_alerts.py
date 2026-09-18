"""Read job alert emails (LinkedIn, jobs.ch, jobup.ch, Indeed, Glassdoor...) via the Gmail API.

Alerts are the only sanctioned way to get LinkedIn postings automatically:
there is no public jobs API and scraping is blocked. The parser is generic:
every anchor in the email whose href matches a known posting URL pattern
becomes a Job, using the anchor text as the title.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..models import Job

# provider -> (href regex, external id group)
PATTERNS = {
    "linkedin": re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d{6,})"),
    "jobs.ch": re.compile(r"jobs\.ch/(?:[a-z]{2}/)?(?:vacancies|stellenangebote|offres-emploi)/detail/([0-9a-f-]{20,})"),
    "jobup.ch": re.compile(r"jobup\.ch/(?:[a-z]{2}/)?(?:emplois|jobs)/detail/([0-9a-f-]{20,})"),
    "indeed": re.compile(r"indeed\.com/(?:rc/clk|viewjob).*?jk=([0-9a-f]{8,})"),
    "glassdoor": re.compile(r"glassdoor\.[a-z.]+/job-listing/.*?jobListingId=(\d+)"),
}

# Anchor texts that are buttons, not titles.
BUTTON_TEXTS = {"view job", "see job", "apply", "apply now", "easy apply", "ver empleo",
                "see all jobs", "view all jobs", "unsubscribe", "job anzeigen", "zur stelle"}


def _clean_linkedin(url: str) -> str:
    m = PATTERNS["linkedin"].search(url)
    return f"https://www.linkedin.com/jobs/view/{m.group(1)}/" if m else url


def parse_alert_html(html: str, default_company: str = "") -> list[Job]:
    soup = BeautifulSoup(html or "", "html.parser")
    jobs: dict[str, Job] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for provider, pat in PATTERNS.items():
            m = pat.search(href)
            if not m:
                continue
            ext = m.group(1)
            key = f"{provider}:{ext}"
            title = a.get_text(" ", strip=True)
            if not title or title.lower() in BUTTON_TEXTS or len(title) < 3:
                # An image link or a button: keep the id so a later titled anchor can fill it.
                jobs.setdefault(key, Job(source=f"gmail:{provider}", company=default_company,
                                         title="", url=href, external_id=ext))
                break
            company, location = _company_and_location_near(a)
            url = _clean_linkedin(href) if provider == "linkedin" else href
            existing = jobs.get(key)
            if existing and existing.title:
                break
            jobs[key] = Job(source=f"gmail:{provider}", company=company or default_company,
                            title=title, url=url, external_id=ext, location=location)
            break
    return [j for j in jobs.values() if j.title]


def _company_and_location_near(a) -> tuple[str, str]:
    """LinkedIn alert rows read: <a>Title</a> <p>Company · Location</p>. Best effort."""
    container = a.find_parent(["td", "div", "li", "tr"]) or a.parent
    texts: list[str] = []
    node = container
    for _ in range(3):
        if node is None:
            break
        for s in node.stripped_strings:
            s = s.strip()
            if s and s != a.get_text(" ", strip=True) and s not in texts:
                texts.append(s)
        if len(texts) >= 2:
            break
        node = node.parent
    for t in texts[:4]:
        if "·" in t:
            comp, _, loc = t.partition("·")
            return comp.strip(), loc.strip()
        if " - " in t and len(t) < 120:
            comp, _, loc = t.partition(" - ")
            return comp.strip(), loc.strip()
    return (texts[0].strip() if texts else ""), (texts[1].strip() if len(texts) > 1 else "")


def _walk_parts(payload: dict):
    if payload.get("parts"):
        for p in payload["parts"]:
            yield from _walk_parts(p)
    else:
        yield payload


def message_html(msg: dict) -> str:
    """Extract the HTML body (or text as fallback) from a Gmail `users.messages.get` result."""
    html, text = "", ""
    for part in _walk_parts(msg.get("payload", {})):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if part.get("mimeType") == "text/html":
            html += decoded
        elif part.get("mimeType") == "text/plain":
            text += decoded
    if html:
        return html
    # Plain text: wrap bare URLs in anchors so the same parser works.
    return "".join(f'<a href="{u}">{u}</a>\n' for u in re.findall(r"https?://\S+", text))


def fetch(service, query: str, max_messages: int = 50) -> tuple[list[Job], int]:
    """service: googleapiclient Gmail resource. Returns (jobs, messages_read)."""
    res = service.users().messages().list(userId="me", q=query, maxResults=max_messages).execute()
    ids = [m["id"] for m in res.get("messages", [])]
    jobs: list[Job] = []
    for mid in ids:
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        sender = next((h["value"] for h in msg["payload"].get("headers", []) if h["name"].lower() == "from"), "")
        host = urlsplit("//" + sender.split("@")[-1].strip("> ")).netloc
        jobs.extend(parse_alert_html(message_html(msg), default_company=""))
        for j in jobs:
            j.raw.setdefault("alert_from", host)
    return jobs, len(ids)
