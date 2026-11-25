"""What anchoring costs, in dollars.

Gas is priced in ether, and "0.0005 ETH" means little to a compliance
officer. The ether price comes from CoinGecko's public API (no key; listed on
ApiVault under Cryptocurrency).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from vigil import net

COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
# What filing one report uses, rounded up. The first filing in a new registry costs about 35,000
# more, and a further report on a match that already has one about 15,000 less. A test holds this
# figure to the contract.
ANCHOR_GAS = 270_000
WEI_PER_GWEI = 10 ** 9
WEI_PER_ETHER = 10 ** 18


def eth_usd(http: httpx.Client | None = None) -> float:
    """The current price of one ether in US dollars."""
    data = net.get_json(http or net.client(), COINGECKO, {"ids": "ethereum", "vs_currencies": "usd"})
    try:
        return float(data["ethereum"]["usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise net.ApiError(f"unexpected response from CoinGecko: {data!r}") from error


@dataclass(frozen=True)
class Cost:
    gas: int
    gas_price_gwei: float
    ether: float
    usd: float | None  # None if no ether price was available

    def describe(self) -> str:
        line = f"{self.gas:,} gas at {self.gas_price_gwei:g} gwei = {self.ether:.6f} ETH"
        return line + (f" = ${self.usd:,.2f}" if self.usd is not None else "")


def cost_of(gas: int, gas_price_wei: int, ether_price_usd: float | None = None) -> Cost:
    ether = gas * gas_price_wei / WEI_PER_ETHER
    return Cost(gas, gas_price_wei / WEI_PER_GWEI, ether,
                ether * ether_price_usd if ether_price_usd is not None else None)
