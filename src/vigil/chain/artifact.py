"""The compiled contract, as shipped with the package.

The ABI and bytecode are generated from ``contracts/src/ComplianceRegistry.sol``
by ``forge build`` and kept in the package, so anchoring a report needs
nothing but Python. ``build()`` regenerates them; a test compiles the source
again and checks that the copy in the package is what comes out.

The contract is compiled without a metadata hash (``bytecode_hash = "none"``
in foundry.toml), so its deployed code is the same bytes wherever and whenever
it is built. That is what lets the client recognise a registry by its code.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from vigil.paths import REPO_ROOT, tools_dir

CONTRACTS = REPO_ROOT / "contracts"
SOURCE = CONTRACTS / "src" / "ComplianceRegistry.sol"
PACKAGED = Path(__file__).parent / "artifacts" / "ComplianceRegistry.json"


def tool(name: str) -> str | None:
    """Path to a Foundry binary: the project's own copy first, then the PATH."""
    local = tools_dir() / "foundry" / name
    if local.is_file():
        return str(local)
    return shutil.which(name)


def source_sha256() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """``{"abi": [...], "bytecode": "0x...", "deployed_bytecode": "0x...", "source_sha256": ..., ...}``"""
    return json.loads(PACKAGED.read_text())


def compile_source(out: str | Path | None = None) -> dict[str, Any]:
    """Compile the contract with forge and return the artifact, without touching the packaged copy."""
    forge = tool("forge")
    if forge is None:
        raise RuntimeError("forge not found; run scripts/bootstrap_tools.sh foundry")
    out = Path(out) if out is not None else CONTRACTS / "out"
    subprocess.run([forge, "build", "--force", "--out", str(out), "--cache-path", str(out / "cache")],
                   cwd=CONTRACTS, check=True, capture_output=True)
    compiled = json.loads((out / "ComplianceRegistry.sol" / "ComplianceRegistry.json").read_text())
    metadata = compiled.get("metadata", {})
    return {
        "contract": "ComplianceRegistry",
        "abi": compiled["abi"],
        "bytecode": compiled["bytecode"]["object"],
        "deployed_bytecode": compiled["deployedBytecode"]["object"],
        "source_sha256": source_sha256(),
        "compiler": metadata.get("compiler", {}).get("version"),
        "settings": {key: metadata.get("settings", {}).get(key) for key in ("evmVersion", "optimizer")},
    }


def build() -> dict[str, Any]:
    """Compile the contract and refresh the packaged artifact."""
    artifact = compile_source()
    PACKAGED.parent.mkdir(parents=True, exist_ok=True)
    PACKAGED.write_text(json.dumps(artifact, indent=1, sort_keys=True) + "\n")
    load.cache_clear()
    return artifact
