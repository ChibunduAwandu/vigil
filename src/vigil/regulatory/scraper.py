"""The scraper: works through the rulebook's watch list and keeps the register current."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from vigil.bus import topics
from vigil.bus.base import Bus
from vigil.regulatory.federal_register import Document, FederalRegister
from vigil.regulatory.store import Register
from vigil.surveillance.rulebook import Rulebook

log = logging.getLogger("vigil.regulatory")
SOURCE = "federal-register"


@dataclass
class ScrapeResult:
    started: str
    finished: str = ""
    requests: int = 0
    seen: int = 0
    added: list[Document] = field(default_factory=list)
    changed: list[Document] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"started": self.started, "finished": self.finished, "requests": self.requests, "seen": self.seen,
                "new": len(self.added), "changed": len(self.changed), "errors": self.errors}


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def scrape(rulebook: Rulebook, register: Register, *, source: FederalRegister | None = None,
           since: dt.date | None = None, years: int = 5, types: tuple[str, ...] = ("RULE", "PRORULE"),
           max_pages: int = 3, bus: Bus | None = None) -> ScrapeResult:
    """Query the Federal Register for every watched term and record what comes back.

    On the first run the search reaches back ``years``; afterwards it starts a
    month before the newest document already held, which is enough to pick up
    late corrections. A failure on one term is recorded and the rest carry on.
    New and changed documents are published to the bus if one is given.
    """
    source = source or FederalRegister()
    if since is None:
        latest = register.latest_publication()
        since = (latest - dt.timedelta(days=31)) if latest else dt.date.today() - dt.timedelta(days=365 * years)
    result = ScrapeResult(started=_now())
    run_id = register.start_run(result.started)
    touched: dict[str, str] = {}

    for key, regulation in rulebook.regulations.items():
        watch = regulation.watch
        for term in watch.get("terms", ()):
            try:
                documents = list(source.search(term, agencies=watch.get("agencies", ()), types=types, since=since,
                                               max_pages=max_pages))
            except Exception as error:  # noqa: BLE001 - one bad query must not sink the run
                result.errors.append(f"{key} / {term!r}: {error}")
                log.warning("search for %r failed: %s", term, error)
                continue
            for document in documents:
                result.seen += 1
                number = document.document_number
                if number not in touched:
                    touched[number] = register.upsert(SOURCE, document, result.started)
                    if touched[number] == "new":
                        result.added.append(document)
                    elif touched[number] == "changed":
                        result.changed.append(document)
                register.relate(SOURCE, number, key, term, document.mentions(term))

    result.requests = source.requests
    result.finished = _now()
    register.finish_run(run_id, result.finished, requests=result.requests, seen=result.seen,
                        added=len(result.added), changed=len(result.changed))
    if bus is not None:
        for status, documents in (("new", result.added), ("changed", result.changed)):
            for document in documents:
                bus.publish(topics.REGULATORY, {"status": status, "source": SOURCE, **document.to_dict()},
                            key=document.document_number)
        bus.flush()
    return result
