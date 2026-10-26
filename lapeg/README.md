# LAPEG: Liquidity-Aware Pre-Execution Guard

Research prototype for the capstone "Measurement and Mitigation of Illiquidity in Collateral-Based Lending in
DeFi". It simulates pending transactions on a mainnet fork and predicts whether they push positions backed by
illiquid collateral into liquidations the market cannot absorb.

**Status:**
- **Phase 0 complete:** fork lifecycle, snapshots, impersonation, balances, storage writes, RPC cache.
- **Phase 1 (lite) complete:** position book for PT-reUSD-10DEC2026 rebuilt from events; health factors match
  Morpho's own computation exactly for all 271 borrowers.
- **Phase 2 complete:** sales executed on the fork through Pendle's aggregator routes and the Pendle pool alone;
  oracle override; gas, flash liquidity and capacity; illiquidity metrics
  ([`lapeg/metrics/README.md`](lapeg/metrics/README.md)). Failing routes are cross-checked on a real node.
  q_max is 13.37M to 13.59M PT (about $13.2M to $13.4M); Delta* is 2.11%. All 9 acceptance checks pass.
- **Phase 3 (v0):** design ([`docs/phase3_design.md`](docs/phase3_design.md)), Aug 25 located, liquidator
  concentration and sizes calibrated.
- **Phase 4 fast tier and Phase 5 guard v0** run on the Phase 2 data; design for Phases 4 to 7 in
  [`docs/design_phases4_7.md`](docs/design_phases4_7.md).
- **Web app:** `python -m lapeg.app` (React front end in `web/`, API in `lapeg/app/api.py`).
- Pre-work from the addendum: archive check, interfaces, oracle sources, Aug 25 event scan.

See [`docs/assumptions.md`](docs/assumptions.md) for findings and open items.

## Setup

Requires Python 3.11+ and Foundry (`anvil`, `forge`, `cast`).

```bash
pip install -e ".[dev]"
forge install foundry-rs/forge-std@v1.10.0 --no-git   # lib/ is not committed
export ARCHIVE_RPC_URL=...                             # optional: a keyed archive RPC; see .env.example
```

Without `ARCHIVE_RPC_URL`, everything uses the free public archive endpoint `eth.drpc.org` (no key; about 100
blocks per `eth_getLogs` request, occasional timeouts that anvil retries). Experiments pin their fork block, so
runs are reproducible on either.

## Layout

```
experiments/        one YAML per run (RPC env var, block, seed, market, parameters)
data/discovered/    facts found on-chain, each with the query used
data/results/       run outputs (JSON)
data/raw/           local caches (git-ignored)
contracts/vendor/   verified sources, copied unchanged (see SOURCE.md)
docs/               interfaces.md, assumptions.md
lapeg/fork/         anvil lifecycle, snapshots, state overrides, Phase 0 acceptance flow
lapeg/chain/        ABIs (compiled from vendored sources), Morpho Blue adapter, RPC cache
lapeg/...           market, liquidity, oracle, agents, cascade, guard, policy, replay, metrics, viz (later phases)
lapeg/app/          web app API (api.py) and live fork session (session.py)
web/                web app front end (React, TypeScript, Vite)
scripts/            discovery and build scripts
tests/, test/       pytest and forge tests
```

## Commands

| Task | Command |
|---|---|
| Phase 0 acceptance (from config) | `python scripts/run_experiment.py experiments/phase0_fork_smoke.yaml` |
| Phase 1 position book + acceptance | `python scripts/run_experiment.py experiments/phase1_position_book.yaml` |
| Phase 2 liquidity and oracle measurement (resumes from its checkpoint) | `python scripts/run_experiment.py experiments/phase2_liquidity.yaml` |
| Phases 4 and 5 on measured data (needs a Phase 2 run) | `python scripts/run_experiment.py experiments/phase45_cascade_guard.yaml` |
| Liquidator calibration from Aug 25 events | `python scripts/calibrate_liquidators.py` |
| Web app (`pip install -e ".[app]"`, then `npm install && npm run build` in `web/` once) | `python -m lapeg.app` |
| Web app front end in development (API on port 8000, page reloads on edit) | `uvicorn lapeg.app.api:app --port 8000`, then `npm run dev` in `web/` |
| Fast tests only (no network) | `pytest -m "not fork"` |
| Python tests (includes fork tests) | `pytest` (skip fork tests: `pytest -m "not fork"`) |
| Solidity fork test | `forge test` |
| Lint and types | `ruff check . && mypy lapeg scripts tests` |
| Rebuild ABIs from vendored sources | `python scripts/build_abis.py` |
| Discover and verify Morpho Blue and markets | `python scripts/discover_morpho.py` |
| Walk each market's oracle to its price sources | `python scripts/inspect_oracles.py` |
| Check archive access at a date | `python scripts/check_archive.py --date 2026-08-25` |
| Scan `Liquidate` events in a date window | `python scripts/scan_liquidations.py --start 2026-08-24 --end 2026-08-27` |

## Fork API (Phase 0)

```python
from lapeg.fork import AnvilFork

with AnvilFork(rpc_url, block) as fork:
    snap = fork.snapshot()                  # consumed by revert
    fork.impersonate(addr); fork.set_eth_balance(addr, 10**18)
    fork.set_storage(token, slot, value)    # anvil_setStorageAt
    with fork.branch():                     # everything inside is undone on exit
        ...
    fork.revert(snap)
```

`lapeg.fork.state.set_erc20_balance(fork, token, holder, amount)` finds the token's balance slot by probing.
`swap(token_in, token_out, amount)` from the agreed fork interface is Phase 2 work.

## Position book (Phase 1)

`data/results/position_book_<market>_<block>.parquet`, one row per account with collateral or debt. The first six
columns are the interface agreed for the cascade code:

| Column | Meaning |
|---|---|
| `market_id`, `block`, `borrower` | Market, fork block, account |
| `collateral` | Collateral in token units (PT) |
| `debt` | Debt in loan-token units, interest accrued to the fork's call timestamp |
| `health_factor` | `maxBorrow / borrowed` with Morpho's rounding; `< 1` means liquidatable |

Extra columns: `health_factor_morpho` (from Morpho's own code via `HealthReader`), `ltv`, and exact raw integers
(`collateral_raw`, `borrow_shares_raw`, `debt_raw`, `max_borrow_raw`).

## Ground rules

From the spec: protocol logic always comes from deployed contracts on a fork; every run is reproducible from a
config; addresses and events are discovered on-chain and recorded with their queries; ABIs come from verified
sources.
