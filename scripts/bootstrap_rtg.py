#!/usr/bin/env python3
"""Install the separately licensed Ready Trader Go dependency from a pinned mirror.

Only files in config/rtg-source.json are downloaded. Every file is verified before
the completed directory is installed. Existing installations are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "rtg-source.json"


def verify(directory: Path, files: dict[str, str]) -> list[str]:
    invalid = []
    for name, expected in files.items():
        path = directory / name
        if not path.is_file():
            invalid.append(name)
        elif name != "exchange.json" and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            # A local exchange configuration may have been customized; source and data must match.
            invalid.append(name)
    return invalid


def install(destination: Path) -> None:
    manifest = json.loads(MANIFEST.read_text())
    files = manifest["files"]
    for name in files:
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError(f"unsafe path in manifest: {name}")
    if destination.exists():
        invalid = verify(destination, files)
        if invalid:
            raise ValueError(f"Existing installation differs or is incomplete: {', '.join(invalid)}. "
                             "It was left untouched; use --destination with an empty path.")
        print(f"Ready Trader Go source and data verified: {destination}")
        return

    repository = manifest["repository"].removeprefix("https://github.com/")
    base = f"https://raw.githubusercontent.com/{repository}/{manifest['revision']}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Ready Trader Go from {manifest['repository']} ({manifest['revision'][:12]})")
    print("Upstream dependency license: AGPL-3.0-or-later. See its COPYING.txt and source notices.")
    with tempfile.TemporaryDirectory(prefix=".rtg-download-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "exchange"
        staging.mkdir()

        def fetch(item: tuple[str, str]) -> None:
            name, expected = item
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(f"{base}/{name}", timeout=120) as response, path.open("wb") as output:
                shutil.copyfileobj(response, output)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"SHA-256 mismatch for {name}; installation aborted")

        with ThreadPoolExecutor(max_workers=6) as executor:
            list(executor.map(fetch, files.items()))
        # Do not replace a destination another process created while downloads were running.
        if destination.exists():
            raise FileExistsError(f"Destination appeared during download: {destination}")
        staging.rename(destination)
    print(f"Verified and installed {len(files)} files in {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "pyready_trader_go-2")
    args = parser.parse_args()
    try:
        install(args.destination.expanduser().resolve())
    except (OSError, ValueError) as error:
        parser.exit(1, f"Ready Trader Go setup failed: {error}\n")


if __name__ == "__main__":
    main()
