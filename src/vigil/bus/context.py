"""Persist the immutable context needed to reproduce an unfinished match."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vigil.reports.canonical import canonical_json


class ReplayContexts:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS contexts (key TEXT PRIMARY KEY, body BLOB NOT NULL)")

    def freeze(self, key: str, context: dict) -> dict:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO contexts VALUES (?, ?)", (key, canonical_json(context)))
        saved = json.loads(self.db.execute("SELECT body FROM contexts WHERE key = ?", (key,)).fetchone()[0])
        for field in ("header_sha256", "rulebook_sha256", "software", "runtime"):
            if saved.get(field) != context.get(field):
                raise ValueError(f"Replay context changed ({field}); restore the recorded software/rulebook "
                                 "or intentionally audit with a new consumer group")
        return saved

    def close(self) -> None:
        self.db.close()
