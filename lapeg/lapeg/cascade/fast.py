"""Phase 4 fast tier: iterative cascade estimator (no fork, milliseconds per run).

Per block:
  1. Oracle update. PT-reUSD's oracle is min(Pendle TWAP, linear-discount ceiling). The TWAP is modelled as
     the mean of the market's marginal price over the last `twap_blocks` blocks (900 s = 75 blocks);
     `twap_blocks=1` removes the lag (instant oracle, worst case).
  2. Liquidatable set: Morpho's rule maxBorrow < borrowed at the oracle price (integer math, morpho_math).
  3. Liquidators act on positions liquidatable for at least `latency_blocks`, largest seizable value first.
     Each liquidation seizes min(c, debt * LIF / P_o), repays seized * P_o / LIF, and sells the seized PT into
     the depth curve *after* everything already sold (cumulative S). It is skipped if the sale is beyond the
     curve (refusal) or unprofitable after gas: that position is "stuck".
  4. Between blocks, arbitrage absorbs a fraction `replenish_per_block` of the cumulative sold amount.
Stops at the horizon or at a fixed point (no liquidation, nothing pending, oracle settled).

Inputs come from measured data: the position book (Phase 1), the executed depth curve and gas (Phase 2),
and the oracle structure (`oracles.json`). This is the one-step Layer 2 model plus feedback and latency.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field

from lapeg.market import morpho_math as mm

SCALE = mm.ORACLE_PRICE_SCALE


class DepthCurve:
    """Cumulative proceeds R(S) for selling S raw PT, piecewise linear through executed sale points.

    Built from (amount_in, amount_out) of executed sales at one block; R(0) = 0. Sizes at or beyond
    `max_sellable` (first refused or reverted size, else the largest executed size) cannot be sold.
    """

    def __init__(self, amounts: list[int], proceeds: list[int], max_sellable: int | None = None) -> None:
        pts = sorted(zip(amounts, proceeds, strict=True))
        self.s = [0] + [a for a, _ in pts]
        self.r = [0] + [o for _, o in pts]
        self.max_sellable = max_sellable if max_sellable is not None else self.s[-1]

    def proceeds(self, s_total: int) -> float | None:
        if s_total > self.max_sellable or s_total > self.s[-1]:
            return None
        i = max(1, bisect.bisect_left(self.s, s_total))
        s0, s1, r0, r1 = self.s[i - 1], self.s[i], self.r[i - 1], self.r[i]
        return r0 + (r1 - r0) * (s_total - s0) / (s1 - s0) if s1 > s0 else float(r1)

    def marginal_price(self, s_total: int) -> float:
        """Raw loan units per raw PT for the next unit sold after s_total (slope of R)."""
        i = min(max(1, bisect.bisect_right(self.s, s_total)), len(self.s) - 1)
        return (self.r[i] - self.r[i - 1]) / (self.s[i] - self.s[i - 1])

    def sale(self, s_before: int, amount: int) -> float | None:
        """Proceeds of selling `amount` after `s_before` was already sold; None if beyond the curve."""
        a, b = self.proceeds(s_before), self.proceeds(s_before + amount)
        return None if a is None or b is None else b - a


@dataclass(frozen=True)
class MarketModel:
    lltv: int  # WAD
    oracle_price: int  # oracle price at t0 (1e36 scale)
    depth: DepthCurve
    linear_cap: int | None = None  # oracle ceiling (1e36 scale), e.g. the linear-discount feed; None: no cap
    gas_cost: float = 0.0  # loan raw units per liquidation (liquidate + sale)

    @property
    def lif(self) -> int:
        return mm.liquidation_incentive_factor(self.lltv)


@dataclass(frozen=True)
class Position:
    account: str
    collateral: int  # raw PT
    debt: int  # raw loan units (accrued)


@dataclass(frozen=True)
class Params:
    horizon_blocks: int = 300
    twap_blocks: int = 75  # 900 s / 12 s
    latency_blocks: int = 1
    replenish_per_block: float = 0.0
    settle_tol: float = 1e-9


@dataclass
class Liquidation:
    block: int
    account: str
    seized: int
    repaid: int
    proceeds: float
    profit: float


@dataclass
class Result:
    liquidations: list[Liquidation] = field(default_factory=list)
    stuck: dict[str, Position] = field(default_factory=dict)  # liquidatable but not clearable at the end
    bad_debt: int = 0  # realised: debt left when collateral hit 0
    bad_debt_by_account: dict[str, int] = field(default_factory=dict)
    timeline: list[dict[str, float]] = field(default_factory=list)
    blocks: int = 0
    fixed_point: bool = False
    seconds: float = 0.0

    @property
    def n_liquidations(self) -> int:
        return len(self.liquidations)

    @property
    def liquidated_repaid(self) -> int:
        return sum(x.repaid for x in self.liquidations)

    @property
    def stuck_debt(self) -> int:
        return sum(p.debt for p in self.stuck.values())

    def summary(self) -> dict[str, float | int | bool]:
        return {
            "n_liquidations": self.n_liquidations,
            "liquidated_repaid": self.liquidated_repaid,
            "n_stuck": len(self.stuck),
            "stuck_debt": self.stuck_debt,
            "bad_debt": self.bad_debt,
            "blocks": self.blocks,
            "fixed_point": self.fixed_point,
            "seconds": self.seconds,
        }


def _liquidatable(p: Position, price: int, lltv: int) -> bool:
    return p.debt > mm.max_borrow(p.collateral, price, lltv)


def simulate(
    positions: list[Position],
    model: MarketModel,
    params: Params | None = None,
    *,
    shock_pt_sold: int = 0,
    oracle_shock: float = 0.0,
) -> Result:
    """Run the cascade. `shock_pt_sold`: PT sold into the market at t0 (e.g. a pending transaction).
    `oracle_shock`: a direct, persistent proportional drop of the oracle (as the Phase 2 override)."""
    t0 = time.perf_counter()
    params = params or Params()
    res = Result()
    book = {p.account: p for p in positions if p.debt > 0}
    lif = model.lif
    s = float(shock_pt_sold)  # cumulative PT sold into the market
    spot0 = model.depth.marginal_price(0)
    k = model.oracle_price / (spot0 * SCALE)  # oracle / market at t0 (calibrates the gap)
    history = [spot0] * params.twap_blocks  # market marginal prices (raw ratio)
    first_seen: dict[str, int] = {}
    prev_oracle = None
    for blk in range(params.horizon_blocks):
        spot = model.depth.marginal_price(int(s))
        history = history[1:] + [spot]
        twap = sum(history) / len(history)
        oracle = int(twap * k * SCALE * (1 - oracle_shock))
        if model.linear_cap is not None:
            oracle = min(oracle, int(model.linear_cap * (1 - oracle_shock)))
        liq = [p for p in book.values() if _liquidatable(p, oracle, model.lltv)]
        for p in liq:
            first_seen.setdefault(p.account, blk)
        ready = sorted(
            (p for p in liq if blk - first_seen[p.account] >= params.latency_blocks),
            key=lambda p: -min(p.collateral, p.debt * lif * SCALE // (oracle * mm.WAD)),
        )
        acted = 0
        for p in ready:
            seized = min(p.collateral, p.debt * lif * SCALE // (oracle * mm.WAD))
            repaid = min(p.debt, -(-seized * oracle * mm.WAD // (lif * SCALE)))  # rounded up, as Morpho
            proceeds = model.depth.sale(int(s), seized)
            if proceeds is None or proceeds - repaid - model.gas_cost <= 0:
                continue
            s += seized
            acted += 1
            res.liquidations.append(
                Liquidation(blk, p.account, seized, repaid, proceeds, proceeds - repaid - model.gas_cost)
            )
            left = Position(p.account, p.collateral - seized, p.debt - repaid)
            if left.collateral == 0:
                res.bad_debt += left.debt
                if left.debt:
                    res.bad_debt_by_account[p.account] = left.debt
                book.pop(p.account)
            else:
                book[p.account] = left
        res.timeline.append(
            {
                "block": blk,
                "spot": spot,
                "oracle": oracle / SCALE,
                "sold_pt": s,
                "n_liquidatable": len(liq),
                "n_liquidated": acted,
            }
        )
        settled = prev_oracle is not None and abs(oracle - prev_oracle) <= params.settle_tol * oracle
        pending = any(blk - first_seen[p.account] < params.latency_blocks for p in liq)
        prev_oracle = oracle
        s *= 1 - params.replenish_per_block
        res.blocks = blk + 1
        if acted == 0 and not pending and settled:
            res.fixed_point = True
            break
    res.stuck = {
        p.account: p for p in book.values() if _liquidatable(p, prev_oracle or model.oracle_price, model.lltv)
    }
    res.seconds = time.perf_counter() - t0
    return res
