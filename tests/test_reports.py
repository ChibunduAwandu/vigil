from __future__ import annotations

import copy
import hashlib
import json

import pytest

from vigil.chain.merkle import inclusion_proof, leaf_hash, merkle_root, node_hash, root_of, verify_inclusion
from vigil.reports.builder import ComplianceReport, alerts_merkle_root, outcomes_from_score_board
from vigil.reports.canonical import EventStreamHasher, canonical_json, parse_strict
from vigil.reports.render import render_markdown
from vigil.rtg.events import read_match_events, write_match_events

# -- Merkle trees ------------------------------------------------------------------------------------

def test_merkle_matches_rfc_6962_definitions():
    assert root_of([]).hex() == hashlib.sha256(b"").hexdigest()
    # RFC 6962 test vector: the leaf hash of the empty string.
    assert leaf_hash(b"").hex() == "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"
    a, b, c = (leaf_hash(x) for x in (b"a", b"b", b"c"))
    assert root_of([a]) == a
    assert root_of([a, b]) == node_hash(a, b)
    assert root_of([a, b, c]) == node_hash(node_hash(a, b), c)  # a lone leaf is promoted, not duplicated
    assert merkle_root([b"a", b"b", b"c"]) == root_of([a, b, c])


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 31, 33])
def test_every_leaf_has_a_proof_that_verifies(size):
    leaves = [leaf_hash(str(i).encode()) for i in range(size)]
    root = root_of(leaves)
    for index in range(size):
        proof = inclusion_proof(leaves, index)
        assert verify_inclusion(leaves[index], index, size, proof, root)
        assert not verify_inclusion(leaf_hash(b"forged"), index, size, proof, root)
        assert not verify_inclusion(leaves[index], size, size, proof, root)
        if size > 1:
            assert not verify_inclusion(leaves[index], (index + 1) % size, size, proof, root)
            assert not verify_inclusion(leaves[index], index, size, proof[:-1], root)
            assert not verify_inclusion(leaves[index], index, size, [*proof, proof[-1]], root)


def test_merkle_agrees_with_the_certificate_transparency_test_vectors():
    """Known answers from outside this code base: the Merkle test vectors of the CT project."""
    inputs = [bytes.fromhex(x) for x in ("", "00", "10", "2021", "3031", "40414243", "5051525354555657",
                                         "606162636465666768696a6b6c6d6e6f")]
    roots = ["6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
             "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
             "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
             "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
             "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
             "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
             "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
             "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"]
    for size, root in enumerate(roots, 1):
        assert merkle_root(inputs[:size]).hex() == root
    # Audit paths from the same suite: (leaf, counting from 1; tree size; the path).
    paths = [(1, 1, []),
             (1, 8, ["96a296d224f285c67bee93c30f8a309157f0daa35dc5b87e410b78630a09cfc7",
                     "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
                     "6b47aaf29ee3c2af9af889bc1fb9254dabd31177f16232dd6aab035ca39bf6e4"]),
             (6, 8, ["bc1a0643b12e4d2d7c77918f44e0f4f79a838b6cf9ec5b5c283e1f4d88599e6b",
                     "ca854ea128ed050b41b35ffc1b87b8eb2bde461e9e3b5596ece6b9d5975a0ae0",
                     "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7"]),
             (3, 3, ["fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125"]),
             (2, 5, ["6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
                     "5f083f0a1a33ca076a95279832580db3e0ef4584bdff1f54c8a360f50de3031e",
                     "bc1a0643b12e4d2d7c77918f44e0f4f79a838b6cf9ec5b5c283e1f4d88599e6b"])]
    for leaf, size, path in paths:
        leaves = [leaf_hash(item) for item in inputs[:size]]
        assert [sibling.hex() for sibling in inclusion_proof(leaves, leaf - 1)] == path
        assert verify_inclusion(leaves[leaf - 1], leaf - 1, size, [bytes.fromhex(x) for x in path],
                                bytes.fromhex(roots[size - 1]))


def test_verification_refuses_what_the_contract_could_not_be_asked():
    leaves = [leaf_hash(str(i).encode()) for i in range(8)]
    root, proof = root_of(leaves), inclusion_proof(leaves, 5)
    assert verify_inclusion(leaves[5], 5, 8, proof, root)
    assert not verify_inclusion(leaves[5], -3, 8, proof, root)  # an unsigned integer cannot be negative
    assert not verify_inclusion(leaves[5], 5, 8, [*proof[:-1], proof[-1][:16]], root)
    assert not verify_inclusion(leaves[5][:16], 5, 8, proof, root)
    assert not verify_inclusion(leaves[5], 5, 8, proof, root + b"\x00")


def test_leaves_and_nodes_cannot_be_confused():
    a, b = leaf_hash(b"a"), leaf_hash(b"b")
    assert leaf_hash(a + b) != node_hash(a, b)  # a leaf whose content is two hashes is not their parent


# -- canonical form ----------------------------------------------------------------------------------

def test_canonical_json_is_independent_of_key_order():
    assert canonical_json({"b": 1, "a": [1, {"d": 2, "c": "é"}]}) == canonical_json({"a": [1, {"c": "é", "d": 2}], "b": 1})
    assert canonical_json({"a": "é"}) == '{"a":"é"}'.encode()
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_canonical_json_survives_being_read_back():
    """Written, read and written again, a document must come out as the same bytes."""
    document = {"b": [1.5, -0.0, 10 ** 30, 1e16, None, True], "a": {"é": "\u2028", "z": {"k": []}}, "c": (1, 2)}
    first = canonical_json(document)
    assert canonical_json(parse_strict(first)) == first
    # A number as a key is written as a string but sorted as a number, so it would not: 1, 2, 10
    # going out and "1", "10", "2" coming back. It is refused, wherever it hides.
    with pytest.raises(TypeError, match="string keys only"):
        canonical_json({"levels": {1: "a", 2: "b", 10: "c"}})
    with pytest.raises(TypeError, match=r"metrics\[0\].depth"):
        canonical_json({"metrics": [{"depth": {2.5: 1}}]})


def test_strict_parsing_refuses_what_has_two_readings():
    assert parse_strict(b'{"a":[1,2.5,"x",null,true],"b":{}}') == {"a": [1, 2.5, "x", None, True], "b": {}}
    for data, reason in ((b'{"a":1,"a":2}', "appears twice"), (b'{"a":{"b":1,"b":1}}', "appears twice"),
                         (b'{"a":NaN}', "not a JSON value"), (b'{"a":-Infinity}', "not a JSON value"),
                         (b'{"a":1e999}', "out of range"), (b"\xef\xbb\xbf{}", "not valid JSON"),
                         ('{"a":1}'.encode("utf-16"), "not UTF-8"), (b'{"a":1', "not valid JSON")):
        with pytest.raises(ValueError, match=reason):
            parse_strict(data)


def test_event_stream_hash_is_the_hash_of_the_exchanges_own_file(capture, tmp_path):
    path = capture / "match_events.csv"
    hasher = EventStreamHasher()
    records = list(read_match_events(path))
    for record in records:
        hasher.update(record)
    assert hasher.hexdigest() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert hasher.count == len(records)
    # And a file we write ourselves is byte-identical to the one the exchange wrote.
    write_match_events(tmp_path / "copy.csv", records)
    assert (tmp_path / "copy.csv").read_bytes() == path.read_bytes()


# -- the report --------------------------------------------------------------------------------------

def test_report_is_a_pure_function_of_the_match(audits, session_one):
    from vigil.pipeline import field_of, simulate_and_audit
    first = audits("wash", 200.0).report
    again = simulate_and_audit(session_one, field_of(session_one, "wash"), seed=7, end_time=200.0,
                               match_id="test-wash-200").report
    assert again.canonical == first.canonical
    assert first.sha256 == hashlib.sha256(first.canonical).hexdigest()
    assert len(first.key) == 64 and first.key == hashlib.sha256(first.report_id.encode()).hexdigest()


def test_report_file_round_trip(audits, tmp_path):
    report = audits("wash", 200.0).report
    path = report.save(tmp_path / "report.json")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == report.sha256  # what shasum would print
    loaded = ComplianceReport.load(path)
    assert loaded.sha256 == report.sha256
    assert [a.alert_id for a in loaded.alerts] == [a.alert_id for a in report.alerts]


def test_report_contents(audits, rulebook):
    audit = audits("wash", 200.0)
    document = audit.report.document
    assert document["schema"] == "vigil.compliance-report/1"
    assert document["summary"]["alerts"] == len(audit.alerts) > 0
    assert document["summary"]["participants_flagged"] == ["WashA", "WashB"]
    assert document["summary"]["by_control"] == {"MKT-02": len(audit.alerts)}
    assert document["summary"]["worst_severity"] == "HIGH"
    assert document["rulebook"]["sha256"] == rulebook.sha256
    assert set(document["rulebook"]["controls_run"]) == set(rulebook.controls)
    assert set(document["regulations_cited"]) == set(rulebook.control("MKT-02").regulations)
    assert document["participants"]["VigilFull"]["alerts"] == 0
    assert document["participants"]["WashA"]["exchange"]["status"] == "OK"
    assert document["match"]["market_data"] == "market_data1.csv" and len(document["match"]["events_sha256"]) == 64
    json.dumps(document)  # everything in it is plain JSON


def test_every_alert_can_be_proved_against_the_root(audits):
    report = audits("rogues", 120.0).report
    assert len(report.document["alerts"]) > 8
    root = bytes.fromhex(report.alerts_root)
    for alert in report.alerts:
        proof = report.alert_proof(alert.alert_id)
        assert leaf_hash(canonical_json(proof["alert"])).hex() == proof["leaf"]
        assert verify_inclusion(bytes.fromhex(proof["leaf"]), proof["index"], proof["size"],
                                [bytes.fromhex(x) for x in proof["proof"]], root)
    with pytest.raises(KeyError):
        report.alert_proof("no-such-alert")


def test_any_edit_changes_the_fingerprint(audits):
    report = audits("wash", 200.0).report
    for edit in (lambda d: d["alerts"].pop(),
                 lambda d: d["alerts"][0].__setitem__("severity", "LOW"),
                 lambda d: d["participants"]["WashA"]["metrics"].__setitem__("executed_lots", 1),
                 lambda d: d["match"].__setitem__("events_sha256", "0" * 64)):
        document = copy.deepcopy(report.document)
        edit(document)
        assert ComplianceReport(document).sha256 != report.sha256
    trimmed = copy.deepcopy(report.document["alerts"])[1:]
    assert alerts_merkle_root(trimmed) != report.alerts_root


def test_markdown_rendering(audits):
    report = audits("rogues", 120.0).report
    text = render_markdown(report)
    assert text.startswith(f"# Compliance report: {report.report_id}")
    for heading in ("## The match", "## Participants", "## Alerts", "## Controls run", "## Regulations cited",
                    "## Provenance"):
        assert heading in text
    assert report.sha256 in text and "Not yet anchored." in text
    assert "The record reconciles" in text
    receipt = {"report_sha256": report.sha256, "chain_id": 31337, "block_number": 7, "transaction": "0xabc",
               "contract": "0xdef", "sequence": 3}
    assert "anchored on chain 31337 in block 7" in render_markdown(report, receipt)
    # A receipt is only quoted for the report it was issued for.
    stale = render_markdown(report, {**receipt, "report_sha256": "0" * 64})
    assert "is for another fingerprint" in stale and "anchored on chain 31337" not in stale

    clean = render_markdown(audits("clean", 60.0).report)
    assert "**No alerts.**" in clean and "None." in clean


def test_outcomes_from_the_exchanges_score_board(capture):
    outcomes = outcomes_from_score_board(capture / "score_board.csv")
    assert set(outcomes) == {"VigilFull", "Example"}
    assert all(team["status"] == "OK" for team in outcomes.values())
    assert outcomes["VigilFull"]["profit_or_loss"] > outcomes["Example"]["profit_or_loss"]
