# Changelog

All notable changes to LAPEG, by phase. Dates are UTC. Commit hashes refer to the `phase-0` branch.
Details behind each finding are in [`docs/assumptions.md`](docs/assumptions.md).

## Phases 2 to 5 and the web app. 2026-10-25 to 2026-10-26

### Added
- **Execution on the fork** (`lapeg/liquidity/`). Pendle Convert API routes are executed as sent, and the Pendle
  pool alone is measured through the router's `swapExactPtForSy`. ABIs come from Sourcify-verified contracts
  (`scripts/fetch_verified_abis.py`); the router is discovered from the API and verified
  (`scripts/discover_pendle_router.py`).
- **Oracle override** (`lapeg/oracle/override.py`, `contracts/MockAggregator.sol`): the PT-reUSD Ojo feed's two
  sources are pointed at a mock; Morpho's oracle then returns the target exactly.
- **Gas, flash liquidity and capacity** (`lapeg/liquidity/capacity.py`). ETH/USD is resolved on-chain via ENS.
- **Illiquidity metrics** with definitions and rationale (`lapeg/metrics/`).
- **Phase 2 experiment** (`experiments/phase2_liquidity.yaml`): depth grid, zoom stages to `q_max`, position book at
  the same block, protocol-verified oracle shocks, `Delta*`, and an end-to-end liquidation plus sale.
- **Real-node cross-check** (`lapeg/liquidity/realnode.py`): a route that fails on the fork is simulated on the
  archive node at the same block, which separates genuine reverts from fork artifacts.
- **Checkpoint and resume** for Phase 2, and automatic anvil restarts on dropped connections.
- **Phase 3 (v0):** design document; liquidator concentration and sizes from Aug 25 events.
- **Phase 4 fast tier** (`lapeg/cascade/fast.py`) and **Phase 5 guard v0** (`lapeg/guard/`). DELAY is used instead
  of DROP; DROP stays off by default.
- **Design for Phases 4 to 7** (`docs/design_phases4_7.md`).
- **Web app** for readers without a DeFi background (`web/`, React; API in `lapeg/app/api.py`; start with
  `python -m lapeg.app`). The measured depth curve as a seabed profile with a draggable sounding line; a what-if
  simulator over all 272 loans (price drop or large sale, refill speed); guard v0 with both outcomes side by side;
  a live lab on a fork (lower the price feed, skip time, sell, liquidate with Morpho's own `liquidate`, undo); a
  glossary behind every technical term. It replaces the Streamlit playground.
- The fast tier records which accounts end in bad debt (`Result.bad_debt_by_account`), and the Phase 4/5 inputs are
  built by one function, `load_recorded`, shared by the experiment and the web app.

### Fixed
- **False reverts from stale quotes.** Sequential bisection quoted each midpoint live and executed it on the earlier
  pinned fork, so routes built for a later market state reverted (`INSUFFICIENT_TOKEN_OUT`, `Call failed`) and
  looked like illiquidity at $13.4M. Measurement now runs in stages: each stage quotes a batch and forks the block
  right after, and `q_max` is bracketed within one stage. Quotes made after their fork block are refused.
- A dropped connection during the real-node cross-check was counted as a revert; transport errors are now retried
  and reported as unknown.
- Anvil without its own throttle exceeded the free endpoint's rate limit; the throttle stays on, and the archive RPC
  can now be set in the git-ignored `lapeg/.env`.
- A slow fork receipt (over 120 s while anvil fetched state) was recorded as a revert. Receipts now wait up to
  15 minutes, and timeouts get their own status, one retry, and are excluded from `q_max`.
- Results were lost when parquet could not store 18-decimal SY amounts (beyond int64). Such columns are now
  written as strings, and the JSON report is written before the parquet files.
- The shock-set check used `HF * (1 - delta) < 1`, which misjudged a dust position that Morpho's integer rounding
  keeps healthy. It now uses Morpho's integer max-borrow, and the sets match at all 11 shocks.
- The fast tier's depth curve mixed sizes measured at different blocks, so near-identical sizes had different
  prices and the marginal price jumped. A later stage now replaces earlier points in its size range, and the curve
  is kept non-decreasing in loss. Results were regenerated; `C*` and the guard decisions are unchanged.
- The playground and the Phase 4/5 run picked the newest `phase2_liquidity_*.json`, which can be the position-book
  report of the same run. They now pick the newest report with depth outputs.

### Results
- **Phase 2** (grid at block 26269480, zoom stages at 26269857 and 26269961): `q_max` between 13.37M and 13.59M PT
  (about $13.19M to $13.40M, 10.7% of borrow), the same with gas and under a 100 gwei stress fee. `G` jumps from
  1.08% to 3.58% between those two sizes. `Delta*` = 2.11% (11 borrowers, 22.1M PT seizable). All 9 acceptance
  checks pass.
- **End to end** at block 26277620: a real `liquidate` of the largest borrower at `Delta*` and a real sale of the
  12.28M PT seized. The profit is overstated, because the override moves the oracle and not the market.
- **Routes that revert on mainnet too:** OKX at $3.84M (`ARM: Insufficient liquidity`) and KyberSwap from 15.5M PT
  (`INSUFFICIENT_TOKEN_OUT`, `Call failed`): quotes can overstate executable depth.
- **Phases 4 and 5** (no refill for one hour, a worst case): the fast tier matches Morpho's liquidatable sets at all
  11 shocks; `C*` is about $10M (155 positions, $94.8M stuck); guard v0 passes $0.5M and $5M sales and delays a $20M
  sale. On Aug 25 the market cleared $35.2M in about 11 minutes with no bad debt, so depth does refill; the refill
  rate is the main open question for Phase 3.
- The Pendle pool alone cannot absorb 10M PT; the API's routes reach further through Pendle limit orders and DEX
  venues.

## Phase 1 (lite): market model. 2026-10-25

Position book for PT-reUSD-10DEC2026/USDC at block 26267600, with health factors checked against Morpho's own code.

### Added
- **Position book from events** (`lapeg/market/position_book.py`). Replays `SupplyCollateral`,
  `WithdrawCollateral`, `Borrow`, `Repay` and `Liquidate` in chain order, following `Morpho.sol` v1.0.0
  accounting, including bad-debt write-offs. A negative balance stops the run, so a missed event cannot pass
  silently. (`39481c9`)
- **Morpho's integer math in Python** (`lapeg/market/morpho_math.py`): share-to-asset conversion with virtual
  shares, the `maxBorrow` and health computation, and the liquidation incentive factor, transcribed from
  `MathLib` and `SharesMathLib`. (`39481c9`)
- **`HealthReader` helper contract** (`contracts/HealthReader.sol`). Mirrors `Morpho._isHealthy` using Morpho's
  vendored libraries, with interest accrued by `MorphoBalancesLib.expectedMarketBalances`. It is never
  deployed: it is injected with an `eth_call` state override (`lapeg/chain/health_reader.py`). (`67fdf53`, `176bcbc`)
- **Ten more Morpho Blue v1.0.0 source files vendored** (math libraries, periphery libraries, `IIrm`). Each is
  byte-identical to upstream; blob hashes are in `contracts/vendor/morpho-blue/SOURCE.md`. (`67fdf53`)
- **Chunked log fetcher** (`lapeg/chain/indexer.py`). Halves the block window on range errors, backs off on
  transient errors, paces requests, and skips the pause after cached responses. (`176bcbc`)
- **Phase 1 experiment** (`experiments/phase1_position_book.yaml`, `lapeg/market/experiment.py`). Discovers the
  market's creation block (Morpho API hint, confirmed by the `CreateMarket` log), fetches all events, rebuilds
  positions, and runs the checks below. (`58fabe7`)
- **Position book output** in the agreed interface: `data/results/position_book_PT-reUSD-10DEC2026_26267600.parquet`
  (`market_id`, `block`, `borrower`, `collateral`, `debt`, `health_factor`, plus raw integers). (`58fabe7`)
- **`logs` section in experiment configs** (`LogsConfig`): log fetching uses its own endpoint, separate from the
  fork RPC. (`58fabe7`)
- **Tests:** maths, event replay including bad debt, log-fetcher coverage and retries, and the Phase 1
  acceptance test (marked `fork` and `slow`). (`176bcbc`, `39481c9`, `58fabe7`)

### Acceptance
| Check | Result |
|---|---|
| Events replayed | 9,879 (3,379 SupplyCollateral, 1,514 WithdrawCollateral, 3,395 Borrow, 1,575 Repay, 16 Liquidate) |
| Positions vs Morpho storage | 284 accounts, all exact |
| Sum of borrow shares vs market total | Exact |
| Health factors vs Morpho's own code | 271 borrowers, max relative error **0** (spec: ≤ 1e-9) |
| Morpho's `liquidate` agrees with `HF >= 1` | All 271 |
| Helper reads at block N's timestamp | Yes |

### Findings
- No position is liquidatable at block 26267600. The lowest non-dust health factor is 1.0037.
- All 16 liquidations in this market's history are the 16 of Aug 25, 2026.

### Changed
- `MarketParams` fields are typed as checksummed addresses. (`58fabe7`)
- The RPC cache records whether the last response was a cache hit, so callers can skip rate-limit pauses.
  Cached reruns of the acceptance test dropped from about 230 s to about 130 s. (`176bcbc`)
- Added the `pyarrow` dependency (parquet output) and the `slow` test marker. (`58fabe7`)

### Deferred
- Aave v3 and Euler v2 adapter stubs, and the collateral registry: outside the v0 "lite" scope. The oracle and
  venue facts the registry would hold are already in `data/discovered/oracles.json`.

## Configuration and documentation. 2026-10-25

### Changed
- **Default RPC is the free archive endpoint `eth.drpc.org`** (`PUBLIC_ARCHIVE_RPC` in `lapeg/config.py`),
  replacing the non-archive public endpoint. Experiments pin their fork block again (26267600), and anvil runs
  with retry options for the free tier's timeouts. (`d178bac`)
- **Root README restructured around the LAPEG spec** into a measurement track (Layers 1 and 2) and a mitigation
  track (LAPEG), with a terminology map, phase status, a draft block diagram, the spec's known conflicts and the
  status of its unverified claims. (`e99aafb`)

## Phase 0: scaffolding. 2026-10-22

Fork control, RPC cache and the addendum's pre-work.

### Added
- **Project layout and tooling** from the spec: `pyproject.toml` (ruff, mypy, pytest), `foundry.toml`, empty
  packages for later phases, and data folders (`raw`, `discovered`, `results`). (`a2ba2ac`)
- **Verified Morpho Blue sources:** `IMorpho.sol`, `IOracle.sol` and `EventsLib.sol` from release v1.0.0
  (commit `55d2d99`), vendored unchanged. ABIs are compiled from them by `scripts/build_abis.py`, not written by
  hand. (`2f01410`)
- **Morpho Blue discovery** (`scripts/discover_morpho.py`). Finds the address via the Morpho API and verifies it
  on-chain. `idToMarketParams` matches the hand-sourced dataset for all three markets. (`b29c1e5`)
- **Read-only Morpho adapter** (`lapeg/chain/morpho.py`): market params, market state, positions, oracle price.
  (`b29c1e5`)
- **Fork control** (`lapeg/fork/`): anvil start and stop, snapshot and revert (including the fact that anvil
  uses up a snapshot when reverting to it), `branch()` for throwaway changes, impersonation, ETH balances,
  storage reads and writes, time and mining. ERC20 balances are set by probing for the balance slot rather than
  hardcoding it. (`a7140c3`)
- **RPC read cache** (`lapeg/chain/rpc_cache.py`): caches only reads that cannot change (pinned blocks, mined
  transactions), in sqlite. (`7f8bcc1`)
- **Experiment configs** (YAML, validated with pydantic) and the Phase 0 smoke experiment. (`aa22849`)
- **Tests:** pytest unit and fork tests, plus a forge fork test. (`4f910f0`)
- **Groundwork scripts** (`87ee138`):
  - `check_archive.py`: probes history, state and forking at a target date.
  - `inspect_oracles.py`: walks each oracle down to its price sources. It identifies unverified contracts by
    matching their bytecode against verified instances.
  - `scan_liquidations.py`: scans `Liquidate` events in a date window.
- **Docs:** `docs/interfaces.md`, `docs/assumptions.md` and the project README. (`0e58a0c`)

### Acceptance
Fork mainnet at a block, read the PT-reUSD market, snapshot, change it with a real `accrueInterest` call, revert,
and confirm the state matches the original. Passes in pytest, forge, and from the experiment config.

### Findings
- **Archive access:** `ethereum-rpc.publicnode.com` is not archive. The free `eth.drpc.org` serves archive state,
  and anvil forks at Aug 25, 2026 work.
- **Oracles:**
  - PT-reUSD: the lower of a 15-minute Pendle TWAP and a 6%/yr linear discount. It follows the market.
  - PT-USD3: a 30%/yr linear discount. It ignores the market.
  - PT-sUSDS: a primary/backup switch whose 13%/yr linear discount currently binds.
- **Aug 25, 2026:** 16 liquidations in the target market repaid 35.19M USDC within 55 blocks, with no bad debt.
