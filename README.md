# Liquidation Capacity of PT-Collateral Markets on Morpho Blue

**Capstone: illiquidity in collateral-based lending (DeFi)**
Status as of 2026-10-19: Layers 1 and 2 complete.

| Layer | Question | Script | Data snapshot (Ethereum mainnet, UTC) |
|---|---|---|---|
| **1** | How much collateral can one liquidation sell profitably? ($q^*$) | [`pendle_ladder_all.py`](pendle_ladder_all.py) | Ladders: 2026-10-18 07:21 and 2026-10-19 14:00 |
| **2** | How big a price shock forces more than that onto the market? ($\Delta^*$) | [`layer2_borrowers.py`](layer2_borrowers.py) | Positions: 2026-10-19 14:56 |

**Contents**
1. [Overview](#1-overview)
2. [Markets studied](#2-markets-studied)
3. [Metrics](#3-metrics)
4. [Layer 1: liquidation capacity (q*)](#4-layer-1-liquidation-capacity-q)
5. [Layer 2: borrowers under a price shock](#5-layer-2-borrowers-under-a-price-shock)
6. [Daily snapshots](#6-daily-snapshots)
7. [Roadmap](#7-roadmap)

---

## 1. Overview

Lending markets assume that a bad loan can always be liquidated: a liquidator repays the debt, takes the collateral at a discount, and sells it. That assumption fails if the collateral cannot be sold in size without moving its price too much.

This project tests that assumption on three Morpho Blue markets that accept **Pendle Principal Tokens (PTs)** as collateral. A PT is redeemable for 1 unit of its underlying asset at maturity, so before maturity it trades at a discount. Its liquidity is mostly one Pendle AMM pool plus whatever DEX liquidity exists for the underlying asset.

- **Layer 1** measures the **supply side**: how large a single sale can be before the liquidation stops paying ($q^*$).
- **Layer 2** measures the **demand side**: which borrowers become liquidatable as prices fall, how much collateral that puts up for sale, and the shock $\Delta^*$ at which that amount exceeds what the market can absorb.

**Results at a glance**

| Market | Layer 1: $q^*$ (Oct 19) | Layer 1 bottleneck | Layer 2: $\Delta^*$ | Layer 2: what breaks |
|---|---|---|---|---|
| PT-reUSD-10DEC2026 | $12.8M to $13.0M | Aggregator leg (cliff) | **2.04%** | Largest borrower plus 9 others exceed $q^*$ |
| PT-USD3-17DEC2026 | $3.95M to $4.00M | Pendle AMM | **3.58%** | 10 borrowers exceed $q^*$; largest is 2.2x $q^*$ alone |
| PT-sUSDS-26NOV2026 | No slippage crossing | Pendle refuses above about $17.5M | **15.56%** | Collateral exceeds Pendle's refusal limit |

## 2. Markets studied

All three borrow USDC on Ethereum. Parameters are from `results/morpho_pt_markets_clean.csv` (2026-10-18) and were re-checked against the live Morpho API on 2026-10-19.

| Market | Maturity | Days left (Oct 18) | LLTV | LIF | Threshold | Borrowed (Oct 18) | Oracle price (Oct 18) | Oracle type |
|---|---|---|---|---|---|---|---|---|
| PT-reUSD-10DEC2026 | 2026-12-10 | 53 | 0.915 | 1.0262 | 2.55% | $116.95M | 0.983591 | ChainlinkOracleV2 |
| PT-USD3-17DEC2026 | 2026-12-17 | 60 | 0.86 | 1.0438 | 4.20% | $23.42M | 0.950918 | ChainlinkOracleV2 |
| PT-sUSDS-26NOV2026 | 2026-11-26 | 39 | 0.915 | 1.0262 | 2.55% | $16.15M | 0.986213 | Unknown (per API) |

Market IDs, collateral token and oracle addresses are in the CSV. None of the three markets has a pre-liquidation contract, so standard Morpho liquidation is the only path.

## 3. Metrics

This section explains every metric used in the report: what it measures, why it matters, and how to read it. Unless stated otherwise, "PT" amounts are token counts and "$" amounts are valued at the oracle price.

### 3.1 Background terms

| Term | Meaning | Role in this project |
|---|---|---|
| Oracle price $P_o$ | The price Morpho uses to value collateral (USDC per PT) | Decides who is liquidatable and how much collateral a liquidator receives |
| LTV | Debt divided by collateral value at $P_o$ | A borrower's leverage |
| LLTV | The LTV at which a position becomes liquidatable (0.915 or 0.86 here) | The liquidation line |
| Health factor $HF$ | $LLTV / LTV$ | Above 1 is safe; at or below 1 the position can be liquidated |
| LIF | Liquidation incentive factor: collateral a liquidator receives per $1 of debt repaid | The liquidator's margin before selling |

How a liquidation works: once a borrower's $HF$ falls below 1, anyone can repay some of their debt and receive $LIF$ times that amount in collateral, valued at the oracle price. The liquidator then usually sells that collateral for the debt asset (USDC) to lock in the bonus. **Every metric below is about whether that last step, the sale, still works.**

### 3.2 $S_o(q)$: slippage against the oracle (both layers)

$$S_o(q) = 1 - \frac{P_{exec}(q)}{P_o}$$

$P_{exec}(q)$ is the average price actually received when selling PT worth $q$ (at oracle value) into USDC.

- **What it measures:** how far below the oracle price the liquidator actually sells. It combines two things: price impact from the sale's size, and any standing gap between the oracle and the market.
- **Why against the oracle, not the market price:** the liquidator's bonus is fixed in oracle terms. What decides profit is how the sale price compares with the oracle, whatever the market's mid price is.
- **How to read it:**
  - $S_o > 0$: the sale gets less than the oracle price, so part of the bonus is lost.
  - $S_o < 0$: the market pays *more* than the oracle, so the liquidator earns extra. This happens when the oracle is set conservatively below the market. Example: a $100k PT-USD3 sale executes at 0.9827 against an oracle of 0.9509, so $S_o = -3.34\%$.
- **Depends on size:** $S_o$ grows as $q$ grows. The list of $S_o$ values at increasing sizes is the "ladder" of Layer 1.

### 3.3 Profit threshold $0.3(1-LLTV)$ (both layers)

Morpho Blue sets the bonus from the LLTV:

$$LIF = \min\left(1.15,\ \frac{1}{0.3\,LLTV + 0.7}\right)$$

A liquidator repaying $r$ USDC receives $r \cdot LIF / P_o$ PT and sells it for $r \cdot LIF \cdot P_{exec} / P_o$ USDC. That beats the $r$ they paid iff

$$LIF \cdot \frac{P_{exec}}{P_o} > 1 \iff S_o(q) < 1 - \frac{1}{LIF} = 0.3\,(1 - LLTV)$$

The last equality holds whenever the 1.15 cap does not bind (LLTV above about 0.565, true for all three markets).

| LLTV | LIF (bonus) | Threshold: maximum tolerable $S_o$ | Markets |
|---|---|---|---|
| 0.915 | 1.0262 (+2.62%) | 2.55% | reUSD, sUSDS |
| 0.86 | 1.0438 (+4.38%) | 4.20% | USD3 |

- **Intuition:** the bonus is the liquidator's cushion. Slippage up to the threshold eats into it. Slippage beyond the threshold turns the liquidation into a loss, so a rational liquidator stops.
- **Lower LLTV, bigger cushion:** riskier collateral gets a lower LLTV and therefore a larger bonus, so USD3 tolerates 4.2% slippage while the other two tolerate 2.55%.
- **Worked example (reUSD):** repaying $1M earns $1.0262M of PT at oracle value.
  - Selling at $S_o = 0.57\%$ (a $13.8M sale) returns about **$1.020M**: a profit of about $20k.
  - Selling at $S_o = 2.92\%$ (a $14.0M sale) returns about **$0.996M**: a loss of about $4k.
- **Gross of costs:** gas and the cost of borrowing the USDC to repay are ignored. Both are small at these sizes.

### 3.4 $q^*$: liquidation capacity (Layer 1)

$$q^* = \text{the sale size where } S_o(q) \text{ reaches } 0.3(1-LLTV)$$

- **What it measures:** the largest amount of collateral that can be liquidated **in one sale** and still make money. It is the market's absorption capacity for liquidations.
- **Units:** dollars at oracle value, or PT tokens ($q^*/P_o$). Layer 2 uses PT, because a price shock changes the dollar value of the same tokens without changing how many must be sold.
- **Reported as a range:** the search brackets the crossing to within 2%, so $q^*$ is given as "last profitable size to first unprofitable size".
- **As a % of borrow:** this gives scale. reUSD's $q^*$ of about 12% of borrow means one sale can only clear collateral worth about an eighth of the market's debt.
- **Refusal variant:** sometimes Pendle refuses to quote before slippage reaches the threshold (sUSDS). Then the refusal size is the capacity: no single sale above it can happen at all.

**Supporting Layer 1 metrics**

| Metric | Meaning | How to read it |
|---|---|---|
| Internal impact | Price impact inside the Pendle AMM (PT to the underlying's yield token) | High: the Pendle pool is the bottleneck (USD3) |
| External impact | Price impact in the aggregator swap from the underlying to USDC | High: DEX liquidity for the underlying asset is the bottleneck (reUSD) |
| Refusal (`MarketProportionTooHigh`) | Pendle's AMM caps the PT share of its pool, so the API will not quote a sale that pushes past it | A hard size limit, independent of price |

### 3.5 Layer 2 metrics

| Metric | Formula | What it measures |
|---|---|---|
| Shock $\Delta$ | Oracle (and market) price falls to $P_o(1-\Delta)$ | The size of the price drop being tested |
| Trigger shock $\Delta_i$ | $1 - \dfrac{LTV_i}{LLTV} = 1 - \dfrac{1}{HF_i}$ | How far the price must fall before borrower $i$ can be liquidated: their **distance to liquidation** |
| Collateral for sale $Q(\Delta)$ | $\sum_{\Delta_i \le \Delta} c_i$ (total) or $\sum_{\Delta_i \le \Delta} \min\left(c_i, \frac{b_i \cdot LIF}{P_o(1-\Delta)}\right)$ (seizable) | How much PT liquidators would need to sell after a shock of $\Delta$ |
| Break point $\Delta^*$ | Smallest $\Delta_i$ with $S_o(Q(\Delta_i)) \ge 0.3(1-LLTV)$ or a refusal | The smallest shock after which the backlog cannot be cleared in one profitable sale |

- **$\Delta_i$ example:** PT-USD3's largest borrower has $HF = 1.0387$, so $\Delta_i = 1 - 1/1.0387 = 3.73\%$. A 3.73% fall in the oracle price makes them liquidatable.
- **Total vs seizable $Q$:** a liquidator can't take more collateral than the debt they repay justifies. Just past the trigger, the seizable share of a position is $LLTV \cdot LIF$: **93.9%** of the collateral at LLTV 0.915 and **89.8%** at LLTV 0.86. Total $Q$ is the conservative upper bound; seizable $Q$ is more realistic.
- **Reading $\Delta^*$:**
  - Below $\Delta^*$, every liquidation that becomes possible can also be completed profitably, so the market clears itself.
  - Above $\Delta^*$, liquidators can't sell everything at a profit. Underwater positions then wait, and the longer they wait, the higher the risk of bad debt.
  - A **small** $\Delta^*$ means a fragile market.

**Concentration metrics**

| Metric | How to read it |
|---|---|
| Largest position / $q^*$ | Above 1x: the largest borrower can't be liquidated in one profitable sale even alone |
| Top-5 share | Share of collateral held by the five largest borrowers. High means a few liquidations decide the outcome |
| Collateral / refusal limit | Above 1x: liquidating all borrowers would need a sale Pendle refuses outright |

### 3.6 How big is a shock of $\Delta$ for a PT?

Under the shock model, the oracle and the market price both fall by $\Delta$. For a PT, a price drop can come from two sources:

- **The underlying asset depegs** by $\Delta$, or
- **The PT's implied yield jumps.** With price $P$ and $d$ days to maturity (simple yield), a drop of $\Delta$ needs a yield increase of
$$\Delta y = \frac{1}{P} \cdot \frac{\Delta}{1-\Delta} \cdot \frac{365}{d}$$

| Market | $\Delta^*$ | Days to maturity (Oct 19) | Same as an underlying depeg of | Same as an implied-yield jump of |
|---|---|---|---|---|
| PT-reUSD-10DEC2026 | 2.04% | 52 | 2.04% | +14.9 percentage points |
| PT-USD3-17DEC2026 | 3.58% | 59 | 3.58% | +24.1 percentage points |
| PT-sUSDS-26NOV2026 | 15.56% | 38 | 15.56% | +180 percentage points |

As maturity approaches, a given yield jump moves the PT price less. So for short-dated PTs, large price drops are more likely to come from the underlying depegging than from rates.

### 3.7 How the metrics connect

1. A shock $\Delta$ hits the oracle price.
2. Every borrower with $\Delta_i \le \Delta$ becomes liquidatable.
3. Their collateral, $Q(\Delta)$, has to be sold.
4. Layer 1's ladder gives the slippage for selling that much: $S_o(Q(\Delta))$.
5. Compare it with the threshold $0.3(1-LLTV)$. The first shock where it fails is $\Delta^*$.

---

## 4. Layer 1: liquidation capacity (q*)

> **Question:** how large can one liquidation sale be before $S_o(q)$ reaches the threshold?
> **Inputs:** market parameters CSV, Pendle Convert API quotes. **Output:** $q^*$ per market.

### 4.1 Method

$q^*$ is the sale size where $S_o(q)$ crosses $0.3(1-LLTV)$. Beyond $q^*$, a single liquidation sale loses money.

Script: [`pendle_ladder_all.py`](pendle_ladder_all.py).

1. Load each market from the CSV (or read `decimals()` and oracle `price()` on-chain with `--live`).
2. Request a PT to USDC quote from Pendle's Convert API (`/core/v3/sdk/1/convert`, aggregators enabled), keeping the best route returned.
3. **Bracket:** start at $100k and double until a quote is unprofitable or refused, capped at 1.5x outstanding borrow.
4. **Bisect** between the last good and first bad size until the interval is within 2%.
5. Record every quote, including Pendle's split of price impact into the **internal** leg (Pendle AMM) and **external** leg (aggregator swap of the underlying into USDC).

Quotes only: no transactions, no wallet, no keys. Calls are paced by the API's rate-limit headers (free tier, 200 CU/min).

### 4.2 Results (2026-10-18 07:21 UTC)

| Market | $q^*$ range | % of borrow | $S_o$ at $100k | Bottleneck |
|---|---|---|---|---|
| PT-reUSD-10DEC2026 | $13.80M to $14.00M | 11.8% to 12.0% | +0.03% | Aggregator leg (sudden cliff) |
| PT-USD3-17DEC2026 | $4.10M to $4.15M | 17.5% to 17.7% | -3.34% | Pendle AMM |
| PT-sUSDS-26NOV2026 | No slippage crossing | n/a | -0.83% | Pendle refuses sales above about $17.7M |

![S_o curves](results/so_curves_2026-10-18T072153Z.png)

*$S_o(q)$ in % on a symlog axis. Dashed lines: thresholds of 2.55% and 4.20%.*

#### PT-reUSD-10DEC2026: a cliff in the aggregator leg

| $q$ | $6.4M | $12.8M | $13.8M | $14.0M | $16.0M | $19.2M | $25.6M |
|---|---|---|---|---|---|---|---|
| $S_o$ | 0.33% | 0.54% | 0.57% | **2.92%** | 11.4% | 27.5% | 46.0% |
| USDC out | $6.38M | $12.73M | $13.72M | $13.59M | $14.18M | $13.92M | $13.84M |

- Slippage stays below 0.6% up to $13.8M, then jumps past the threshold within $200k.
- At $14.0M, 2.55 of the 2.95 points of price impact are in the external (aggregator) leg, so the binding limit is USDC liquidity for the underlying asset, not the Pendle pool.
- **USDC output tops out around $13.9M to $14.2M whatever the size.** Selling $14.0M returns less than selling $13.8M. The route behaves like a hard cap on USDC depth, not a gradual slope.

#### PT-USD3-17DEC2026: a gradual Pendle AMM limit

| $q$ | $1.6M | $3.2M | $4.0M | $4.10M | $4.15M | $4.8M | $6.4M |
|---|---|---|---|---|---|---|---|
| $S_o$ | -2.39% | +1.94% | 3.99% | 4.19% | **4.28%** | 5.35% | 7.50% |

- From $1.6M upward, all of the price impact is internal (the Pendle AMM). The aggregator leg contributes zero.
- The oracle sits **3.2% below** the small-trade execution price (a $100k sale executes 3.34% above the oracle). That gap works as a buffer: without it, $q^*$ would be smaller.

#### PT-sUSDS-26NOV2026: a refusal limit, not a slippage limit

| $q$ | $12.8M | $15.66M | $17.08M | $17.62M | $17.80M | $18.51M | $24.2M |
|---|---|---|---|---|---|---|---|
| $S_o$ | -0.81% | -0.80% | -0.75% | -0.69% | refused | refused | refused |

- Slippage never comes close to the 2.55% threshold. The worst quote is still 0.69% *above* the oracle.
- Pendle refuses sales between **$17.62M and $17.80M** (17.87M to 18.05M PT) with `MarketProportionTooHigh`. The sale would push the pool's PT share past the AMM's maximum.
- A rerun at 07:25 UTC reproduced every quote to within about $5.
- The oracle sits about 0.8% below market.

### 4.3 Second snapshot (2026-10-19 14:00 UTC, `--live`)

Liquidity was slightly thinner one day later. Layer 2 uses this snapshot, since it is closest in time to the positions.

| Market | $q^*$ on Oct 18 | $q^*$ on Oct 19 | $q^*$ on Oct 19 (PT) |
|---|---|---|---|
| PT-reUSD-10DEC2026 | $13.80M to $14.00M | $12.80M to $13.00M | 13.01M to 13.21M |
| PT-USD3-17DEC2026 | $4.10M to $4.15M | $3.95M to $4.00M | 4.15M to 4.20M |
| PT-sUSDS-26NOV2026 (refusal) | $17.62M to $17.80M | $17.44M to $17.62M | 17.68M to 17.86M |

### 4.4 Verification (2026-10-19)

Inputs and results were re-checked with read-only calls to the Morpho API and to Ethereum.

**Confirmed**
- LLTV, collateral token, oracle address and collateral decimals for all three markets match the live Morpho API and on-chain `decimals()`.
- Oracle scale exponents (36 for 6-decimal PTs, 24 for the 18-decimal PT) and days to maturity are correct.
- LIF values, thresholds, $q^*$ brackets, % of borrow and bottleneck attributions match the raw ladder data.

**Corrections and known issues**

| # | Issue | Effect | Fix |
|---|---|---|---|
| 1 | `implied_apy_simple_pct` computes the discount rate $(1-P)\frac{365}{d}$, not a yield | Understates yields: reUSD 11.3% should be **11.5%**, USD3 29.9% should be **31.4%**, sUSDS 12.9% should be **13.1%** | Use $\left(\frac{1}{P}-1\right)\frac{365}{d}$ |
| 2 | The sUSDS summary row shows `status=ok` and "$q^*$ = 109% of borrow" | Reads like a slippage crossing but is a refusal boundary | **Fixed 2026-10-20:** status is now `refusal boundary`, `slippage crossing` or `refused even at the starting size` |
| 3 | Quotes use `slippage=0.5`, which Pendle reads as 50% | No effect on quoted amounts (taken from route outputs) | Set to 0.01 |
| 4 | Borrow figures in the CSV are rounded and their source is not logged | Small effect on % of borrow | Pull from the API with a timestamp |

**Live values on 2026-10-19 (for reference)**

| Market | Borrowed | Collateral (oracle value) | Oracle price |
|---|---|---|---|
| PT-reUSD-10DEC2026 | $118.40M | $149.82M | 0.984010 |
| PT-USD3-17DEC2026 | $21.53M | $28.88M | 0.951982 |
| PT-sUSDS-26NOV2026 | $16.16M | $18.83M (18.93M PT) | 0.986672 |

### 4.5 Limitations

- **One sale, one route.** Each quote is a single atomic sale through the best route returned. A real liquidator could split sales across blocks, use other venues, or hold the PT to maturity.
- **Gross of costs.** $S_o$ ignores gas and the cost of capital. Both are small next to these sizes.
- **Snapshots, not a time series.** Liquidity and routes change: $q^*$ fell by 1% to 7% in one day, and the best route switched between OKX and KyberSwap from quote to quote.
- **Oracle price drifts up over time.** PT oracle prices rise toward 1 as maturity approaches (reUSD went from 0.98359 to 0.98401 in about a day). Later runs should use `--live`.

### 4.6 Files and reproduce

```
pendle_ladder_all.py                               quote ladder and q* search
results/
  morpho_pt_markets_clean.csv                      market inputs (2026-10-18)
  ladder_all_2026-10-18T072153Z.csv                every quote, all markets
  qstar_summary_2026-10-18T072153Z.csv             q* per market
  so_curves_2026-10-18T072153Z.png                 S_o curves
  ladder_all_2026-10-18T072539Z.csv                sUSDS rerun (reproducibility)
  qstar_summary_2026-10-18T072539Z.csv
  so_curves_2026-10-18T072539Z.png
  ladder_all_2026-10-19T140033Z.csv                second snapshot (--live), used by Layer 2
  qstar_summary_2026-10-19T140033Z.csv
```

```bash
pip install requests pandas matplotlib
python pendle_ladder_all.py --data results/morpho_pt_markets_clean.csv --outdir results --plot
python pendle_ladder_all.py --data results/morpho_pt_markets_clean.csv --outdir results --live --plot   # fresh oracle prices
python pendle_ladder_all.py --data results/morpho_pt_markets_clean.csv --only PT-USD3-17DEC2026         # one market
python pendle_ladder_all.py --mock                                                                     # offline test
```

Optional environment variables: `PENDLE_API_KEY`, `ETH_RPC_URL`.

---

## 5. Layer 2: borrowers under a price shock

> **Question:** as prices fall, which borrowers become liquidatable, and at what shock $\Delta^*$ does the collateral to be sold exceed what Layer 1 says the market can absorb?
> **Inputs:** Morpho GraphQL positions, live oracle price, Layer 1 ladder. **Output:** $\Delta^*$ per market.

### 5.1 Method

Script: [`layer2_borrowers.py`](layer2_borrowers.py). It makes read-only Morpho GraphQL and `eth_call` requests, plus live Pendle quotes only with `--requote`.

1. **Positions.** Pull every borrower's collateral $c_i$ (PT) and debt $b_i$ (USDC) from Morpho's `marketPositions`.
   - Field names were confirmed by schema introspection: `Market` exposes `marketId` (not `uniqueKey`), positions are filtered with `marketUniqueKey_in` and `borrowShares_gte: "1"`, and amounts in `MarketPosition.state` are raw integers.
2. **LTV at the live oracle price:** $LTV_i = \dfrac{b_i}{c_i P_o}$.
3. **Shock model** (metrics explained in section 3.5).
   - The oracle and the market price both fall to $P_o(1-\Delta)$, so $S_o$ measured in PT units is unchanged.
   - Borrower $i$ becomes liquidatable at $\Delta_i = 1 - LTV_i/LLTV = 1 - 1/HF_i$.
   - The collateral to be sold is $Q(\Delta)$, in two versions:
     - **Total:** $Q_{tot}(\Delta) = \sum_{\Delta_i \le \Delta} c_i$.
     - **Seizable:** $Q_{seiz}(\Delta) = \sum_{\Delta_i \le \Delta} \min\left(c_i, \frac{b_i \cdot LIF}{P_o(1-\Delta)}\right)$, the most a liquidator can take.
4. **Link to Layer 1.**
   - $S_o$ at $Q(\Delta)$ is interpolated from the newest ladder (by default) over `pt_sold`.
   - Any size at or above the first refused quote counts as a failure.
   - Sizes beyond the ladder are re-quoted live with `--requote`. This was not needed: every $\Delta^*$ fell inside the ladder.
5. **Break point:** $\Delta^* = \min\{\Delta_i : S_o(Q(\Delta_i)) \ge 0.3(1-LLTV) \text{ or refused}\}$.

### 5.2 Results (positions 2026-10-19 14:56 UTC, ladder 2026-10-19 14:00 UTC)

| Market | Borrowers | $\Delta^*$ | Borrowers liquidatable at $\Delta^*$ | $Q$ at $\Delta^*$ (total / seizable) | What breaks |
|---|---|---|---|---|---|
| PT-reUSD-10DEC2026 | 243 | **2.04%** | 10 | 23.27M / 21.88M PT | Slippage: $S_o$ = 38% / 34% |
| PT-USD3-17DEC2026 | 223 | **3.58%** | 10 | 5.52M / 5.02M PT | Slippage: $S_o$ = 6.0% / 5.3% |
| PT-sUSDS-26NOV2026 | 14 | **15.56%** | 13 | 18.93M / 18.80M PT | Pendle refuses the sale |

| Market | $q^*$ (PT) | Largest position (PT) | Largest / $q^*$ | Largest position's trigger | Top-5 share of collateral |
|---|---|---|---|---|---|
| PT-reUSD-10DEC2026 | 13.01M | 13.07M | 1.00x | 2.04% | 32.0% |
| PT-USD3-17DEC2026 | 4.15M | 9.23M | **2.22x** | 3.73% | 49.7% |
| PT-sUSDS-26NOV2026 | 17.68M (refusal) | 6.35M | 0.36x | 7.19% | **93.6%** |

### 5.3 Findings

- **reUSD: one borrower decides the outcome.** Up to a 2.04% shock, 9 small positions (10.19M PT in total) can be liquidated at under 0.5% slippage. At 2.04% the largest borrower (13.07M PT) joins them. That alone is about the size of $q^*$, and the combined 23.27M PT hits the aggregator cliff ($S_o \approx 38\%$).
- **USD3: liquidation fails before the largest borrower is even reached.** At a 3.58% shock, 10 borrowers holding 5.52M PT already exceed $q^*$. The largest position (9.23M PT, trigger 3.73%) is 2.2x $q^*$, so it could not be liquidated in one profitable sale even alone.
- **sUSDS: slippage never binds, the pool size does.** Up to a 15.35% shock every liquidation is profitable, with $S_o \approx -0.8\%$. At 15.56% the 13th borrower pushes $Q$ to 18.93M PT, beyond Pendle's 17.86M PT refusal limit. Borrower collateral is **1.06x** the most Pendle will accept in one sale. Five borrowers hold 93.6% of it.

### 5.4 Verification

- **Positions are complete.** Summed debt matches each market's total exactly. Summed collateral matches to within 0.08% for USD3 and sUSDS. reUSD differs by 3.8%: that is 5.81M PT held in 13 positions with no debt, which can never be liquidated (146.57M + 5.81M = 152.38M PT, the market total).
- **LTVs are right.** $LLTV / LTV_i$ matches the API's health factor to within $3 \times 10^{-5}$. No borrower is liquidatable today.
- **Paging is stable.** Fetching with page sizes of 7 and 500 returns the same 223 PT-USD3 borrowers with no duplicates.
- **Total vs seizable doesn't matter.** Both give the same $\Delta^*$ in every market.
- **The result holds across snapshots.** Using the Oct 18 ladder instead gives the same $\Delta^*$ for all three markets.

### 5.5 Limitations

- **Shock definition.** reUSD and USD3 oracle prices rise steadily toward 1, which looks like a fixed-rate (linear discount) feed. If so, a market crash does **not** move the oracle, and the scenario above needs a separate oracle shock, such as a feed update or depeg. With a fixed oracle, liquidations come from interest pushing up LTVs, and a falling market price raises $S_o$ instead.
- **Stress case.** All liquidatable borrowers are assumed to be liquidated in full, in one sale, with no top-ups or repayments first.
- **Inherits Layer 1's limitations** (section 4.5): one route, gross of costs, snapshot liquidity.
- **Timing gap.** Positions (14:56 UTC) and the ladder (14:00 UTC) are about an hour apart.

### 5.6 Files and reproduce

```
layer2_borrowers.py                                positions, shock model, Delta*
results/
  positions_2026-10-19T145646Z.csv                 every borrower: collateral, debt, LTV, Delta_i
  shock_curve_2026-10-19T145646Z.csv               Q(Delta) and S_o(Q) per market
  delta_star_2026-10-19T145646Z.csv                Delta*, largest position, top-5 share, refusal check
```

```bash
python layer2_borrowers.py                                     # all markets, newest ladder per market
python layer2_borrowers.py --only PT-USD3-17DEC2026 --requote  # one market, live quotes beyond the ladder
python layer2_borrowers.py --ladder results/ladder_all_2026-10-18T072153Z.csv   # pin a specific ladder
```

Every output file name and row carries its UTC snapshot timestamp.

---

## 6. Daily snapshots

Since 2026-10-20, a GitHub Actions workflow ([`.github/workflows/daily-snapshot.yml`](.github/workflows/daily-snapshot.yml)) runs both layers every day at 14:17 UTC and commits the results to `results/daily/`. The goal is a time series of $q^*$ and $\Delta^*$ as each PT approaches maturity (Nov 26, Dec 10 and Dec 17). This data cannot be collected after the fact, because quotes are live.

- **Layer 1** runs with `--live` (fresh oracle prices). **Layer 2** runs with `--requote`, so any $Q$ beyond the ladder gets a live quote instead of a guess.
- **Wallets are shortened** to `0x6b28...be5b` in daily files (`--short-addresses`). That is still unique enough to follow each borrower across days.
- **Failures are contained.** If one market fails, the others are still recorded. If Layer 1 fails, Layer 2 still records positions against the last available ladder.
- **Manual run:** GitHub → Actions → "daily snapshot" → "Run workflow". **To stop:** disable the workflow on the same page.

---

## 7. Roadmap

- [x] **Layer 1:** $S_o(q)$ ladders and $q^*$ per market, plus fact-check
- [x] **Layer 2:** per-borrower LTV, $Q(\Delta)$ under shocks, $\Delta^*$, sUSDS collateral vs the refusal limit, largest position vs $q^*$
- [ ] Block diagram: data sources, ingestion, measurement of $S_o$ and $q^*$, shock model, mitigation
- [ ] Mitigation design based on the measured limits
- [x] Daily snapshots until maturity (automated, from 2026-10-20)
- [ ] Slides
