"""Constrained report paths and crash-safe local artifact writes."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path


def identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("identifier must be a nonempty string of at most 256 characters without control characters")
    return value


def artifact_path(root: Path, external_id: str) -> Path:
    """External identifiers are never interpreted as filesystem paths."""
    external_id = identifier(external_id)
    name = external_id if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", external_id) else (
        "match-" + hashlib.sha256(external_id.encode()).hexdigest())
    root = root.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("artifact path escapes its storage directory (possibly through a symlink)")
    return path


def atomic_write(path: Path, content: bytes) -> None:
    """Replace one file atomically; a crash leaves either the old or the new bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vigil-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
