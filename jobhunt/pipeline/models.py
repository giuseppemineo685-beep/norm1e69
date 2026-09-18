"""Core data model shared by every source, the store and the web app."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Query parameters that only carry tracking noise. Stripping them makes the
# same posting hash to the same id whether it came from a company page,
# an email alert or a manual paste.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "trk", "trackingid", "refid", "ref", "src", "source", "gh_src", "lever-source",
    "midtoken", "midsig", "trkemail", "eid", "otptoken", "lipi", "licu",
}

STATUSES = (
    "new",         # scraped, not yet scored
    "filtered",    # failed the keyword prefilter, hidden from the list
    "pending",     # scored, waiting for a decision
    "accepted",    # user clicked Aplicar, waiting for the processor
    "discarded",   # user clicked Descartar
    "processing",  # processor is generating documents
    "ready",       # documents in Drive, tracker row written
    "applied",     # user marked the application as sent
    "error",       # processor failed, see .error
)


def canonical_url(url: str) -> str:
    """Normalise a posting URL so duplicates collapse to one id."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if k.lower() not in TRACKING_PARAMS]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def make_job_id(source: str, external_id: Optional[str], url: str) -> str:
    """Stable id: prefer the source's own id, fall back to the canonical URL."""
    key = f"{source}:{external_id}" if external_id else canonical_url(url)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Job:
    source: str                     # greenhouse | lever | gmail:linkedin | ...
    company: str
    title: str
    url: str
    location: str = ""
    external_id: Optional[str] = None
    posted_at: Optional[str] = None  # ISO date if the source gives one
    jd: str = ""                     # plain-text job description
    raw: dict[str, Any] = field(default_factory=dict)

    # Filled by the pipeline
    id: str = ""
    status: str = "new"
    score: Optional[int] = None
    reasons: dict[str, Any] = field(default_factory=dict)
    first_seen: str = ""
    last_seen: str = ""
    decided_at: Optional[str] = None
    drive_url: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        self.url = self.url.strip()
        self.company = (self.company or "").strip()
        self.title = re.sub(r"\s+", " ", self.title or "").strip()
        self.location = re.sub(r"\s+", " ", self.location or "").strip()
        if not self.id:
            self.id = make_job_id(self.source, self.external_id, self.url)
        ts = now_iso()
        self.first_seen = self.first_seen or ts
        self.last_seen = self.last_seen or ts

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "Job":
        known = {k: rec.get(k) for k in cls.__dataclass_fields__ if k in rec}
        known.setdefault("raw", {})
        known.setdefault("reasons", {})
        if known.get("raw") is None:
            known["raw"] = {}
        if known.get("reasons") is None:
            known["reasons"] = {}
        return cls(**known)
