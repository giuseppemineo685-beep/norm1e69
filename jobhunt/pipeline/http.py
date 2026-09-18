"""Shared HTTP session with a browser-like user agent and retries."""
from __future__ import annotations

import time

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en,de;q=0.8,es;q=0.7"})
    return s


def get(url: str, *, retries: int = 3, timeout: int = 30, **kw) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = session().get(url, timeout=timeout, **kw)
            if r.status_code < 500:
                return r
            last = RuntimeError(f"{r.status_code} from {url}")
        except requests.RequestException as e:  # network
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed: {last}")


def post_json(url: str, body: dict, *, retries: int = 3, timeout: int = 30, **kw) -> requests.Response:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = session().post(url, json=body, timeout=timeout, **kw)
            if r.status_code < 500:
                return r
            last = RuntimeError(f"{r.status_code} from {url}")
        except requests.RequestException as e:
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"POST {url} failed: {last}")
