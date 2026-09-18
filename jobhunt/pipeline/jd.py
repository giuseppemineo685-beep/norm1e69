"""Turn a posting's HTML into readable plain text for scoring and for the Drive folder."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from . import http

NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "svg", "iframe", "form")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(NOISE_TAGS):
        t.decompose()
    # Prefer the densest content block when the page has one.
    main = soup.find("main") or soup.find("article") or soup.body or soup
    for br in main.find_all("br"):
        br.replace_with("\n")
    for li in main.find_all("li"):
        li.insert_before("- ")
    for tag in main.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr", "section"]):
        tag.append("\n")
    text = main.get_text(" ")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_jd(url: str, max_chars: int = 20000) -> str:
    """Best effort. LinkedIn and some Workday pages return little without a login."""
    try:
        r = http.get(url, timeout=30)
        if r.status_code >= 400:
            return ""
        text = html_to_text(r.text)
        return text[:max_chars]
    except Exception:
        return ""
