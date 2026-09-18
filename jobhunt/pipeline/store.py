"""Persistence. Two backends behind one interface:

- LocalStore: a JSON file, for trying the pipeline without any account.
- SupabaseStore: PostgREST over the `jobs` and `runs` tables (see supabase/schema.sql).

The web app reads the same data, so both backends share the record shape in models.Job.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

import requests

from .models import Job, now_iso


class Store:
    def get_ids(self) -> set[str]:
        raise NotImplementedError

    def get(self, job_id: str) -> Optional[Job]:
        raise NotImplementedError

    def upsert(self, jobs: Iterable[Job]) -> None:
        raise NotImplementedError

    def touch_seen(self, ids: Iterable[str]) -> None:
        """Bump last_seen on postings that are still online."""
        raise NotImplementedError

    def list_by_status(self, status: str) -> list[Job]:
        raise NotImplementedError

    def update(self, job_id: str, **fields) -> None:
        raise NotImplementedError

    def log_run(self, kind: str, source: str, found: int, new: int, error: str = "") -> None:
        raise NotImplementedError


class LocalStore(Store):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._data = json.loads(self.path.read_text("utf-8"))
        else:
            self._data = {"jobs": {}, "runs": []}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), "utf-8")

    def get_ids(self) -> set[str]:
        return set(self._data["jobs"].keys())

    def get(self, job_id: str) -> Optional[Job]:
        rec = self._data["jobs"].get(job_id)
        return Job.from_record(rec) if rec else None

    def upsert(self, jobs: Iterable[Job]) -> None:
        for job in jobs:
            self._data["jobs"][job.id] = job.to_record()
        self._save()

    def touch_seen(self, ids: Iterable[str]) -> None:
        ts = now_iso()
        for i in ids:
            if i in self._data["jobs"]:
                self._data["jobs"][i]["last_seen"] = ts
        self._save()

    def list_by_status(self, status: str) -> list[Job]:
        return [Job.from_record(r) for r in self._data["jobs"].values() if r.get("status") == status]

    def update(self, job_id: str, **fields) -> None:
        rec = self._data["jobs"].get(job_id)
        if rec is None:
            raise KeyError(job_id)
        rec.update(fields)
        self._save()

    def log_run(self, kind: str, source: str, found: int, new: int, error: str = "") -> None:
        self._data["runs"].append({
            "at": now_iso(), "kind": kind, "source": source,
            "found": found, "new": new, "error": error,
        })
        self._save()


class SupabaseStore(Store):
    """Thin PostgREST client. Uses the service role key, so run it server side only."""

    def __init__(self, url: str, service_key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def _req(self, method: str, table: str, *, params=None, json_body=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        r = requests.request(method, f"{self.base}/{table}", headers=headers,
                             params=params, json=json_body, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"supabase {method} {table}: {r.status_code} {r.text[:300]}")
        return r.json() if r.text else None

    def get_ids(self) -> set[str]:
        ids: set[str] = set()
        offset, page = 0, 1000
        while True:
            rows = self._req("GET", "jobs", params={"select": "id", "offset": offset, "limit": page})
            ids.update(r["id"] for r in rows)
            if len(rows) < page:
                return ids
            offset += page

    def get(self, job_id: str) -> Optional[Job]:
        rows = self._req("GET", "jobs", params={"id": f"eq.{job_id}", "limit": 1})
        return Job.from_record(rows[0]) if rows else None

    def upsert(self, jobs: Iterable[Job]) -> None:
        batch = [j.to_record() for j in jobs]
        for i in range(0, len(batch), 200):
            self._req("POST", "jobs", json_body=batch[i:i + 200],
                      prefer="resolution=merge-duplicates,return=minimal")

    def touch_seen(self, ids: Iterable[str]) -> None:
        ids = list(ids)
        ts = now_iso()
        for i in range(0, len(ids), 200):
            chunk = ",".join(ids[i:i + 200])
            self._req("PATCH", "jobs", params={"id": f"in.({chunk})"},
                      json_body={"last_seen": ts}, prefer="return=minimal")

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._req("GET", "jobs", params={"status": f"eq.{status}", "order": "first_seen.asc"})
        return [Job.from_record(r) for r in rows]

    def update(self, job_id: str, **fields) -> None:
        self._req("PATCH", "jobs", params={"id": f"eq.{job_id}"}, json_body=fields,
                  prefer="return=minimal")

    def log_run(self, kind: str, source: str, found: int, new: int, error: str = "") -> None:
        self._req("POST", "runs", json_body={
            "at": now_iso(), "kind": kind, "source": source,
            "found": found, "new": new, "error": error,
        }, prefer="return=minimal")


def store_from_env(local_path: str | Path = "data/jobs.json") -> Store:
    """Supabase when credentials are present, otherwise the local JSON file."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return SupabaseStore(url, key)
    return LocalStore(local_path)
