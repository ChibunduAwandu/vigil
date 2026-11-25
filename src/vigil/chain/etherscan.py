"""An independent look at an anchor, through a block explorer.

``Registry.verify`` trusts the node it is connected to. On a public network a
second opinion is cheap: Etherscan indexes the same chain from its own nodes,
and its logs API (listed on ApiVault under Blockchain; needs a free key)
returns the ``ReportAnchored`` event for a report. If the fingerprint in that
event matches the file, two unrelated parties agree on what was filed.

There is no explorer for a local development chain, so this is only
meaningful on networks Etherscan covers (mainnet is chain 1, Sepolia 11155111).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from web3 import Web3

from vigil import net
from vigil.reports.builder import ComplianceReport

API_URL = "https://api.etherscan.io/v2/api"
EVENT_SIGNATURE = "ReportAnchored(bytes32,uint64,address,bytes32,bytes32,bytes32,string)"
EVENT_TOPIC = Web3.to_hex(Web3.keccak(text=EVENT_SIGNATURE))


def anchor_event(report: ComplianceReport, contract: str, chain_id: int, *, api_key: str | None = None,
                 http: httpx.Client | None = None) -> dict[str, Any] | None:
    """The ``ReportAnchored`` event for ``report`` as Etherscan has it, or ``None`` if it has none."""
    api_key = api_key or os.environ.get("ETHERSCAN_API_KEY")
    if not api_key:
        raise RuntimeError("an Etherscan API key is required (set ETHERSCAN_API_KEY)")
    data = net.get_json(http or net.client(), API_URL, {
        "chainid": chain_id, "module": "logs", "action": "getLogs", "address": contract,
        "topic0": EVENT_TOPIC, "topic0_1_opr": "and", "topic1": "0x" + report.key,
        "fromBlock": 0, "toBlock": "latest", "apikey": api_key,
    })
    logs = data.get("result")
    if not isinstance(logs, list):
        raise net.ApiError(f"Etherscan: {data.get('message')}: {logs}")
    if not logs:
        return None
    entry = logs[0]
    words = bytes.fromhex(entry["data"].removeprefix("0x"))
    # Non-indexed arguments, ABI-encoded: reportHash, alertsRoot, chainHead, then the offset of the URI.
    return {
        "transaction": entry["transactionHash"],
        "block_number": int(entry["blockNumber"], 16),
        "sequence": int(entry["topics"][2], 16),
        "reporter": Web3.to_checksum_address("0x" + entry["topics"][3][-40:]),
        "report_hash": words[0:32].hex(),
        "alerts_root": words[32:64].hex(),
        "chain_head": words[64:96].hex(),
    }


def second_opinion(report: ComplianceReport, contract: str, chain_id: int, **kwargs: Any) -> dict[str, Any]:
    """Does the explorer's record of the anchor agree with the report file?"""
    event = anchor_event(report, contract, chain_id, **kwargs)
    if event is None:
        return {"found": False, "agrees": False}
    from vigil.reports.builder import alerts_merkle_root
    return {
        "found": True,
        "agrees": (event["report_hash"] == report.sha256
                   and event["alerts_root"] == alerts_merkle_root(report.document["alerts"])),
        **event,
    }
