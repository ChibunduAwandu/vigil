"""ApiVault (https://apivault.dev): the public API directory this project draws its external APIs from.

ApiVault is a catalogue, not a gateway: it lists public APIs with their
category, authentication and documentation link. Vigil uses it the way a
developer would, to find the APIs it needs, and keeps the list of those it
actually integrates here so that the choice is visible and checkable: each
one can be looked up in the live catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from vigil import net

CATALOG_URL = "https://api.apivault.dev/api"


@dataclass(frozen=True)
class Integration:
    """An API from the catalogue that Vigil uses, and what for."""

    name: str  # exactly as listed on ApiVault
    category: str
    purpose: str
    module: str
    needs_key: bool
    base_url: str


INTEGRATIONS: tuple[Integration, ...] = (
    Integration("Federal Register", "Government",
                "Regulatory scraping: new and proposed SEC and CFTC rules that bear on each control.",
                "vigil.regulatory.federal_register", False, "https://www.federalregister.gov/api/v1"),
    Integration("CoinGecko", "Cryptocurrency",
                "ETH/USD price, to state what anchoring a report costs in dollars.",
                "vigil.chain.cost", False, "https://api.coingecko.com/api/v3"),
    Integration("Etherscan", "Blockchain",
                "Independent check of an anchor on a public network, through an explorer rather than our own node.",
                "vigil.chain.etherscan", True, "https://api.etherscan.io/v2/api"),
    Integration("INFURA Ethereum", "Cryptocurrency",
                "JSON-RPC endpoint for anchoring on Sepolia or mainnet (set VIGIL_ETH_RPC).",
                "vigil.chain.registry", True, "https://sepolia.infura.io/v3/<key>"),
    Integration("Alchemy Ethereum", "Cryptocurrency",
                "Alternative JSON-RPC endpoint for public networks (set VIGIL_ETH_RPC).",
                "vigil.chain.registry", True, "https://eth-sepolia.g.alchemy.com/v2/<key>"),
)

# Looked at and left out, with the reason. Recorded so the omission is a decision, not an oversight.
CONSIDERED: dict[str, str] = {
    "SEC EDGAR Data": "Company filings, not rule-making: nothing in a Ready Trader Go match has an issuer to look up.",
    "OpenSanctions": "Counterparty screening needs real legal entities; the participants here are trading bots. "
                     "The hosted API also requires a key.",
    "FRED": "A risk-free rate is immaterial over a fifteen-minute session.",
    "Fed Treasury": "As for FRED.",
    "Alpha Vantage": "Ready Trader Go supplies its own market data; an external feed would describe a different market.",
    "Finnhub": "As for Alpha Vantage.",
    "Portfolio Optimizer": "The Kelly sizing is a closed-form calculation; a remote optimiser would add a network "
                           "dependency to the trading path for no gain.",
}


def fetch_catalog(http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Every API ApiVault lists (about 1,500 entries)."""
    return net.get_json(http or net.client(), f"{CATALOG_URL}/all")


def search(query: str, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    return net.get_json(http or net.client(), f"{CATALOG_URL}/search", {"query": query})


def category(name: str, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    return net.get_json(http or net.client(), f"{CATALOG_URL}/category/{name}", {"order": "asc"})


def check_integrations(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Look each integration up in the catalogue; ``listed`` is false for any that ApiVault does not carry."""
    by_name = {entry["name"]: entry for entry in catalog}
    rows = []
    for integration in INTEGRATIONS:
        entry = by_name.get(integration.name)
        rows.append({
            "name": integration.name,
            "listed": entry is not None,
            "category": entry["category"] if entry else integration.category,
            "auth": (entry.get("auth") or "none") if entry else ("apiKey" if integration.needs_key else "none"),
            "docs": entry["url"] if entry else None,
            "purpose": integration.purpose,
            "module": integration.module,
        })
    return rows
