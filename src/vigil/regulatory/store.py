"""The regulatory register: every document the scraper has seen, and which regulations it bears on."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from vigil.paths import artifacts_dir
from vigil.regulatory.federal_register import Document

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    source TEXT NOT NULL,
    document_number TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_changed TEXT NOT NULL,
    PRIMARY KEY (source, document_number)
);
CREATE TABLE IF NOT EXISTS relevance (
    source TEXT NOT NULL,
    document_number TEXT NOT NULL,
    regulation TEXT NOT NULL,
    term TEXT NOT NULL,
    score INTEGER NOT NULL,
    PRIMARY KEY (source, document_number, regulation, term)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    requests INTEGER DEFAULT 0,
    seen INTEGER DEFAULT 0,
    added INTEGER DEFAULT 0,
    changed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS documents_by_date ON documents (publication_date DESC);
"""


def default_path() -> Path:
    return artifacts_dir() / "regulatory.db"


class Register:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- writing -------------------------------------------------------------------------------

    def upsert(self, source: str, document: Document, now: str) -> str:
        """Store a document. Returns ``"new"``, ``"changed"`` or ``"same"``."""
        digest = document.content_hash()
        row = self.db.execute("SELECT content_hash FROM documents WHERE source = ? AND document_number = ?",
                              (source, document.document_number)).fetchone()
        body = json.dumps(document.to_dict(), sort_keys=True)
        if row is None:
            self.db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source, document.document_number, document.publication_date, digest, body, now, now))
            return "new"
        if row[0] != digest:
            self.db.execute("UPDATE documents SET content_hash = ?, body = ?, publication_date = ?, last_changed = ? "
                            "WHERE source = ? AND document_number = ?",
                            (digest, body, document.publication_date, now, source, document.document_number))
            return "changed"
        return "same"

    def relate(self, source: str, document_number: str, regulation: str, term: str, score: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO relevance VALUES (?, ?, ?, ?, ?)",
                        (source, document_number, regulation, term, score))

    def start_run(self, now: str) -> int:
        cursor = self.db.execute("INSERT INTO runs (started) VALUES (?)", (now,))
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, now: str, *, requests: int, seen: int, added: int, changed: int) -> None:
        self.db.execute("UPDATE runs SET finished = ?, requests = ?, seen = ?, added = ?, changed = ? WHERE id = ?",
                        (now, requests, seen, added, changed, run_id))
        self.db.commit()

    # -- reading -------------------------------------------------------------------------------

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def last_run(self) -> dict[str, Any] | None:
        row = self.db.execute("SELECT id, started, finished, requests, seen, added, changed FROM runs "
                              "WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return dict(zip(("id", "started", "finished", "requests", "seen", "added", "changed"), row, strict=True))

    def latest_publication(self) -> dt.date | None:
        row = self.db.execute("SELECT MAX(publication_date) FROM documents").fetchone()
        return dt.date.fromisoformat(row[0]) if row and row[0] else None

    def fingerprint(self) -> str:
        """SHA-256 over every document's identity and content hash: changes if the register does."""
        digest = hashlib.sha256()
        for source, number, content in self.db.execute(
                "SELECT source, document_number, content_hash FROM documents ORDER BY source, document_number"):
            digest.update(f"{source}|{number}|{content}\n".encode())
        return digest.hexdigest()

    def relevant(self, regulations: list[str] | None = None, *, min_score: int = 2,
                 limit: int = 10) -> list[dict[str, Any]]:
        """The most recent documents that feature a watched term prominently, newest first."""
        rows = self.db.execute(
            "SELECT d.source, d.document_number, d.body, r.regulation, MAX(r.score) "
            "FROM documents d JOIN relevance r ON r.source = d.source AND r.document_number = d.document_number "
            "GROUP BY d.source, d.document_number, r.regulation "
            "ORDER BY d.publication_date DESC, d.document_number").fetchall()
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for source, number, body, regulation, score in rows:
            if score < min_score or (regulations is not None and regulation not in regulations):
                continue
            entry = found.get((source, number))
            if entry is None:
                document = json.loads(body)
                entry = found[(source, number)] = {
                    "source": source, "document_number": number, "title": document["title"],
                    "type": document["type"], "agency": "; ".join(document["agencies"]),
                    "publication_date": document["publication_date"], "effective_on": document["effective_on"],
                    "comments_close_on": document["comments_close_on"], "url": document["url"],
                    "regulations": [],
                }
            entry["regulations"].append(regulation)
        out = list(found.values())[:limit]
        for entry in out:
            entry["regulations"].sort()
        return out

    def snapshot(self, regulations: list[str] | None = None, *, limit: int = 8) -> dict[str, Any] | None:
        """What a compliance report records about the state of the register; ``None`` if it is empty."""
        if self.count() == 0:
            return None
        run = self.last_run()
        return {
            "source": "Federal Register (https://www.federalregister.gov)",
            "as_of": (run["finished"] if run else None),
            "documents": self.count(),
            "sha256": self.fingerprint(),
            "recent": self.relevant(regulations, limit=limit),
        }
