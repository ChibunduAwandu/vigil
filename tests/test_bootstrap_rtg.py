"""The external dependency installer verifies downloads and preserves existing files."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.fixture
def installer(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_rtg.py"
    spec = importlib.util.spec_from_file_location("bootstrap_rtg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    contents = {"ready_trader_go/example.py": b"# upstream source\n", "COPYING.txt": b"upstream license\n"}
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps({
        "repository": "https://github.com/example/exchange", "revision": "pinned",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }))
    monkeypatch.setattr(module, "MANIFEST", manifest)

    def download(url, timeout):
        return io.BytesIO(contents[url.split("/pinned/")[1]])

    monkeypatch.setattr(module, "urlopen", download)
    return module, contents


def test_installs_verified_files_and_preserves_existing_installation(installer, tmp_path):
    module, contents = installer
    destination = tmp_path / "exchange"
    module.install(destination)
    for name, data in contents.items():
        assert (destination / name).read_bytes() == data
    module.install(destination)  # valid repeat is harmless
    changed = destination / "ready_trader_go/example.py"
    changed.write_text("local changes")
    with pytest.raises(ValueError, match="left untouched"):
        module.install(destination)
    assert changed.read_text() == "local changes"


def test_corrupt_download_does_not_install_partial_directory(installer, tmp_path):
    module, contents = installer
    contents["COPYING.txt"] = b"unexpected content"
    destination = tmp_path / "exchange"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        module.install(destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".rtg-download-*"))


def test_manifest_cannot_write_outside_destination(installer, tmp_path):
    module, _ = installer
    manifest = json.loads(module.MANIFEST.read_text())
    manifest["files"] = {"../outside.py": "0" * 64}
    module.MANIFEST.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe path"):
        module.install(tmp_path / "exchange")
    assert not (tmp_path / "outside.py").exists()
