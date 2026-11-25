"""Where things live on disk.

Everything is resolved relative to the repository root so the CLI behaves the
same from any working directory. Each location can be overridden through an
environment variable for deployments that keep data elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


def rtg_home() -> Path:
    """Directory of the (unmodified) Ready Trader Go distribution."""
    return _env_path("VIGIL_RTG_HOME", REPO_ROOT / "pyready_trader_go-2")


def artifacts_dir() -> Path:
    path = _env_path("VIGIL_ARTIFACTS", REPO_ROOT / "artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def tools_dir() -> Path:
    """Project-local toolchains (Kafka, Foundry) installed by scripts/bootstrap_tools.sh."""
    return _env_path("VIGIL_TOOLS", REPO_ROOT / ".tools")


def config_dir() -> Path:
    return _env_path("VIGIL_CONFIG", REPO_ROOT / "config")


def market_data_file(which: int | str | os.PathLike) -> Path:
    """Resolve a market data session: ``1``..``4`` or an explicit CSV path."""
    text = str(which)
    if text.isdigit():
        path = rtg_home() / "data" / f"market_data{text}.csv"
    else:
        path = Path(text).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"market data file not found: {path}")
    return path.resolve()


def all_market_data_files() -> list[Path]:
    return sorted((rtg_home() / "data").glob("market_data*.csv"))
