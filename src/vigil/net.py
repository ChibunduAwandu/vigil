"""A small HTTP helper for the public APIs Vigil talks to: polite, patient, and honest about who is calling."""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

import vigil

USER_AGENT = f"vigil-compliance-monitor/{vigil.__version__} (+https://github.com/; research use)"


class ApiError(RuntimeError):
    pass


def redact(text: str) -> str:
    """``text`` with every web address cut back to its host: hosted providers put the API key in the path."""
    return re.sub(r"\b(https?://[^/\s'\"]+)/[^\s'\"]*", r"\1/...", text)


def client(*, timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
           headers: dict[str, str] | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})


def get_json(http: httpx.Client, url: str, params: Any = None, *, retries: int = 3, backoff: float = 1.0) -> Any:
    """GET a JSON document, retrying on rate limits and server errors with exponential back-off."""
    last = ""
    for attempt in range(retries + 1):
        try:
            response = http.get(url, params=params)
        except httpx.TransportError as error:
            last = f"{type(error).__name__}: {error}"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as error:
                    raise ApiError(f"{url}: response is not JSON") from error
            last = f"HTTP {response.status_code}"
            if response.status_code not in (429, 500, 502, 503, 504):
                raise ApiError(f"{url}: {last}: {response.text[:200]}")
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                continue
        if attempt < retries:
            time.sleep(backoff * 2 ** attempt)
    raise ApiError(f"{url}: gave up after {retries + 1} attempts ({last})")
