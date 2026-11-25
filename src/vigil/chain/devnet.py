"""A local Ethereum node (Foundry's anvil) for development and tests."""
from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

from vigil.chain.artifact import tool

# The first account of anvil's well-known development mnemonic ("test test ... junk"). Its key is
# public knowledge: it is only ever used against a chain with anvil's own chain id.
DEV_CHAIN_ID = 31337
DEV_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Anvil:
    """Run anvil for the duration of a ``with`` block.

    With ``state`` the chain is loaded from, and on exit written back to, that
    file, so that what was anchored in one run is still there in the next.
    """

    def __init__(self, port: int | None = None, state: str | Path | None = None):
        self.port = port or free_port()
        self.state = Path(state) if state else None
        self.url = f"http://127.0.0.1:{self.port}"
        self._process: subprocess.Popen | None = None

    def _listening(self) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", self.port)) == 0

    def start(self) -> Anvil:
        binary = tool("anvil")
        if binary is None:
            raise RuntimeError("anvil not found; run scripts/bootstrap_tools.sh foundry")
        if self._listening():
            # Otherwise the wait below would be satisfied by whatever is already there, while our
            # own node failed to bind and exited.
            raise RuntimeError(f"port {self.port} is already in use; is another node running?")
        command = [binary, "--host", "127.0.0.1", "--port", str(self.port), "--silent"]
        if self.state is not None:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            command += ["--state", str(self.state)]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"anvil exited: {self._process.stderr.read().decode().strip()}")
            if self._listening():
                return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError("anvil did not start listening within 15 s")

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()  # SIGTERM: anvil writes its state file before it goes
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def __enter__(self) -> Anvil:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
