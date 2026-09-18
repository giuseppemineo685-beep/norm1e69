"""Each source exposes fetch(entry: dict) -> list[Job].

`entry` is one item from config/companies.yaml, for example:
  - name: Google
    type: greenhouse
    slug: google
"""
from __future__ import annotations

from typing import Callable

from ..models import Job
from . import greenhouse, lever, smartrecruiters, ashby, workday, generic

REGISTRY: dict[str, Callable[[dict], list[Job]]] = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "ashby": ashby.fetch,
    "workday": workday.fetch,
    "html": generic.fetch,
}


def fetch_company(entry: dict) -> list[Job]:
    kind = entry.get("type", "html")
    if kind not in REGISTRY:
        raise ValueError(f"unknown source type {kind!r} for {entry.get('name')}")
    return REGISTRY[kind](entry)
