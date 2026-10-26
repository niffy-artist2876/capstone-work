# Illiquidity metrics

Defined in `illiquidity.py`; computed by the Phase 2 experiment (`lapeg/liquidity/experiment.py`).
Sizes are in collateral units (PT): a price shock changes a position's dollar value, not the number of
tokens that must be sold.

## Inputs, all measured on the fork

| Input | Symbol | How it is measured |
|---|---|---|
| Executable depth curve | $P_{exec}(q)$ | Sell $q$ PT for USDC on the fork through the best route returned by Pendle's Convert API (aggregator route across real venues), inside a snapshot that is reverted. The calldata is saved, so the sale can be re-executed at the same block on an archive node |
| Oracle price | $P_o$ | Morpho's oracle `price()` at the same block |
| Oracle gap | $G(q) = 1 - P_{exec}(q)/P_o$ | Spec definition; identical to the measurement track's $S_o(q)$ |
| Liquidation incentive | $LIF$ | Morpho's formula, from the market's LLTV |
| Gas | $g$ | `gasUsed` of the executed sale times the block's base fee, priced with Chainlink ETH/USD (resolved via ENS). A stress scenario adds a priority fee |
| Liquidatable set and collateral | $Q(\Delta)$ | The Morpho oracle is forced to $P_o(1-\Delta)$ (oracle override); Morpho's own health computation (HealthReader) decides who is liquidatable. Seizable collateral per borrower: $\min(c_i,\ b_i \cdot LIF / P_o(1-\Delta))$ |

## Metrics

**1. Profitability threshold.** A liquidator who seizes value $V$ (at the oracle) repays $V/LIF$ and sells for
$V(1-G)$. Profit after gas is positive iff

$$G(q) < \Big(1 - \frac{1}{LIF}\Big) - \frac{g}{V}$$

The first term is the bonus cushion ($0.3(1-LLTV)$ for these markets); the second is gas per unit of value.

**2. Liquidation capacity $q_{max}$.** The largest size whose sale executes (no revert, no API refusal) and keeps
$G$ below the threshold, reported as a bracket (last passing size, first failing size). It is refined by zoom stages:
each stage quotes sizes across the previous bracket and forks the block right after, because a live quote is only
valid against the state it was computed from.
Reported with and without gas, and under a high-fee stress scenario.

**3. Liquidator capacity per block.** Liquidators in one block sell into the same depth, which does not
replenish within the block. Capital is not the binding constraint: Morpho's `liquidate` sends the collateral
before pulling repayment (callback), so the sale can fund the repayment, and Morpho's fee-free flash loans add
its whole loan-token balance. So:

$$C_{block} = q_{max}(\text{bonus, gas}),\qquad \text{reported bound: flash liquidity} \times LIF$$

How fast depth refills *between* blocks (arbitrage) is a Phase 3 question; Phase 2 does not assume it.

**4. Backlog ratio** at shock $\Delta$:

$$B(\Delta) = \frac{Q_{seizable}(\Delta)}{q_{max}}$$

$B \le 1$: one block of liquidations can clear everything that became liquidatable. $B > 1$: a backlog
forms, and the position-by-position order of liquidations starts to matter (this is what the guard acts on).

**5. Break point $\Delta^*$.** The smallest shock with $B(\Delta) > 1$, found exactly at the borrowers' trigger
shocks $\Delta_i = 1 - 1/HF_i$ (the Phase 1 position book) and checked with the oracle override.

**Per-market score.** The vector $(\Delta^*,\ q_{max}/\text{borrow},\ B(\Delta) \text{ at } \Delta = 2\%, 5\%, 10\%)$.
It is not collapsed into one number on purpose: $\Delta^*$ says *when* the market breaks, $q_{max}$/borrow says
*how much* it can absorb, and $B$ says *how badly* it breaks beyond $\Delta^*$.

## Why these definitions

- **Against the oracle, not the market mid:** the liquidator's bonus is set in oracle terms.
- **Executed, not quoted:** the spec requires real execution on a fork; the API quote is kept only as a
  live cross-check.
- **Refusals count as failures:** a sale that reverts cannot clear any collateral, whatever its price.
- **Seizable rather than total collateral:** Morpho never lets a liquidator take more than $LIF$ times the debt
  repaid, so total collateral overstates what must be sold.

## Limits

- Depth is measured at one block; it is not assumed to refill between blocks.
- The route is the aggregator's at quote time; a liquidator could split across blocks or use other venues.
- Assumes the market price falls together with the oracle under a shock. True for PT-reUSD, whose oracle
  follows a 15-minute Pendle TWAP (with lag); not for linear-discount oracles (see `docs/assumptions.md`).
