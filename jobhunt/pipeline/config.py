"""Loads config/*.yaml, config/profile.md and the two templates."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class Search:
    include: list[str] = field(default_factory=list)   # at least one must match title/jd
    exclude: list[str] = field(default_factory=list)   # any match discards
    locations: list[str] = field(default_factory=list) # at least one must match location (empty = any)
    remote_ok: bool = True


@dataclass
class Config:
    companies: list[dict[str, Any]]
    search: Search
    alerts: dict[str, Any]
    profile: str
    cv_template: str
    cover_letter_template: str
    writing_rules: str
    min_score: int = 50


def _read(path: Path, default: str = "") -> str:
    return path.read_text("utf-8") if path.exists() else default


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    companies = yaml.safe_load(_read(config_dir / "companies.yaml", "companies: []")) or {}
    searches = yaml.safe_load(_read(config_dir / "searches.yaml", "{}")) or {}
    s = searches.get("filter", {}) or {}
    search = Search(
        include=[k.lower() for k in s.get("include", [])],
        exclude=[k.lower() for k in s.get("exclude", [])],
        locations=[k.lower() for k in s.get("locations", [])],
        remote_ok=bool(s.get("remote_ok", True)),
    )
    return Config(
        companies=companies.get("companies", []) or [],
        search=search,
        alerts=searches.get("alerts", {}) or {},
        profile=_read(config_dir / "profile.md"),
        cv_template=_read(config_dir / "templates" / "cv.md"),
        cover_letter_template=_read(config_dir / "templates" / "cover_letter.md"),
        writing_rules=_read(config_dir / "writing_rules.md"),
        min_score=int(searches.get("min_score", 50)),
    )
