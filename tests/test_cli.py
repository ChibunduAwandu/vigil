from __future__ import annotations

import hashlib
import json

import pytest

from vigil.cli import build_parser, main


def test_parser_knows_every_command():
    parser = build_parser()
    for argv in (["apis"], ["train"], ["backtest"], ["simulate"], ["audit", "x.csv"], ["live", "--team", "A=example"],
                 ["produce", "x.csv"], ["monitor", "--once"], ["regulatory", "show"], ["chain", "history"],
                 ["report", "r.json"], ["calibrate"], ["replay", "match_events.csv"]):
        assert callable(parser.parse_args(argv).run)
    with pytest.raises(SystemExit):
        parser.parse_args(["chain", "explode"])


def test_audit_command_writes_a_verifiable_report(capture, tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["audit", str(capture / "match_events.csv"), "--score-board", str(capture / "score_board.csv"),
                 "--exchange-json", str(capture / "exchange.json"), "--match-id", "cli-test", "--out", str(out),
                 "--no-regulatory"])
    assert code == 0
    printed = capsys.readouterr().out
    report = json.loads((out / "report.json").read_bytes())
    assert report["report_id"] == "cli-test" and report["summary"]["integrity"]["discrepancies"] == 0
    digest = hashlib.sha256((out / "report.json").read_bytes()).hexdigest()
    assert f"sha256 {digest}" in printed and "record reconciles" in printed
    assert (out / "report.md").read_text().startswith("# Compliance report: cli-test")
    assert report["participants"]["VigilFull"]["exchange"]["status"] == "OK"
    assert report["regulatory_watch"] is None

    assert main(["report", str(out / "report.json")]) == 0
    assert "## Provenance" in capsys.readouterr().out


def test_simulate_command(tmp_path, capsys):
    out = tmp_path / "sim"
    code = main(["simulate", "--session", "1", "--scenario", "wash", "--seconds", "150", "--match-id", "cli-sim",
                 "--out", str(out), "--no-regulatory"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "WashA" in printed and "MKT-02" in printed
    assert {p.name for p in out.iterdir()} == {"report.json", "report.md", "match_events.csv", "score_board.csv"}
    # The event file written next to the report is the one the report was built from.
    report = json.loads((out / "report.json").read_bytes())
    assert hashlib.sha256((out / "match_events.csv").read_bytes()).hexdigest() == report["match"]["events_sha256"]


def test_a_simulation_that_publishes_keeps_its_report_out_of_the_monitors_way(tmp_path, capsys, monkeypatch):
    """A monitor on the same machine writes its report on the match to artifacts/reports/<id>."""
    monkeypatch.setenv("VIGIL_ARTIFACTS", str(tmp_path))
    simulate = ["simulate", "--scenario", "wash", "--seconds", "60", "--no-regulatory"]
    assert main([*simulate, "--match-id", "cli-quiet"]) == 0
    assert (tmp_path / "reports" / "cli-quiet" / "report.json").is_file()
    assert main([*simulate, "--match-id", "cli-published", "--publish", "--bus", "memory://"]) == 0
    assert "published" in capsys.readouterr().out
    assert (tmp_path / "runs" / "cli-published" / "report.json").is_file()
    assert not (tmp_path / "reports" / "cli-published").exists()


def test_a_node_that_turns_requests_away_is_named_without_its_key(capsys):
    """A hosted provider puts the API key in the path of its address; it must never be printed."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Busy(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(429)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v3/a-secret-project-key"
        assert main(["chain", "history", "--rpc", url]) == 2
    finally:
        server.shutdown()
    said = capsys.readouterr().err
    assert "refusing requests for now" in said and "rate limit" in said
    assert "a-secret-project-key" not in said


def test_a_match_is_replayed_with_the_rules_it_was_played_under(tmp_path, monkeypatch, capsys):
    import subprocess

    from vigil import live
    from vigil.paths import rtg_home
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    monkeypatch.setattr(live, "display_available", lambda: True)
    run = tmp_path / "run"
    run.mkdir()
    (run / "match_events.csv").write_text("")
    assert main(["replay", str(run / "match_events.csv")]) == 0
    (run / "exchange.json").write_text("{}")  # a live run directory leaves the exchange's configuration
    assert main(["replay", str(run / "match_events.csv")]) == 0
    assert [kwargs["cwd"] for _, kwargs in calls] == [rtg_home(), run]
    assert calls[0][0][-1] == str(run / "match_events.csv")
    assert "replaying match_events.csv" in capsys.readouterr().out

    monkeypatch.setattr(live, "display_available", lambda: False)
    with pytest.raises(SystemExit, match="PySide6"):
        main(["replay", str(run / "match_events.csv")])


def test_addresses_are_cut_back_to_their_host_before_they_are_shown():
    from vigil.net import redact
    assert (redact("429 Client Error: Too Many Requests for url: https://sepolia.infura.io/v3/0123abcd")
            == "429 Client Error: Too Many Requests for url: https://sepolia.infura.io/...")
    assert redact("no node at http://127.0.0.1:8545") == "no node at http://127.0.0.1:8545"
    assert "secret" not in redact("'https://eth-sepolia.g.alchemy.com/v2/secret?x=1'")


@pytest.mark.chain
def test_chain_commands_end_to_end(tmp_path, capsys, monkeypatch):
    from vigil.chain.devnet import Anvil
    monkeypatch.setenv("VIGIL_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.delenv("VIGIL_REGISTRY", raising=False)
    simulate = ["simulate", "--scenario", "wash", "--seconds", "150", "--match-id", "cli-chain", "--no-regulatory"]
    with Anvil() as node:
        monkeypatch.setenv("VIGIL_ETH_RPC", node.url)
        out = tmp_path / "sim"
        assert main([*simulate, "--out", str(out), "--anchor"]) == 0
        printed = capsys.readouterr().out
        assert "deployed a registry" in printed and "anchored: chain 31337" in printed
        receipt = json.loads((out / "anchor.json").read_text())
        assert receipt["sequence"] == 1 and "anchored on chain 31337" in (out / "report.md").read_text()
        # Nothing about this machine goes on chain unless it is asked for.
        assert receipt["uri"] == ""

        assert main(["chain", "verify", str(out / "report.json")]) == 0
        printed = capsys.readouterr().out
        digest = hashlib.sha256((out / "report.json").read_bytes()).hexdigest()
        assert "VERIFIED" in printed and f"file sha256   {digest}" in printed
        assert f"registry      {receipt['contract']} on chain 31337" in printed and "note:" not in printed

        document = json.loads((out / "report.json").read_bytes())

        # The same report, indented: not the file that was anchored, and shown with its own fingerprint.
        pretty = tmp_path / "pretty.json"
        pretty.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False))
        assert main(["chain", "verify", str(pretty)]) == 1
        printed = capsys.readouterr().out
        assert "VERIFIED" not in printed and "saved again in another layout" in printed
        assert f"file sha256   {hashlib.sha256(pretty.read_bytes()).hexdigest()}" in printed

        document["alerts"][0]["severity"] = "LOW"
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        assert main(["chain", "verify", str(forged)]) == 1
        printed = capsys.readouterr().out
        assert "FAILED: the report's fingerprint differs" in printed and "VERIFIED" not in printed

        # A file that reads two ways is a failed verification, not a crash.
        ambiguous = tmp_path / "ambiguous.json"
        ambiguous.write_bytes(b'{"alerts":[],' + (out / "report.json").read_bytes()[1:])
        assert main(["chain", "verify", str(ambiguous)]) == 1
        printed = capsys.readouterr().out
        assert "FAILED" in printed and "'alerts' appears twice" in printed and "VERIFIED" not in printed

        alert = document["alerts"][1]["alert_id"]
        assert main(["chain", "proof", str(out / "report.json"), alert]) == 0
        assert "accepts this proof" in capsys.readouterr().out
        assert main(["chain", "history"]) == 0
        assert "chain verifies" in capsys.readouterr().out

        # The same match again: the identical report is already on file, which is not a failure.
        assert main([*simulate, "--out", str(out), "--anchor"]) == 0
        assert "already anchored (entry 1)" in capsys.readouterr().out
        # A different report under the same identifier is refused, and the command says so.
        other = tmp_path / "other"
        assert main([*simulate, "--out", str(other), "--anchor", "--seed", "3"]) == 1
        assert "NOT anchored: a different report is on file" in capsys.readouterr().out
        assert not (other / "anchor.json").exists()
        # ...but under a name of its own the second report does file, and both can be found from
        # the record of the match.
        assert main([*simulate[:-3], "--match-id", "cli-chain-again", "--no-regulatory", "--out", str(other),
                     "--anchor", "--uri", "https://example.org/again.json"]) == 0
        assert json.loads((other / "anchor.json").read_text())["uri"] == "https://example.org/again.json"
        capsys.readouterr()
        assert main(["chain", "about", str(out / "match_events.csv")]) == 0
        assert "2 reports filed about it" in capsys.readouterr().out
        assert main(["chain", "verify", str(out / "report.json")]) == 0
        printed = capsys.readouterr().out
        assert "VERIFIED" in printed and "note: 1 other report on the same event stream is on file: entry 2" in printed
        assert "explorer" not in printed  # there is none for a development chain

        # On a network Etherscan covers, and with a key, its view of the filing is reported, and a
        # contradiction fails the verification.
        from vigil import cli
        from vigil.chain import etherscan
        monkeypatch.setitem(cli.EXPLORER_CHAINS, 31337, "the test chain")
        monkeypatch.setenv("ETHERSCAN_API_KEY", "a-secret-key")
        for opinion, code, said in [
            ({"found": True, "agrees": True, "block_number": 1234, "transaction": "0xab"}, 0, "Etherscan agrees"),
            ({"found": False, "agrees": False}, 0, "has no record of this filing yet"),
            ({"found": True, "agrees": False, "block_number": 1234, "transaction": "0xab"}, 1,
             "FAILED: Etherscan's record of the filing"),
        ]:
            monkeypatch.setattr(etherscan, "second_opinion", lambda *a, opinion=opinion, **k: opinion)
            assert main(["chain", "verify", str(out / "report.json")]) == code
            assert said in capsys.readouterr().out

        def unreachable(*args, **kwargs):
            raise RuntimeError("no route to https://api.etherscan.io/v2/api?apikey=a-secret-key")
        monkeypatch.setattr(etherscan, "second_opinion", unreachable)
        assert main(["chain", "verify", str(out / "report.json")]) == 0
        printed = capsys.readouterr().out
        assert "could not be asked" in printed and "a-secret-key" not in printed
