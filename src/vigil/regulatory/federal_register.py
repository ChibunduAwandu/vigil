"""The Federal Register: the daily journal of the US government, where every SEC and CFTC rule is published.

Its API is public and needs no key (it is listed on ApiVault under
Government). Searching it per regulation gives a running picture of what is
being proposed or finalised that touches each control.

API reference: https://www.federalregister.gov/developers/documentation/api/v1
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from vigil import net

BASE_URL = "https://www.federalregister.gov/api/v1"
FIELDS = ("document_number", "title", "type", "publication_date", "agencies", "abstract", "html_url",
          "cfr_references", "effective_on", "comments_close_on", "citation", "action", "docket_ids")
TYPES = {"RULE": "Rule", "PRORULE": "Proposed Rule", "NOTICE": "Notice", "PRESDOCU": "Presidential Document"}


@dataclass(frozen=True)
class Document:
    """One Federal Register document, reduced to what the register keeps."""

    document_number: str
    title: str
    type: str  # "Rule", "Proposed Rule", "Notice"
    publication_date: str  # ISO date
    agencies: tuple[str, ...]
    abstract: str
    url: str
    cfr_references: tuple[str, ...]  # e.g. "17 CFR 240"
    effective_on: str | None = None
    comments_close_on: str | None = None
    citation: str | None = None
    action: str | None = None
    dockets: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Document:
        return cls(
            document_number=raw["document_number"],
            title=" ".join((raw.get("title") or "").split()),
            type=raw.get("type") or "",
            publication_date=raw.get("publication_date") or "",
            agencies=tuple(sorted(a.get("name") or a.get("raw_name") or "" for a in raw.get("agencies") or ())),
            abstract=" ".join((raw.get("abstract") or "").split()),
            url=raw.get("html_url") or "",
            cfr_references=tuple(sorted({f"{ref['title']} CFR {ref['part']}"
                                         for ref in raw.get("cfr_references") or () if ref.get("part")})),
            effective_on=raw.get("effective_on"),
            comments_close_on=raw.get("comments_close_on"),
            citation=raw.get("citation"),
            action=raw.get("action"),
            dockets=tuple(sorted(raw.get("docket_ids") or ())),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("agencies", "cfr_references", "dockets"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        data = dict(data)
        for key in ("agencies", "cfr_references", "dockets"):
            data[key] = tuple(data.get(key) or ())
        return cls(**data)

    def content_hash(self) -> str:
        """Changes whenever anything about the document changes (a corrected date, a new abstract)."""
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def mentions(self, term: str) -> int:
        """How prominently the document features a search term: 3 in the title, 2 in the abstract, 1 in the text only."""
        needle = term.lower()
        if needle in self.title.lower():
            return 3
        if needle in self.abstract.lower():
            return 2
        return 1


class FederalRegister:
    def __init__(self, http: httpx.Client | None = None, *, pause: float = 0.3):
        self.http = http or net.client()
        self.pause = pause  # seconds between requests: the API is free and unauthenticated; be a good guest
        self.requests = 0

    def search(self, term: str, *, agencies: Sequence[str] = (), types: Sequence[str] = ("RULE", "PRORULE"),
               since: dt.date | None = None, cfr: tuple[int, int] | None = None, per_page: int = 100,
               max_pages: int = 5) -> Iterator[Document]:
        """Documents whose text contains ``term`` (as a phrase), newest first."""
        params: list[tuple[str, str]] = [("per_page", str(per_page)), ("order", "newest"),
                                         ("conditions[term]", f'"{term}"')]
        params += [("conditions[agencies][]", slug) for slug in agencies]
        params += [("conditions[type][]", kind) for kind in types]
        if since is not None:
            params.append(("conditions[publication_date][gte]", since.isoformat()))
        if cfr is not None:
            params += [("conditions[cfr][title]", str(cfr[0])), ("conditions[cfr][part]", str(cfr[1]))]
        params += [("fields[]", name) for name in FIELDS]

        url: str | None = f"{BASE_URL}/documents.json"
        for _ in range(max_pages):
            if self.requests:
                time.sleep(self.pause)
            page = net.get_json(self.http, url, params)
            self.requests += 1
            for raw in page.get("results") or ():
                yield Document.from_api(raw)
            url, params = page.get("next_page_url"), None  # the next-page URL carries the query
            if not url:
                break
