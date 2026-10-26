"""Cross-check a route on a real node: eth_call at the pinned block with state overrides.

When a route fails on the anvil fork, the same transaction is simulated on the upstream archive node at the
same block, with the seller's token balance and router allowance injected as state overrides. Both are EVM
executions against the same chain state, so:
  - real node also reverts  -> the quoted route is genuinely not executable (the quote overstated depth);
  - real node succeeds      -> the fork failure is an artifact (anvil), and the real-node output is used;
  - transport error         -> unknown (retried; never counted as a revert).
The router's return value is decoded with the verified ABI (swapExactPtForToken returns netTokenOut first).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from eth_abi import decode
from web3 import Web3
from web3.exceptions import ContractLogicError

from lapeg.fork.state import mapping_slot, nested_mapping_slot


@dataclass(frozen=True)
class TokenSlots:
    token: str
    balance_index: int
    balance_layout: str
    allowance_index: int
    allowance_layout: str


@dataclass(frozen=True)
class CallResult:
    status: str  # "ok", "reverted" (EVM revert), or "error" (transport failure: result unknown)
    amount_out: int
    error: str | None = None


def _word(x: int) -> str:
    return "0x" + x.to_bytes(32, "big").hex()


def call_route(
    w3: Web3,
    block: int,
    slots: TokenSlots,
    seller: str,
    router: str,
    data: str,
    amount_in: int,
    retries: int = 4,
) -> CallResult:
    """Simulate a router transaction from `seller` holding `amount_in` of the input token."""
    seller, router = Web3.to_checksum_address(seller), Web3.to_checksum_address(router)
    bal = mapping_slot(seller, slots.balance_index, slots.balance_layout)  # type: ignore[arg-type]
    allow = nested_mapping_slot(seller, router, slots.allowance_index, slots.allowance_layout)  # type: ignore[arg-type]
    override: Any = {
        Web3.to_checksum_address(slots.token): {
            "stateDiff": {_word(bal): _word(amount_in), _word(allow): _word(amount_in)}
        }
    }
    last: Exception | None = None
    for attempt in range(retries):
        try:
            out = w3.eth.call(
                {"from": seller, "to": router, "data": data, "gas": 15_000_000},  # type: ignore[typeddict-item]
                block,
                override,
            )
            return CallResult("ok", int(decode(["uint256"], bytes(out)[:32])[0]))
        except ContractLogicError as e:  # the EVM reverted: a real result
            return CallResult("reverted", 0, f"{type(e).__name__}: {str(e)[:200]}")
        except Exception as e:  # transport problem (dropped connection): says nothing about the route
            last = e
            time.sleep(2**attempt)
    return CallResult("error", 0, f"{type(last).__name__}: {str(last)[:200]}")
