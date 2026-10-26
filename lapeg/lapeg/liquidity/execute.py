"""Execute sales and liquidations on the fork. By default inside a snapshot that is reverted afterwards;
`keep=True` leaves the result on the fork (used by the interactive playground, which has its own undo).

Two paths:
  - `execute_route`: send a Convert API router transaction (aggregator route, real venues) as its intended
    sender, after giving that sender the input tokens. Output = the sender's token-out balance change.
  - `sell_pt_for_sy`: the Pendle AMM leg alone (router `swapExactPtForSy`, no aggregator, no limit orders),
    to measure the internal leg of the price impact on-chain.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from web3 import Web3

from lapeg.chain.abi import load_abi
from lapeg.fork.anvil import AnvilFork
from lapeg.fork.state import BalanceSlot, balance_of, set_erc20_balance
from lapeg.liquidity.pendle_api import Route

EMPTY_LIMIT: tuple[str, int, list[Any], list[Any], bytes] = (
    "0x0000000000000000000000000000000000000000",
    0,
    [],
    [],
    b"",
)  # LimitOrderData: AMM only
GAS_LIMIT = 15_000_000
# A fresh fork fetches every touched contract's state from the upstream RPC on first use; a route across
# many venues can take minutes on a free endpoint. Waiting is cheap; misreading slowness as a revert is not.
RECEIPT_TIMEOUT_S = 900


@dataclass(frozen=True)
class Execution:
    status: str  # "ok", "reverted", or "timeout" (fork too slow; says nothing about the market)
    amount_out: int
    gas_used: int
    error: str | None = None


def revert_reason(fork: AnvilFork, tx: dict[str, Any]) -> str:
    """Re-run a failed transaction as an eth_call to recover its revert message."""
    assert fork.w3 is not None
    try:
        fork.w3.eth.call({k: v for k, v in tx.items() if k in ("from", "to", "data", "gas")})  # type: ignore[arg-type]
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:200]}"
    return "reverted (no reason on replay)"


def _failure(e: Exception) -> str:
    """'timeout' when the fork was too slow to mine (not a property of the market), else 'reverted'."""
    return "timeout" if type(e).__name__ == "TimeExhausted" else "reverted"


def _scope(fork: AnvilFork, keep: bool) -> AbstractContextManager[None]:
    return nullcontext() if keep else fork.branch()


def _fund_and_approve(
    fork: AnvilFork, token: str, owner: str, spender: str, amount: int, slot: BalanceSlot | None
) -> None:
    assert fork.w3 is not None
    fork.set_eth_balance(owner, 10**20)
    set_erc20_balance(fork, token, owner, amount, slot)
    erc = fork.w3.eth.contract(address=Web3.to_checksum_address(token), abi=load_abi("ERC20"))
    with fork.impersonated(owner):
        erc.functions.approve(Web3.to_checksum_address(spender), amount).transact({"from": owner})


def execute_route(
    fork: AnvilFork,
    route: Route,
    token_in: str,
    token_out: str,
    amount_in: int,
    slot_in: BalanceSlot | None = None,
    keep: bool = False,
) -> Execution:
    """Execute one API route on the fork. State is restored afterwards unless `keep`."""
    assert fork.w3 is not None
    sender, router = Web3.to_checksum_address(route.tx_from), Web3.to_checksum_address(route.tx_to)
    with _scope(fork, keep):
        _fund_and_approve(fork, token_in, sender, router, amount_in, slot_in)
        before = balance_of(fork, token_out, sender)
        tx = {"from": sender, "to": router, "data": route.tx_data, "gas": GAS_LIMIT}
        with fork.impersonated(sender):
            try:
                h = fork.w3.eth.send_transaction(tx)  # type: ignore[arg-type]
                rc = fork.w3.eth.wait_for_transaction_receipt(h, timeout=RECEIPT_TIMEOUT_S)
            except Exception as e:  # anvil may reject a reverting tx at submission
                return Execution(_failure(e), 0, 0, f"{type(e).__name__}: {str(e)[:200]}")
            if rc["status"] != 1:
                return Execution("reverted", 0, int(rc["gasUsed"]), revert_reason(fork, tx))
        return Execution("ok", balance_of(fork, token_out, sender) - before, int(rc["gasUsed"]))


def sell_pt_for_sy(
    fork: AnvilFork,
    router: str,
    market: str,
    pt: str,
    sy: str,
    amount_in: int,
    seller: str,
    slot_in: BalanceSlot | None = None,
    keep: bool = False,
) -> Execution:
    """Sell PT for SY in the Pendle market only (no aggregator). Restored afterwards unless `keep`."""
    assert fork.w3 is not None
    seller, router = Web3.to_checksum_address(seller), Web3.to_checksum_address(router)
    actions = fork.w3.eth.contract(address=router, abi=load_abi("PendleActionSwapPT"))
    with _scope(fork, keep):
        _fund_and_approve(fork, pt, seller, router, amount_in, slot_in)
        before = balance_of(fork, sy, seller)
        fn: Any = actions.functions.swapExactPtForSy(
            seller, Web3.to_checksum_address(market), amount_in, 0, EMPTY_LIMIT
        )
        tx = fn.build_transaction({"from": seller, "gas": GAS_LIMIT})
        with fork.impersonated(seller):
            try:
                rc = fork.w3.eth.wait_for_transaction_receipt(
                    fork.w3.eth.send_transaction(tx), timeout=RECEIPT_TIMEOUT_S
                )
            except Exception as e:
                return Execution(_failure(e), 0, 0, f"{type(e).__name__}: {str(e)[:200]}")
            if rc["status"] != 1:
                return Execution("reverted", 0, int(rc["gasUsed"]), revert_reason(fork, dict(tx)))
        return Execution("ok", balance_of(fork, sy, seller) - before, int(rc["gasUsed"]))


@dataclass(frozen=True)
class LiquidationResult:
    status: str  # "ok" or "reverted"
    seized: int  # raw collateral received
    repaid: int  # raw loan tokens paid
    gas_used: int
    error: str | None = None


def liquidate_position(
    fork: AnvilFork,
    morpho: Any,
    params: Any,
    borrower: str,
    liquidator: str,
    borrow_shares: int,
    funding: int,
    keep: bool = False,
    seized_assets: int = 0,
) -> LiquidationResult:
    """Repay `borrow_shares` of `borrower`'s debt with Morpho's own `liquidate`, from an impersonated account
    funded with `funding` loan tokens. `morpho`: lapeg.chain.morpho.MorphoBlue; `params`: its MarketParams.
    With `seized_assets` > 0, seize that much collateral instead (pass borrow_shares=0). That is the only way
    to liquidate an underwater position: repaying all its shares would seize more collateral than it has, and
    Morpho reverts with an arithmetic panic."""
    assert fork.w3 is not None
    liquidator = Web3.to_checksum_address(liquidator)
    erc = lambda t: fork.w3.eth.contract(address=Web3.to_checksum_address(t), abi=load_abi("ERC20"))  # noqa: E731
    loan, coll = erc(params.loan_token), erc(params.collateral_token)
    with _scope(fork, keep):
        fork.set_eth_balance(liquidator, 10**20)
        set_erc20_balance(fork, params.loan_token, liquidator, funding)
        coll_before = int(coll.functions.balanceOf(liquidator).call())
        fn: Any = morpho.contract.functions.liquidate(
            params.as_tuple(), borrower, seized_assets, borrow_shares, b""
        )
        tx = fn.build_transaction({"from": liquidator, "gas": 3_000_000})
        with fork.impersonated(liquidator):
            loan.functions.approve(morpho.address, funding).transact({"from": liquidator})
            try:
                rc = fork.w3.eth.wait_for_transaction_receipt(
                    fork.w3.eth.send_transaction(tx), timeout=RECEIPT_TIMEOUT_S
                )
            except Exception as e:
                return LiquidationResult("reverted", 0, 0, 0, f"{type(e).__name__}: {str(e)[:200]}")
            if rc["status"] != 1:
                return LiquidationResult("reverted", 0, 0, int(rc["gasUsed"]), revert_reason(fork, dict(tx)))
        seized = int(coll.functions.balanceOf(liquidator).call()) - coll_before
        repaid = funding - int(loan.functions.balanceOf(liquidator).call())
        return LiquidationResult("ok", seized, repaid, int(rc["gasUsed"]))
