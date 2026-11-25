"""Content fingerprints for replay and experiment provenance."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from functools import lru_cache
from pathlib import Path

from vigil import __version__
from vigil.paths import REPO_ROOT, rtg_home


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def software() -> dict:
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "src" / "vigil").rglob("*.py")):
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    exchange = hashlib.sha256()
    home = rtg_home()
    for path in sorted((home / "ready_trader_go").rglob("*.py")):
        exchange.update(path.relative_to(home).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return {"vigil": __version__, "source_sha256": digest.hexdigest(), "exchange_sha256": exchange.hexdigest()}


def environment() -> dict:
    versions = {}
    for package in ("numpy", "PyYAML", "httpx", "confluent-kafka", "web3", "cryptography", "pytest"):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[package] = importlib.metadata.version(package)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL,
                                         text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                             cwd=REPO_ROOT, text=True))
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    return {"software": software(), "git_commit": commit, "working_tree_dirty": dirty,
            "python": platform.python_version(), "platform": platform.platform(), "dependencies": versions,
            "dependencies_sha256": hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()}
