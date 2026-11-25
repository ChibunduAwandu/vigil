"""Python client for the ComplianceRegistry contract: file a report, and check one against the chain."""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import BlockNotFound, ContractCustomError, ContractLogicError
from web3.logs import DISCARD
from web3.middleware import SignAndSendRawMiddlewareBuilder
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from vigil.chain import artifact
from vigil.chain.devnet import DEV_CHAIN_ID, DEV_PRIVATE_KEY
from vigil.paths import artifacts_dir
from vigil.reports.builder import ComplianceReport, alerts_merkle_root
from vigil.surveillance.alerts import Severity

DEFAULT_RPC = "http://127.0.0.1:8545"
LOG_WINDOW = 10_000  # the most blocks one search of the logs may span (Infura's limit)


class AlreadyAnchored(Exception):
    """A report with this identifier is already in the registry; it can never be replaced.

    ``filed`` is what the registry holds under the identifier and ``same``
    whether that is the very report that was offered again, which is harmless,
    or a different one, which is not.
    """

    def __init__(self, report_id: str, filed: dict[str, Any], same: bool):
        super().__init__(report_id)
        self.report_id, self.filed, self.same = report_id, filed, same


@dataclass(frozen=True)
class AnchorReceipt:
    """Proof of filing: where on which chain a report's fingerprint was recorded."""

    report_id: str
    report_sha256: str
    chain_id: int
    contract: str
    transaction: str
    block_number: int
    block_timestamp: int
    sequence: int
    chain_head: str
    reporter: str
    gas_used: int
    gas_price_wei: int
    uri: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | os.PathLike) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path


@dataclass(frozen=True)
class Verification:
    """The outcome of checking a report file against what the registry holds."""

    report_id: str
    anchored: bool
    report_matches: bool = False  # the file's bytes hash to the fingerprint on chain
    content_matches: bool = False  # what the file says, written in the canonical layout, does
    alerts_root_matches: bool = False
    events_match: bool = False
    rulebook_matches: bool = False
    alert_count_matches: bool = False
    severity_matches: bool = False
    defects: tuple[str, ...] = ()  # ways in which the file contradicts itself
    filed: dict[str, Any] | None = None
    related: tuple[dict[str, Any], ...] = ()  # other reports on file about the same event stream

    @property
    def ok(self) -> bool:
        return (self.anchored and self.report_matches and self.alerts_root_matches and self.events_match
                and self.rulebook_matches and self.alert_count_matches and self.severity_matches
                and not self.defects)

    def problems(self) -> list[str]:
        if not self.anchored:
            return ["no report with this identifier has been anchored"]
        if self.content_matches and not self.report_matches:
            fingerprint = ("these are not the bytes that were anchored, though they say the same thing: the file "
                           "has been saved again in another layout")
        else:
            fingerprint = "the report's fingerprint differs from the one on chain"
        checks = [(self.report_matches, fingerprint),
                  (self.alerts_root_matches, "the alerts do not hash to the Merkle root on chain"),
                  (self.events_match, "the event stream fingerprint differs from the one on chain"),
                  (self.rulebook_matches, "the rulebook fingerprint differs from the one on chain"),
                  (self.alert_count_matches, "the number of alerts differs from the one on chain"),
                  (self.severity_matches, "the most serious alert differs in severity from the one on chain")]
        return [message for passed, message in checks if not passed] + list(self.defects)


def _bytes32(hex_digest: str) -> bytes:
    """A 32-byte hash from its hex form. Anything else is an error, never a run of zeros."""
    if not isinstance(hex_digest, str):
        raise ValueError(f"expected a 32-byte hash in hex, not {hex_digest!r}")
    try:
        raw = bytes.fromhex(hex_digest.removeprefix("0x"))
    except ValueError:
        raise ValueError(f"expected a 32-byte hash in hex, not {hex_digest!r}") from None
    if len(raw) != 32:
        raise ValueError(f"expected a 32-byte hash, got {len(raw)} bytes")
    return raw


def _same_hash(filed: str, stated: Any) -> bool:
    """Is ``stated``, however it is written, the hash ``filed``? A thing that is not a hash is not."""
    try:
        return _bytes32(stated) == bytes.fromhex(filed)
    except ValueError:
        return False


def _where(url: str) -> str:
    """A node URL fit to print: hosted providers put the API key in the path or the query."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")


def _why_not(w3: Web3) -> str:
    """What the node said when it would not answer, without the address it was asked at."""
    try:
        w3.provider.make_request("eth_chainId", [])
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status == 429:
            return ": it is refusing requests for now (too many, by the provider's rate limit); try again shortly"
        if status in (401, 403):
            return f": it refused the request (HTTP {status}); check the API key in the node's address"
        return f" (HTTP {status})"
    except Exception as error:  # noqa: BLE001 - only to say what went wrong
        return f" ({type(error).__name__})"
    return ""


def connect(rpc_url: str | None = None, private_key: str | None = None) -> tuple[Web3, LocalAccount | None]:
    """Open a connection and, if a key is available, arrange for transactions to be signed with it.

    The key comes from the argument or ``$VIGIL_ETH_PRIVATE_KEY``. Against a
    local anvil node, and only there, anvil's published development key is
    used when no other is given.
    """
    url = rpc_url or os.environ.get("VIGIL_ETH_RPC") or DEFAULT_RPC
    local = urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")
    # A hosted provider on a free plan answers "too many requests" to a burst of calls. Give it time:
    # up to half a minute of backing off, where a local node that is not there should fail at once.
    retry = ExceptionRetryConfiguration(errors=(ConnectionError, requests.HTTPError, requests.Timeout),
                                        retries=3 if local else 7, backoff_factor=0.125 if local else 0.5)
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}, exception_retry_configuration=retry))
    if not w3.is_connected():
        raise ConnectionError(f"no Ethereum node answering at {_where(url)}{_why_not(w3)}")
    key = private_key or os.environ.get("VIGIL_ETH_PRIVATE_KEY")
    if not key and w3.eth.chain_id == DEV_CHAIN_ID:
        key = DEV_PRIVATE_KEY
    account = None
    if key:
        account = Account.from_key(key)
        w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(account), layer=0)
        w3.eth.default_account = account.address
    return w3, account


class Registry:
    def __init__(self, w3: Web3, address: str):
        self.w3 = w3
        self.address = Web3.to_checksum_address(address)
        self.contract = w3.eth.contract(address=self.address, abi=artifact.load()["abi"])

    # -- deployment ----------------------------------------------------------------------------

    @classmethod
    def deploy(cls, w3: Web3) -> Registry:
        """Deploy a fresh registry, owned by the connection's account, and remember it for this chain."""
        compiled = artifact.load()
        factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
        receipt = w3.eth.wait_for_transaction_receipt(factory.constructor().transact(), timeout=120)
        if receipt["status"] != 1:
            raise RuntimeError("deployment transaction failed")
        registry = cls(w3, receipt["contractAddress"])
        remember_deployment(w3.eth.chain_id, registry.address)
        return registry

    @classmethod
    def at_known_address(cls, w3: Web3, address: str | None = None) -> Registry:
        """The registry for this chain: the given address, ``$VIGIL_REGISTRY``, or the last one deployed.

        The code at the address must be this contract, byte for byte. An
        address that holds something else, or a registry built from another
        version of the source, is refused rather than talked to through the
        wrong interface.
        """
        chain_id = w3.eth.chain_id
        address = address or os.environ.get("VIGIL_REGISTRY") or known_deployments().get(str(chain_id))
        if not address:
            raise LookupError(f"no registry known for chain {chain_id}; deploy one (vigil chain deploy)")
        code = bytes(w3.eth.get_code(Web3.to_checksum_address(address)))
        if len(code) == 0:
            raise LookupError(f"there is no contract at {address} on chain {chain_id}")
        if code != bytes.fromhex(artifact.load()["deployed_bytecode"].removeprefix("0x")):
            raise LookupError(f"the contract at {address} on chain {chain_id} is not the registry this version "
                              "of Vigil was built with")
        return cls(w3, address)

    # -- filing --------------------------------------------------------------------------------

    def anchor(self, report: ComplianceReport, uri: str = "") -> AnchorReceipt:
        """File the report's fingerprint.

        What goes on chain is computed from the alerts, not copied from what
        the document says about itself, and a document that contradicts itself
        is refused (``ValueError``): a filing cannot be taken back. Raises
        ``AlreadyAnchored`` if the identifier is taken.
        """
        defects = report.defects()
        if defects:
            raise ValueError(f"report {report.report_id} contradicts itself: " + "; ".join(defects))
        document = report.document
        worst = report.worst_severity
        call = self.contract.functions.anchor(
            _bytes32(report.key), _bytes32(report.sha256), _bytes32(alerts_merkle_root(document["alerts"])),
            _bytes32(document["match"]["events_sha256"]), _bytes32(document["rulebook"]["sha256"]),
            len(document["alerts"]), 0 if worst is None else int(worst) + 1, uri)
        try:
            transaction = call.transact()
        except (ContractCustomError, ContractLogicError) as error:
            filed = self.get(report)
            if filed is not None:
                raise AlreadyAnchored(report.report_id, filed, filed["report_hash"] == report.sha256) from error
            raise
        receipt = self.w3.eth.wait_for_transaction_receipt(transaction, timeout=120)
        if receipt["status"] != 1:
            raise RuntimeError(f"anchoring transaction {transaction.hex()} failed")
        # The receipt itself says what was filed; asking the node again could reach one that lags.
        logged = self.contract.events.ReportAnchored().process_receipt(receipt, errors=DISCARD)
        if len(logged) != 1:
            raise RuntimeError(f"anchoring transaction {transaction.hex()} did not log a filing")
        return self._receipt(report, receipt, logged[0]["args"])

    def receipt_for(self, report: ComplianceReport) -> AnchorReceipt | None:
        """The receipt of a filing already on chain, rebuilt from the chain's own log of it.

        For a filing whose sender never got to keep the receipt: the transaction
        went through, and the program that sent it was cut off before it could
        read the outcome back. ``None`` if this report is not what is on file
        under its identifier.
        """
        filed = self.get(report)
        if filed is None or filed["report_hash"] != report.sha256:
            return None
        # Providers cap a search of the logs at a few thousand blocks (Infura at 10,000), so find
        # the block from the time the registry recorded and search from there.
        start = self._first_block_at(filed["anchored_at"])
        logs = self.contract.events.ReportAnchored().get_logs(
            argument_filters={"reportId": _bytes32(report.key)}, from_block=start,
            to_block=min(start + LOG_WINDOW - 1, self.w3.eth.block_number))
        if not logs:
            return None
        return self._receipt(report, self.w3.eth.get_transaction_receipt(logs[0]["transactionHash"]),
                             logs[0]["args"])

    def _receipt(self, report: ComplianceReport, receipt: Any, filed: Any) -> AnchorReceipt:
        return AnchorReceipt(
            report_id=report.report_id, report_sha256=report.sha256, chain_id=self.w3.eth.chain_id,
            contract=self.address, transaction=Web3.to_hex(receipt["transactionHash"]),
            block_number=receipt["blockNumber"], block_timestamp=self._block_time(receipt["blockNumber"]),
            sequence=filed["sequence"], chain_head=bytes(filed["chainHead"]).hex(), reporter=filed["reporter"],
            gas_used=receipt["gasUsed"], gas_price_wei=receipt.get("effectiveGasPrice", 0), uri=filed["uri"])

    def _first_block_at(self, timestamp: int) -> int:
        """The first block mined at or after ``timestamp``: block times only ever increase, so bisect."""
        low, high = 0, self.w3.eth.block_number
        while low < high:
            middle = (low + high) // 2
            if self._block_time(middle) < timestamp:
                low = middle + 1
            else:
                high = middle
        return low

    def _block_time(self, number: int, attempts: int = 8) -> int:
        """When a block was mined, asked for just after it was.

        A hosted provider spreads requests over many nodes: the one that handed
        out the receipt has the block, and the next one asked may not have it
        yet. That is a matter of seconds, so ask again a few times.
        """
        for attempt in range(attempts):
            try:
                return self.w3.eth.get_block(number)["timestamp"]
            except BlockNotFound:
                if attempt == attempts - 1:
                    raise
                time.sleep(min(0.5 * 2 ** attempt, 8.0))
        raise AssertionError("unreachable")

    # -- reading -------------------------------------------------------------------------------

    def is_anchored(self, report: ComplianceReport | str) -> bool:
        key = report.key if isinstance(report, ComplianceReport) else report
        return self.contract.functions.isAnchored(_bytes32(key)).call()

    def get(self, report: ComplianceReport | str) -> dict[str, Any] | None:
        """What the registry holds for a report (or for a 32-byte key in hex), or ``None``."""
        key = report.key if isinstance(report, ComplianceReport) else report
        if not self.is_anchored(key):
            return None
        filed = self.contract.functions.get(_bytes32(key)).call()
        severity = filed[8]
        return {
            "report_hash": filed[0].hex(), "alerts_root": filed[1].hex(), "events_hash": filed[2].hex(),
            "rulebook_hash": filed[3].hex(), "chain_head": filed[4].hex(), "anchored_at": filed[5],
            "sequence": filed[6], "alert_count": filed[7],
            "worst_severity": Severity(severity - 1).name if severity else None, "severity_code": severity,
            "reporter": filed[9],
        }

    def count(self) -> int:
        return self.contract.functions.count().call()

    def head(self) -> str:
        return self.contract.functions.head().call().hex()

    def history(self) -> list[dict[str, Any]]:
        """Every anchor, in filing order."""
        entries = []
        for sequence in range(1, self.count() + 1):
            key = self.contract.functions.reportIdAt(sequence).call().hex()
            entries.append({"key": key, **self.get(key)})
        return entries

    def reports_about(self, events_sha256: str) -> list[dict[str, Any]]:
        """Every report filed about the event stream with this fingerprint, in filing order.

        A report's identifier is its filer's choice, so the same match could
        be reported twice under two names. Whoever holds the exchange's record
        of the match can find every report about it here.
        """
        keys = self.contract.functions.reportsAbout(_bytes32(events_sha256)).call()
        return [{"key": key.hex(), **self.get(key.hex())} for key in keys]

    # -- verification --------------------------------------------------------------------------

    def verify(self, report: ComplianceReport) -> Verification:
        """Compare a report file with what was filed for it.

        Everything is recomputed from the file rather than read from it: the
        fingerprint from its bytes, the Merkle root, count and severity from
        its alerts. A file whose alerts were edited fails even if everything
        it says about itself was left alone.
        """
        filed = self.get(report)
        if filed is None:
            return Verification(report.report_id, anchored=False)
        document = report.document
        worst = report.worst_severity
        others = [entry for entry in self.reports_about(filed["events_hash"]) if entry["key"] != report.key]
        return Verification(
            report_id=report.report_id, anchored=True,
            report_matches=filed["report_hash"] == report.sha256,
            content_matches=filed["report_hash"] == hashlib.sha256(report.canonical).hexdigest(),
            alerts_root_matches=filed["alerts_root"] == alerts_merkle_root(document["alerts"]),
            events_match=_same_hash(filed["events_hash"], document["match"]["events_sha256"]),
            rulebook_matches=_same_hash(filed["rulebook_hash"], document["rulebook"]["sha256"]),
            alert_count_matches=filed["alert_count"] == len(document["alerts"]),
            severity_matches=filed["worst_severity"] == (worst.name if worst is not None else None),
            defects=tuple(report.defects()), filed=filed, related=tuple(others))

    def verify_alert(self, proof: dict[str, Any]) -> bool:
        """Ask the contract whether an alert belongs to an anchored report (see ``ComplianceReport.alert_proof``)."""
        from vigil.reports.canonical import canonical_json
        key = hashlib.sha256(proof["report_id"].encode()).digest()
        leaf = self.contract.functions.leafHash(canonical_json(proof["alert"])).call()
        return self.contract.functions.verifyAlert(
            key, leaf, proof["index"], [bytes.fromhex(sibling) for sibling in proof["proof"]]).call()

    def verify_chain(self) -> bool:
        """Recompute the running hash over every field of every anchor and compare it with the registry's head."""
        head = bytes(32)
        for entry in self.history():
            head = hashlib.sha256(
                head + bytes.fromhex(entry["key"]) + bytes.fromhex(entry["report_hash"])
                + bytes.fromhex(entry["alerts_root"]) + bytes.fromhex(entry["events_hash"])
                + bytes.fromhex(entry["rulebook_hash"]) + entry["alert_count"].to_bytes(4, "big")
                + entry["severity_code"].to_bytes(1, "big") + bytes.fromhex(entry["reporter"][2:])
                + entry["anchored_at"].to_bytes(8, "big")).digest()
            if head.hex() != entry["chain_head"]:
                return False
        return head.hex() == self.head()


# -- remembering where registries live --------------------------------------------------------------

def _deployments_file() -> Path:
    return artifacts_dir() / "chain" / "deployments.json"


def _deployments() -> dict[str, list[str]]:
    """Chain id -> every registry deployed there from this machine, oldest first."""
    path = _deployments_file()
    if not path.is_file():
        return {}
    return {chain: [addresses] if isinstance(addresses, str) else list(addresses)
            for chain, addresses in json.loads(path.read_text()).items()}


def known_deployments() -> dict[str, str]:
    """Chain id -> the registry most recently deployed there."""
    return {chain: addresses[-1] for chain, addresses in _deployments().items() if addresses}


def earlier_deployments(chain_id: int) -> list[str]:
    """Registries deployed on this chain before the current one. What was anchored in them is still there."""
    return _deployments().get(str(chain_id), [])[:-1]


def remember_deployment(chain_id: int, address: str) -> None:
    path = _deployments_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    deployments = _deployments()
    known = deployments.setdefault(str(chain_id), [])
    if address in known:
        known.remove(address)
    known.append(address)
    path.write_text(json.dumps(deployments, indent=2) + "\n")
