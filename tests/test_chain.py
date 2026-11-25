from __future__ import annotations

import copy
import json
import socket

import pytest

from vigil.chain import artifact
from vigil.reports.builder import ComplianceReport, alerts_merkle_root
from vigil.surveillance.alerts import Severity

pytestmark = pytest.mark.chain


@pytest.fixture(scope="module")
def node():
    from vigil.chain.devnet import Anvil
    with Anvil() as anvil:
        yield anvil


@pytest.fixture()
def registry(node, monkeypatch, tmp_path):
    from vigil.chain.registry import Registry, connect
    monkeypatch.setenv("VIGIL_ARTIFACTS", str(tmp_path))  # keep the deployment record out of the project
    monkeypatch.delenv("VIGIL_REGISTRY", raising=False)
    w3, _ = connect(node.url)
    return Registry.deploy(w3)


def restated(document: dict, **changes) -> ComplianceReport:
    """A report edited the way a careful forger would: everything it says about itself made true again."""
    document = copy.deepcopy(document)
    document.update(changes)
    alerts = document["alerts"]
    worst = max((Severity.parse(a["severity"]) for a in alerts), default=None)
    document["alerts_merkle_root"] = alerts_merkle_root(alerts)
    document["summary"]["alerts"] = len(alerts)
    document["summary"]["worst_severity"] = worst.name if worst is not None else None
    return ComplianceReport(document)


def test_packaged_artifact_is_what_the_source_compiles_to(tmp_path):
    """The ABI and bytecode shipped in the package are those of the contract as it stands."""
    packaged = artifact.load()
    assert packaged["source_sha256"] == artifact.source_sha256(), "run: vigil chain build"
    fresh = artifact.compile_source(tmp_path / "out")
    for part in ("abi", "bytecode", "deployed_bytecode", "compiler", "settings"):
        assert packaged[part] == fresh[part], f"{part} is stale; run: vigil chain build"
    names = {entry.get("name") for entry in packaged["abi"] if entry["type"] == "function"}
    assert {"anchor", "get", "verifyReport", "verifyAlert", "reportsAbout", "head", "count"} <= names


# -- filing ------------------------------------------------------------------------------------------

def test_anchor_and_verify(registry, audits):
    report = audits("wash", 200.0).report
    assert registry.verify(report).anchored is False and registry.get(report) is None

    receipt = registry.anchor(report, uri="https://example.org/report.json")
    assert receipt.sequence == 1 and receipt.report_sha256 == report.sha256
    assert receipt.chain_id == 31337 and receipt.contract == registry.address
    filed = registry.get(report)
    assert filed["report_hash"] == report.sha256 and filed["alerts_root"] == report.alerts_root
    assert filed["events_hash"] == report.document["match"]["events_sha256"]
    assert filed["rulebook_hash"] == report.document["rulebook"]["sha256"]
    assert filed["alert_count"] == len(report.alerts) and filed["worst_severity"] == "HIGH"
    # The receipt is read off the transaction's own log, and says what the registry now holds.
    assert (receipt.sequence, receipt.chain_head, receipt.reporter) == (
        filed["sequence"], filed["chain_head"], filed["reporter"])
    assert receipt.block_timestamp == filed["anchored_at"]
    verdict = registry.verify(report)
    assert verdict.ok and verdict.problems() == [] and verdict.related == ()


def test_a_report_can_be_anchored_only_once(registry, audits):
    from vigil.chain.registry import AlreadyAnchored
    report = audits("wash", 200.0).report
    registry.anchor(report)
    with pytest.raises(AlreadyAnchored) as again:
        registry.anchor(report)
    assert again.value.same and again.value.filed["sequence"] == 1

    # Same identifier, more convenient contents, and told consistently.
    convenient = restated(report.document, alerts=[])
    assert convenient.defects() == []
    with pytest.raises(AlreadyAnchored) as swap:
        registry.anchor(convenient)
    assert not swap.value.same and swap.value.filed["report_hash"] == report.sha256
    assert registry.count() == 1 and registry.verify(report).ok
    assert not registry.verify(convenient).ok


def test_a_receipt_that_was_never_kept_can_be_rebuilt_from_the_chain(registry, audits, monkeypatch):
    from vigil.chain.registry import LOG_WINDOW
    report = audits("wash", 200.0).report
    assert registry.receipt_for(report) is None  # nothing on file yet
    # A chain long enough that the logs cannot be searched from its start in one go, as on Sepolia.
    registry.w3.provider.make_request("anvil_mine", [hex(3 * LOG_WINDOW), hex(12)])
    receipt = registry.anchor(report, uri="https://example.org/wash.json")
    registry.w3.provider.make_request("anvil_mine", [hex(50), hex(12)])
    searched = []
    real_get_logs = registry.w3.eth.get_logs

    def capped(params):
        searched.append(params)
        span = int(str(params["toBlock"]), 0) - int(str(params["fromBlock"]), 0) + 1
        assert span <= LOG_WINDOW, f"a provider would refuse a search of {span} blocks"
        return real_get_logs(params)

    monkeypatch.setattr(registry.w3.eth, "get_logs", capped)
    assert registry.receipt_for(report) == receipt and searched
    # Under the same identifier, a different report has no receipt to recover: it was never filed.
    assert registry.receipt_for(restated(report.document, alerts=report.document["alerts"][1:])) is None


def test_a_node_that_has_not_yet_seen_the_block_is_asked_again(registry, audits, monkeypatch):
    """Hosted providers answer from many nodes; the block of a receipt can be missing from the next one."""
    from web3.exceptions import BlockNotFound

    from vigil.chain import registry as module
    real, misses = registry.w3.eth.get_block, []

    def lagging(number, *args, **kwargs):
        if isinstance(number, int) and len(misses) < 2:  # the block just mined, not "latest"
            misses.append(number)
            raise BlockNotFound(f"Block with id: {number!r} not found.")
        return real(number, *args, **kwargs)

    monkeypatch.setattr(registry.w3.eth, "get_block", lagging)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    receipt = registry.anchor(audits("wash", 200.0).report)
    assert len(misses) == 2 and receipt.block_timestamp == real(receipt.block_number)["timestamp"]


def test_a_report_that_contradicts_itself_is_not_filed(registry, audits):
    """A filing cannot be withdrawn, so what is filed must be what the alerts themselves say."""
    report = audits("wash", 200.0).report
    assert report.defects() == []

    def edited(change) -> ComplianceReport:
        document = copy.deepcopy(report.document)
        change(document)
        return ComplianceReport(document)

    cases = {
        "root": edited(lambda d: d["alerts"].pop()),  # the stated root is now that of a larger tree
        "count": edited(lambda d: d["summary"].__setitem__("alerts", 0)),
        "severity": edited(lambda d: d["summary"].__setitem__("worst_severity", None)),
        "events": edited(lambda d: d["match"].__setitem__("events_sha256", None)),
        "empty": edited(lambda d: d["match"].__setitem__("events_sha256", "")),
        "case": edited(lambda d: d["match"].__setitem__("events_sha256", d["match"]["events_sha256"].upper())),
        "rulebook": edited(lambda d: d["rulebook"].__setitem__("sha256", "0x" + d["rulebook"]["sha256"])),
        "twins": edited(lambda d: d["alerts"].append(d["alerts"][0])),
    }
    for name, doctored in cases.items():
        assert doctored.defects(), name
        with pytest.raises(ValueError, match="contradicts itself"):
            registry.anchor(doctored)
    assert registry.count() == 0
    # Nothing was spent: the genuine report still files under its identifier.
    assert registry.anchor(report).sequence == 1


# -- verification ------------------------------------------------------------------------------------

def test_tampering_is_detected(registry, audits):
    report = audits("wash", 200.0).report
    registry.anchor(report)

    softened = copy.deepcopy(report.document)
    softened["alerts"][0]["severity"] = "LOW"
    verdict = registry.verify(ComplianceReport(softened))
    assert not verdict.ok and not verdict.report_matches and not verdict.alerts_root_matches
    assert verdict.alert_count_matches

    dropped = copy.deepcopy(report.document)
    dropped["alerts"].pop()
    verdict = registry.verify(ComplianceReport(dropped))
    assert not verdict.alerts_root_matches and not verdict.alert_count_matches
    # Editing the alerts but leaving the stated Merkle root alone does not help: it is recomputed.
    assert dropped["alerts_merkle_root"] == report.alerts_root

    # Nor does making the forgery consistent with itself: the chain holds the original's figures.
    cleared = registry.verify(restated(report.document, alerts=[]))
    assert cleared.defects == () and not cleared.ok
    assert not (cleared.report_matches or cleared.alerts_root_matches or cleared.alert_count_matches
                or cleared.severity_matches)

    retold = copy.deepcopy(report.document)
    retold["participants"]["WashA"]["metrics"]["executed_lots"] = 0
    verdict = registry.verify(ComplianceReport(retold))
    assert not verdict.ok and verdict.alerts_root_matches and verdict.problems() == [
        "the report's fingerprint differs from the one on chain"]


def test_only_the_bytes_that_were_anchored_verify(registry, audits, tmp_path):
    """A fingerprint is of a file. A copy that parses to the same thing is still another file."""
    report = audits("wash", 200.0).report
    path = report.save(tmp_path / "report.json")
    registry.anchor(ComplianceReport.load(path))
    assert registry.verify(ComplianceReport.load(path)).ok

    document = json.loads(path.read_bytes())
    variants = {
        "indented": json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False).encode(),
        "reordered": json.dumps(dict(reversed(document.items())), separators=(",", ":"), ensure_ascii=False).encode(),
        "escaped": json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        "newline": path.read_bytes() + b"\n",
    }
    for name, data in variants.items():
        if data == path.read_bytes():
            continue  # a report with no non-ASCII text has no escaped variant
        copy_path = tmp_path / f"{name}.json"
        copy_path.write_bytes(data)
        loaded = ComplianceReport.load(copy_path)
        assert loaded.document == document and not loaded.in_canonical_form, name
        # What is shown as the file's fingerprint is what shasum would print for that file.
        assert loaded.sha256 != report.sha256, name
        verdict = registry.verify(loaded)
        assert not verdict.ok and not verdict.report_matches and verdict.content_matches, name
        assert "saved again in another layout" in verdict.problems()[0], name


def test_a_file_with_more_than_one_reading_is_not_a_report(audits, tmp_path):
    report = audits("wash", 200.0).report
    genuine = report.canonical.decode()
    assert genuine.startswith('{"alerts":[')

    # The same key twice: a parser that keeps the first value sees a clean report, one that keeps the
    # last sees the genuine one. Neither is what the file "says".
    twice = tmp_path / "twice.json"
    twice.write_text('{"alerts":[],' + genuine[1:])
    with pytest.raises(ValueError, match="'alerts' appears twice"):
        ComplianceReport.load(twice)

    for name, data in {
        "nan": genuine.replace('"alerts":[', '"x":NaN,"alerts":[', 1).encode(),
        "huge": genuine.replace('"alerts":[', '"x":1e999,"alerts":[', 1).encode(),
        "bom": b"\xef\xbb\xbf" + genuine.encode(),
        "utf16": genuine.encode("utf-16"),
        "array": b"[1,2,3]",
        "hollow": b'{"report_id":"x"}',
        "truncated": genuine[:-40].encode(),
    }.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(data)
        with pytest.raises(ValueError, match="cannot be read as a report"):
            ComplianceReport.load(path)


def test_alert_proofs_are_checked_by_the_contract(registry, audits):
    report = audits("rogues", 120.0).report
    registry.anchor(report)
    for alert in report.alerts[::5]:
        assert registry.verify_alert(report.alert_proof(alert.alert_id))
    proof = report.alert_proof(report.alerts[0].alert_id)
    forged = copy.deepcopy(proof)
    forged["alert"]["competitor"] = "SomeoneElse"
    assert not registry.verify_alert(forged)
    shifted = {**proof, "index": proof["index"] + 1}
    assert not registry.verify_alert(shifted)


def test_history_is_a_hash_chain(registry, audits):
    reports = [audits(name, seconds).report for name, seconds in (("wash", 200.0), ("clean", 60.0), ("rogues", 120.0))]
    receipts = [registry.anchor(report) for report in reports]
    assert [r.sequence for r in receipts] == [1, 2, 3]
    history = registry.history()
    assert [entry["report_hash"] for entry in history] == [report.sha256 for report in reports]
    assert history[1]["worst_severity"] is None and history[1]["alert_count"] == 0
    assert registry.head() == history[-1]["chain_head"] == receipts[-1].chain_head
    assert registry.verify_chain()


def test_every_report_about_a_match_can_be_found(registry, audits):
    """An identifier is the filer's to choose, so a second report on the same match can be filed
    under another name. It cannot be hidden: filings are indexed by the event stream they are about."""
    report = audits("wash", 200.0).report
    events = report.document["match"]["events_sha256"]
    assert registry.reports_about(events) == []
    registry.anchor(report)
    assert registry.verify(report).related == ()

    quiet = restated(report.document, report_id=report.report_id + " ", alerts=[])
    registry.anchor(quiet)  # accepted: the identifier is new
    unrelated = audits("clean", 60.0).report
    registry.anchor(unrelated)

    about = registry.reports_about(events)
    assert [entry["report_hash"] for entry in about] == [report.sha256, quiet.sha256]
    assert [entry["sequence"] for entry in about] == [1, 2]
    verdict = registry.verify(report)
    assert verdict.ok and [entry["report_hash"] for entry in verdict.related] == [quiet.sha256]
    assert [entry["report_hash"] for entry in registry.verify(quiet).related] == [report.sha256]
    assert registry.verify(unrelated).related == ()


# -- who may file, and where -------------------------------------------------------------------------

def test_only_reporters_may_anchor(node, registry, audits):
    from eth_account import Account
    from web3 import Web3
    from web3.exceptions import ContractCustomError
    from web3.middleware import SignAndSendRawMiddlewareBuilder

    from vigil.chain.registry import Registry
    stranger = Account.create()
    w3 = Web3(Web3.HTTPProvider(node.url))
    w3.provider.make_request("anvil_setBalance", [stranger.address, hex(10 ** 18)])
    w3.middleware_onion.inject(SignAndSendRawMiddlewareBuilder.build(stranger), layer=0)
    w3.eth.default_account = stranger.address
    with pytest.raises(ContractCustomError):
        Registry(w3, registry.address).anchor(audits("clean", 60.0).report)
    assert registry.count() == 0


def test_a_registry_is_recognised_by_its_code(node, registry):
    from vigil.chain.registry import Registry
    w3 = registry.w3
    assert Registry.at_known_address(w3).address == registry.address
    assert Registry.at_known_address(w3, registry.address).address == registry.address

    nowhere = "0x00000000000000000000000000000000000000Aa"
    with pytest.raises(LookupError, match="no contract"):
        Registry.at_known_address(w3, nowhere)

    # Something else with code: the registry's own, one byte short.
    impostor = "0x00000000000000000000000000000000000000bb"
    code = artifact.load()["deployed_bytecode"]
    w3.provider.make_request("anvil_setCode", [impostor, code[:-2]])
    with pytest.raises(LookupError, match="not the registry"):
        Registry.at_known_address(w3, impostor)


def test_deployments_are_remembered_in_order(registry):
    from vigil.chain.registry import Registry, earlier_deployments, known_deployments
    w3 = registry.w3
    assert known_deployments() == {"31337": registry.address} and earlier_deployments(31337) == []
    second = Registry.deploy(w3)
    assert second.address != registry.address
    assert known_deployments() == {"31337": second.address}
    assert earlier_deployments(31337) == [registry.address]
    assert Registry.at_known_address(w3).address == second.address


def test_the_chain_survives_a_restart(audits, tmp_path, monkeypatch):
    from vigil.chain.devnet import Anvil
    from vigil.chain.registry import Registry, connect
    monkeypatch.setenv("VIGIL_ARTIFACTS", str(tmp_path))
    monkeypatch.delenv("VIGIL_REGISTRY", raising=False)
    report = audits("wash", 200.0).report
    state = tmp_path / "chain.json"
    with Anvil(state=state) as first:
        registry = Registry.deploy(connect(first.url)[0])
        registry.anchor(report)
        address = registry.address
    with Anvil(state=state) as second:
        w3, _ = connect(second.url)
        assert Registry.at_known_address(w3).address == address  # remembered from the deployment
        assert Registry(w3, address).verify(report).ok


def test_a_node_is_not_started_on_a_port_that_is_taken(node):
    from vigil.chain.devnet import Anvil
    with pytest.raises(RuntimeError, match="already in use"):
        Anvil(port=node.port).start()
    with socket.socket() as sock:  # the node that was there is untouched
        assert sock.connect_ex(("127.0.0.1", node.port)) == 0


def test_a_node_address_is_printed_without_its_key():
    from vigil.chain.registry import _where, connect
    assert _where("https://mainnet.infura.io/v3/0123456789abcdef") == "https://mainnet.infura.io"
    assert _where("https://eth-sepolia.g.alchemy.com/v2/secret?x=1") == "https://eth-sepolia.g.alchemy.com"
    assert _where("http://127.0.0.1:8545") == "http://127.0.0.1:8545"
    with pytest.raises(ConnectionError) as refused:
        connect("http://127.0.0.1:9/v3/secret-key")
    assert "secret-key" not in str(refused.value)


# -- what it costs -----------------------------------------------------------------------------------

def test_anchoring_gas_is_what_the_cost_estimate_assumes(registry, audits):
    from vigil.chain.cost import ANCHOR_GAS
    first = registry.anchor(audits("wash", 200.0).report, uri="https://example.org/reports/wash.json")
    later = registry.anchor(audits("rogues", 120.0).report, uri="https://example.org/reports/rogues.json")
    assert 0.95 * ANCHOR_GAS < later.gas_used <= ANCHOR_GAS
    # The first filing in a registry also pays to initialise its head and its list.
    assert later.gas_used < first.gas_used <= ANCHOR_GAS + 40_000


# -- a second opinion --------------------------------------------------------------------------------

def test_explorer_second_opinion_decodes_a_real_log(registry, audits):
    """Build Etherscan's answer from the event the contract really emitted, and check we read it right."""
    import httpx

    from vigil import net
    from vigil.chain import etherscan
    report = audits("wash", 200.0).report
    receipt = registry.anchor(report)
    log = registry.w3.eth.get_transaction_receipt(receipt.transaction)["logs"][0]
    assert log["topics"][0].hex().removeprefix("0x") == etherscan.EVENT_TOPIC.removeprefix("0x")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": [{
            "address": log["address"], "topics": ["0x" + t.hex().removeprefix("0x") for t in log["topics"]],
            "data": "0x" + log["data"].hex().removeprefix("0x"), "blockNumber": hex(log["blockNumber"]),
            "transactionHash": receipt.transaction}]})

    http = net.client(transport=httpx.MockTransport(handler))
    opinion = etherscan.second_opinion(report, registry.address, 11155111, api_key="k", http=http)
    assert opinion["found"] and opinion["agrees"]
    assert opinion["report_hash"] == report.sha256 and opinion["sequence"] == 1
    assert opinion["reporter"] == receipt.reporter and opinion["chain_head"] == receipt.chain_head
    assert seen["topic1"] == "0x" + report.key and seen["chainid"] == "11155111"

    softened = copy.deepcopy(report.document)
    softened["alerts"][0]["severity"] = "LOW"
    assert etherscan.second_opinion(ComplianceReport(softened), registry.address, 11155111, api_key="k",
                                    http=http)["agrees"] is False

    empty = net.client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "0", "message": "No records found", "result": []})))
    assert etherscan.second_opinion(report, registry.address, 1, api_key="k", http=empty) == {
        "found": False, "agrees": False}
