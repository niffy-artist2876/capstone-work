"""Phase 2 experiment: liquidity and oracle measurement for one market, on forks of mainnet.

Live quotes are only valid against the state they were computed from, so the measurement runs in *stages*.
A stage is a batch of Pendle Convert API quotes followed immediately by a fork of the then-current block; all
of a stage's sales are executed on that fork. Stages:
  grid    log-spaced sizes over the whole range (the depth curve and its shape)
  zoomN   sizes spread across the previous stage's q_max bracket, re-quoted with fresh endpoints, so the
          new bracket comes from one consistent state (replaces sequential bisection, which mixed later
          quotes with an earlier fork and produced false reverts)
Then, at the final stage's block: gas and flash liquidity, the position book (Phase 1), protocol-verified
oracle shocks and Delta*, and an end-to-end liquidation followed by the sale of the seized PT.

Every stage is checkpointed per size and resumes at its own block. A route that fails on the fork is
cross-checked on the real archive node at the same block (`realnode`).
Acceptance (spec, Phase 2): depth curve and G(q) at one block; G ~ 0 for tiny q when the oracle is fresh;
G non-decreasing in q. Shared addendum test: fork-executed output matches the API quote at the current block.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from web3 import Web3
from web3.exceptions import Web3RPCError

from lapeg import PROJECT_ROOT
from lapeg.chain.abi import load_abi
from lapeg.chain.health_reader import read_health
from lapeg.chain.morpho import MorphoBlue, load_morpho_address
from lapeg.config import ExperimentConfig, resolve_rpc_url
from lapeg.fork.anvil import AnvilError, AnvilFork
from lapeg.fork.state import BalanceSlot, find_allowance_index, find_balance_slot, set_erc20_balance
from lapeg.liquidity.capacity import GasPrice, flash_liquidity, gas_cost_usd, gas_price
from lapeg.liquidity.execute import Execution, execute_route, sell_pt_for_sy
from lapeg.liquidity.pendle_api import Quote, Route, quote
from lapeg.liquidity.realnode import TokenSlots, call_route
from lapeg.market import morpho_math as mm
from lapeg.market.experiment import run_position_book
from lapeg.metrics import illiquidity as il
from lapeg.oracle.override import override_ojo_feed

DISCOVERED = PROJECT_ROOT / "data" / "discovered"
TRANSIENT = (AnvilError, requests.exceptions.ConnectionError, TimeoutError, Web3RPCError)


# ---------- helpers ----------
def _grid(lo: float, hi: float, growth: float) -> list[float]:
    n = int(math.floor(math.log(hi / lo) / math.log(growth))) + 1
    return [lo * growth**i for i in range(n)]


def _spread(lo: int, hi: int, n: int) -> list[int]:
    """n sizes from lo to hi inclusive, geometrically spaced."""
    return [int(lo * (hi / lo) ** (i / (n - 1))) for i in range(n)]


def _oracle_setup(name: str) -> dict[str, str]:
    tree = json.loads((DISCOVERED / "oracles.json").read_text(encoding="utf-8"))["markets"][name]["tree"]
    feed = tree["children"].get("BASE_FEED_1", {})
    if not str(feed.get("verified_name", "")).startswith("OjoPTFeed"):
        raise RuntimeError(f"{name}: oracle override is implemented for OjoPTFeed base feeds only")
    twap = feed["children"]["FEED_1"]
    return {"ojo": feed["address"], "pendle_market": twap["children"]["market"]["address"]}


def _head(url: str, attempts: int = 6) -> tuple[int, int]:
    """(block number, timestamp) of the current head, retried on a fresh connection."""
    for i in range(attempts):
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
            b = w3.eth.get_block("latest")
            return int(b["number"]), int(b["timestamp"])
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2**i)
    raise AssertionError("unreachable")


def _upstream_failure(e: Exception) -> bool:
    """Anvil could not fetch forked state from the upstream RPC (rate limit, dropped connection)."""
    msg = str(e).lower()
    keys = (
        "failed to get account",
        "failed to get storage",
        "429",
        "rate limit",
        "connection closed",
        "error sending request",
    )
    return any(k in msg for k in keys)


def _quote_to_json(phase: str, q: Quote) -> dict[str, Any]:
    return {"phase": phase, "quote": {**asdict(q), "routes": [asdict(r) for r in q.routes]}}


def _quote_from_json(d: dict[str, Any]) -> tuple[str, Quote]:
    q = dict(d["quote"])
    q["routes"] = [Route(**r) for r in q["routes"]]
    return d["phase"], Quote(**q)


def _utc(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())


@dataclass
class BlockContext:
    """What a stage's measurements need from its fork."""

    price: int
    lif: int
    thr: float
    gp: GasPrice
    slot_pt: BalanceSlot
    token_slots: TokenSlots
    sy: str
    sy_rate: int
    sy_dec: int
    params: Any
    flash: int


# ---------- checkpoint ----------
class Checkpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stages: list[dict[str, Any]] = []
        if path.exists():
            raw = json.loads(path.read_text())
            if "stages" in raw:
                self.stages = raw["stages"]
            else:  # older single-block format: keep the grid, drop anything quoted after the fork
                keep = {int(q["quote"]["amount_in"]) for q in raw["quotes"] if q["phase"] == "grid"}
                self.stages = [
                    {
                        "name": "grid",
                        "block": raw["block"],
                        "block_ts": None,
                        "quotes": [q for q in raw["quotes"] if q["phase"] == "grid"],
                        "rows": [r for r in raw["rows"] if r["amount_in"] in keep],
                        "route_rows": [r for r in raw["route_rows"] if int(r["amount_in"]) in keep],
                    }
                ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"stages": self.stages}, default=str))

    def stage(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.stages if s["name"] == name), None)


# ---------- the experiment ----------
def run_liquidity(cfg: ExperimentConfig, write_results: bool = True) -> dict[str, Any]:
    t0 = time.monotonic()
    p = cfg.params
    market = next(
        m
        for m in json.loads((DISCOVERED / "markets.json").read_text())["markets"]
        if m["market_id"] == cfg.market.market_id
    )
    pt, usdc, oracle = market["collateral_token"], market["loan_token"], market["oracle"]
    setup = _oracle_setup(cfg.market.name)
    morpho_addr = load_morpho_address(cfg.path(cfg.morpho_discovery))
    seller = Web3.to_checksum_address(Web3.keccak(text=f"lapeg-seller-{cfg.seed}")[-20:])
    url, rpc_source = resolve_rpc_url(cfg.fork)
    up = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))
    real = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 120}))  # real-node cross-check
    loan_dec = int(up.eth.contract(address=usdc, abi=load_abi("ERC20")).functions.decimals().call())
    stress_fee = int(float(p.get("stress_priority_fee_gwei", 100)) * 1e9)
    max_quote_lag = int(p.get("max_quote_after_block_s", 60))
    ckpt = Checkpoint(cfg.path(Path("data/raw")) / f"checkpoint_{cfg.name}_{cfg.market.name}.json")
    if not p.get("resume", True):
        ckpt.stages = []

    def new_stage(name: str, amounts: list[int]) -> dict[str, Any]:
        print(f"[{name}] quoting {len(amounts)} sizes")
        quotes = [_quote_to_json(name, quote(pt, usdc, a, seller)) for a in amounts]
        block, ts = _head(url)  # fork the state the quotes were computed against
        print(f"[{name}] forking block {block}")
        st = {"name": name, "block": block, "block_ts": ts, "quotes": quotes, "rows": [], "route_rows": []}
        ckpt.stages.append(st)
        ckpt.save()
        return st

    def measure_stage(st: dict[str, Any]) -> BlockContext:
        """Execute every pending quote of a stage on a fork of its block. Retries transient failures."""
        quotes = [_quote_from_json(d) for d in st["quotes"]]
        stale = [
            q.amount_in
            for _, q in quotes
            if st["block_ts"] and _utc(q.quoted_utc) > st["block_ts"] + max_quote_lag
        ]
        if stale:
            raise RuntimeError(
                f"[{st['name']}] {len(stale)} quotes were made after the fork block: not executable"
            )
        retries = int(p.get("fork_retries", 6))
        for attempt in range(retries):
            try:
                with AnvilFork(
                    url,
                    st["block"],
                    extra_args=cfg.fork.anvil_args,
                    startup_timeout_s=cfg.fork.startup_timeout_s,
                ) as fork:
                    ctx = _context(fork, st["block"], morpho_addr, cfg, setup, pt, usdc)
                    st["price"] = ctx.price
                    done = {r["amount_in"] for r in st["rows"]}
                    for phase, q in quotes:
                        if q.amount_in not in done:
                            st["rows"].append(
                                _measure(
                                    fork,
                                    ctx,
                                    real,
                                    st,
                                    phase,
                                    q,
                                    pt,
                                    usdc,
                                    seller,
                                    setup,
                                    loan_dec,
                                    stress_fee,
                                )
                            )
                            ckpt.save()
                    return ctx
            except TRANSIENT as e:
                if isinstance(e, Web3RPCError) and not _upstream_failure(e):
                    raise
                print(
                    f"  [{st['name']}] fork failed ({type(e).__name__}); restarting at block {st['block']} "
                    f"({attempt + 1}/{retries})"
                )
                if attempt == retries - 1:
                    raise
                time.sleep(60 if "429" in str(e) or "rate limit" in str(e).lower() else 10)
        raise AssertionError("unreachable")

    # 1. coarse grid
    price_now = MorphoBlue(up, morpho_addr).oracle_price(oracle)
    grid = ckpt.stage("grid")
    if grid is None:
        sizes = _grid(float(p.get("min_usd", 1e5)), float(p.get("max_usd", 4e7)), float(p.get("growth", 1.5)))
        grid = new_stage("grid", [int(s * 10**loan_dec * il.ORACLE_PRICE_SCALE // price_now) for s in sizes])
    else:
        print(f"[grid] resuming at block {grid['block']}: {len(grid['rows'])} of {len(grid['quotes'])} done")
    ctx = measure_stage(grid)

    # 2. zoom rounds: each re-quotes the bracket of the previous stage and forks a fresh block
    final = grid
    for z in range(1, int(p.get("zoom_rounds", 2)) + 1):
        lo, hi = _bracket(final, ctx)
        if (
            lo is None
            or hi is None
            or (hi.amount_in - lo.amount_in) / lo.amount_in <= float(p.get("rel_tol", 0.02))
        ):
            break
        name = f"zoom{z}"
        st = ckpt.stage(name) or new_stage(
            name, _spread(lo.amount_in, hi.amount_in, int(p.get("zoom_points", 8)))
        )
        ctx = measure_stage(st)
        final = st

    # 3. results at the final stage's block
    block = final["block"]
    brackets = {
        "bonus": _bracket(final, ctx, "bonus"),
        "bonus_gas": _bracket(final, ctx, "gas"),
        "bonus_stress_gas": _bracket(final, ctx, "stress"),
    }
    lo, hi = brackets["bonus_gas"]
    book_cfg = cfg.model_copy(
        update={"fork": cfg.fork.model_copy(update={"block": block, "allow_recent_block": False})}
    )
    book = run_position_book(book_cfg, write_results=write_results)
    frame = book["_frame"]
    borrowers = frame[frame.borrow_shares_raw != "0"]
    shock_rows, override_exact, set_mismatch, delta_star, target = _shocks(
        cfg, url, block, morpho_addr, oracle, setup, borrowers, ctx, lo, hi
    )
    e2e = (
        _end_to_end(cfg, url, morpho_addr, oracle, setup, ctx, delta_star, target, pt, usdc, loan_dec)
        if delta_star and target and p.get("end_to_end", True)
        else {}
    )

    # 4. acceptance
    grid_ok = [r for r in grid["rows"] if r["status"] == "ok"]
    smallest = min(grid_ok, key=lambda r: r["amount_in"]) if grid_ok else None
    viol = {
        s["name"]: il.monotone_violations(
            _points(s), _stage_price(s, ctx), float(p.get("monotone_tol", 1e-4))
        )
        for s in ckpt.stages
    }
    api_rows = [
        r
        for s in ckpt.stages
        for r in s["rows"]
        if r["status"] == "ok"
        and r.get("api_match") is not None
        and r.get("measured_on") == "fork"
        and (lo is None or r["amount_in"] <= lo.amount_in)
    ]
    checks = {
        "depth_curve_has_points": len(grid_ok) >= 8,
        "g_near_zero_for_tiny_q": smallest is not None
        and abs(smallest["g"]) <= float(p.get("tiny_g_tol", 0.005)),
        "g_non_decreasing": not any(viol.values()),
        "fork_matches_api_within_qmax": bool(api_rows)
        and max(abs(r["api_match"]) for r in api_rows) <= float(p.get("api_match_tol", 0.001)),
        "q_max_bracketed": lo is not None and hi is not None,
        "no_timeouts_left": not any(r["status"] == "timeout" for s in ckpt.stages for r in s["rows"]),
        "oracle_override_exact": override_exact,
        "shock_sets_match_position_book": set_mismatch == 0,
        "position_book_passed": bool(book["passed"]),
    }

    def amount_out(x: il.DepthPoint | None) -> dict[str, Any] | None:
        return (
            None
            if x is None
            else {
                "amount_pt": x.amount_in / 10**6,
                "usd_at_oracle": x.amount_in * ctx.price / il.ORACLE_PRICE_SCALE / 10**loan_dec,
            }
        )

    borrow_usd = float(borrowers.debt.sum())
    rows = [{**r, "stage": s["name"], "block": s["block"]} for s in ckpt.stages for r in s["rows"]]
    route_rows = [
        {**r, "stage": s["name"], "block": s["block"]} for s in ckpt.stages for r in s["route_rows"]
    ]
    report: dict[str, Any] = {
        "experiment": cfg.name,
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ"),
        "rpc_source": rpc_source,
        "fork_block": block,
        "block_pinned": True,
        "stages": [
            {
                "name": s["name"],
                "block": s["block"],
                "n": len(s["rows"]),
                "bracket_gas": {
                    k: amount_out(v)
                    for k, v in zip(("last_ok", "first_fail"), _bracket(s, ctx, "gas"), strict=True)
                },
            }
            for s in ckpt.stages
        ],
        "market": {"name": cfg.market.name, "id": cfg.market.market_id},
        "oracle_price": ctx.price / il.ORACLE_PRICE_SCALE,
        "lif": ctx.lif / 1e18,
        "bonus_threshold": ctx.thr,
        "gas": {
            "base_fee_gwei": ctx.gp.base_fee_wei / 1e9,
            "eth_usd": ctx.gp.eth_usd,
            "eth_usd_feed": ctx.gp.feed,
            "stress_priority_fee_gwei": stress_fee / 1e9,
        },
        "q_max": {
            k: {"last_ok": amount_out(v[0]), "first_fail": amount_out(v[1])} for k, v in brackets.items()
        },
        "q_max_share_of_borrow": (
            lo.amount_in * ctx.price / il.ORACLE_PRICE_SCALE / 10**loan_dec / borrow_usd
        )
        if lo
        else None,
        "flash_liquidity_usdc": ctx.flash / 10**loan_dec,
        "capacity_per_block_usd": amount_out(lo),
        "delta_star": delta_star,
        "shocks": shock_rows,
        "end_to_end": e2e,
        "monotone_violations": viol,
        "n_points": len(rows),
        "n_routes_executed": len(route_rows),
        "routes_reverting_on_real_node": [
            {k: r[k] for k in ("stage", "amount_in", "aggregator", "real_node_error")}
            for r in route_rows
            if r.get("real_node_status") == "reverted"
        ],
        "sy": {"address": ctx.sy, "exchange_rate": str(ctx.sy_rate), "decimals": ctx.sy_dec},
        "position_book": {
            "n_borrowers": book["n_borrowers"],
            "min_health_factor": book["min_health_factor"],
            "parquet": book.get("parquet"),
        },
        "seconds": round(time.monotonic() - t0, 1),
        "checks": checks,
        "passed": all(checks.values()),
    }
    if write_results:
        out = cfg.path(cfg.results_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{cfg.name}_{cfg.market.name}_{block}"
        report["outputs"] = [f"{stem}_depth.parquet", f"{stem}_routes.parquet", f"{stem}_shocks.parquet"]
        # report first: a failed parquet write must not lose the shocks and end-to-end results
        (out / f"{cfg.name}_{report['run_utc']}.json").write_text(json.dumps(report, indent=1, default=str))
        _frame(rows).to_parquet(out / f"{stem}_depth.parquet", index=False)
        _frame(route_rows).to_parquet(out / f"{stem}_routes.parquet", index=False)  # calldata: replayable
        _frame(shock_rows).to_parquet(out / f"{stem}_shocks.parquet", index=False)
    report["_depth"] = pd.DataFrame(rows)
    return report


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """DataFrame for parquet: integer columns beyond int64 (18-decimal raw amounts) are stored as strings."""
    df = pd.DataFrame(rows)
    for c in df.columns:
        if any(isinstance(v, int) and not -(2**63) <= v < 2**63 for v in df[c]):
            df[c] = df[c].map(lambda v: None if v is None else str(v))
    return df


# ---------- per-stage pieces ----------
def _context(
    fork: AnvilFork,
    block: int,
    morpho_addr: str,
    cfg: ExperimentConfig,
    setup: dict[str, str],
    pt: str,
    usdc: str,
) -> BlockContext:
    assert fork.w3 is not None
    w3 = fork.w3
    fork.set_block_interval(12)  # deterministic timestamps: fork block + 12 s per mined block
    morpho = MorphoBlue(w3, morpho_addr)
    params = morpho.market_params(cfg.market.market_id)
    price = morpho.oracle_price(params.oracle)
    lif = mm.liquidation_incentive_factor(params.lltv)
    slot_pt = find_balance_slot(fork, pt)
    allow_index, allow_layout = find_allowance_index(fork, pt)
    pmarket = w3.eth.contract(
        address=Web3.to_checksum_address(setup["pendle_market"]), abi=load_abi("PendleMarket")
    )
    sy, mpt, _ = pmarket.functions.readTokens().call()
    if mpt.lower() != pt.lower():
        raise RuntimeError("Pendle market does not trade this PT")
    syc = w3.eth.contract(address=sy, abi=load_abi("PendleSY"))
    return BlockContext(
        price=price,
        lif=lif,
        thr=il.bonus_threshold(lif),
        gp=gas_price(w3, block),
        slot_pt=slot_pt,
        token_slots=TokenSlots(pt, slot_pt.index, slot_pt.layout, allow_index, allow_layout),
        sy=sy,
        sy_rate=int(syc.functions.exchangeRate().call()),
        sy_dec=int(w3.eth.contract(address=sy, abi=load_abi("ERC20")).functions.decimals().call()),
        params=params,
        flash=flash_liquidity(w3, morpho_addr, usdc),
    )


def _measure(
    fork: AnvilFork,
    ctx: BlockContext,
    real: Web3,
    st: dict[str, Any],
    phase: str,
    q: Quote,
    pt: str,
    usdc: str,
    seller: str,
    setup: dict[str, str],
    loan_dec: int,
    stress_fee: int,
) -> dict[str, Any]:
    amt = q.amount_in
    best: Execution | None = None
    best_route: Route | None = None
    measured_on = None
    first = len(st["route_rows"])
    for i, r in enumerate(q.routes):
        ex = execute_route(fork, r, pt, usdc, amt, ctx.slot_pt)
        if ex.status == "timeout":  # first touch of many venues: retry once on the warmed fork
            ex = execute_route(fork, r, pt, usdc, amt, ctx.slot_pt)
        rn = None
        if ex.status != "ok":  # same tx on the real node at the same block: real revert or fork artifact?
            rn = call_route(real, st["block"], ctx.token_slots, r.tx_from, r.tx_to, r.tx_data, amt)
        st["route_rows"].append(
            {
                "phase": phase,
                "amount_in": str(amt),
                "route": i,
                "aggregator": r.aggregator,
                "status": ex.status,
                "amount_out": str(ex.amount_out),
                "quoted_out": str(r.quoted_out),
                "gas_used": ex.gas_used,
                "error": ex.error,
                "tx_to": r.tx_to,
                "tx_from": r.tx_from,
                "tx_data": r.tx_data,
                "quoted_utc": q.quoted_utc,
                "real_node_status": rn.status if rn else None,
                "real_node_out": str(rn.amount_out) if rn else None,
                "real_node_error": rn.error if rn else None,
            }
        )
        if ex.status == "ok" and (best is None or ex.amount_out > best.amount_out):
            best, best_route, measured_on = ex, r, "fork"
        elif rn and rn.status == "ok" and (best is None or rn.amount_out > best.amount_out):
            best, best_route, measured_on = (
                Execution("ok", rn.amount_out, int(r.api_gas or 0)),
                r,
                "real_node_call",
            )
    router = q.routes[0].tx_to if q.routes else None
    leg = (
        sell_pt_for_sy(fork, router, setup["pendle_market"], pt, ctx.sy, amt, seller, ctx.slot_pt)
        if router
        else None
    )
    mine = st["route_rows"][first:]
    status = (
        "refused"
        if q.refusal
        else "ok"
        if best
        else "timeout"
        if any(x["status"] == "timeout" for x in mine)
        else "reverted"
        if q.routes
        else "no_route"
    )
    row: dict[str, Any] = {
        "phase": phase,
        "amount_in": amt,
        "size_usd_at_oracle": amt * ctx.price / il.ORACLE_PRICE_SCALE / 10**loan_dec,
        "status": status,
        "refusal": q.refusal,
        "n_routes": len(q.routes),
        "amount_out": best.amount_out if best else 0,
        "measured_on": measured_on,
        "n_routes_reverting_on_real_node": sum(1 for x in mine if x["real_node_status"] == "reverted"),
        "gas_used": best.gas_used if best else 0,
        "aggregator": best_route.aggregator if best_route else None,
        "api_quoted_out": best_route.quoted_out if best_route else None,
        "api_internal_impact": best_route.internal_impact if best_route else None,
        "api_external_impact": best_route.external_impact if best_route else None,
        "pendle_leg_status": leg.status if leg else None,
        "pendle_leg_error": leg.error if leg else None,
        "pendle_leg_sy_out": leg.amount_out if leg else None,
    }
    if best and best_route:
        value = amt * ctx.price / il.ORACLE_PRICE_SCALE
        g_usd, s_usd = gas_cost_usd(best.gas_used, ctx.gp), gas_cost_usd(best.gas_used, ctx.gp, stress_fee)
        row.update(
            g=il.oracle_gap(best.amount_out, amt, ctx.price),
            api_match=best.amount_out / best_route.quoted_out - 1,
            gas_usd=g_usd,
            gas_usd_stress=s_usd,
            threshold_gas=il.gas_adjusted_threshold(ctx.lif, value, g_usd * 10**loan_dec),
            threshold_stress=il.gas_adjusted_threshold(ctx.lif, value, s_usd * 10**loan_dec),
        )
    if leg and leg.status == "ok":
        row["g_pendle_leg"] = 1 - (leg.amount_out * ctx.sy_rate / 1e18) * il.ORACLE_PRICE_SCALE / (
            amt * ctx.price
        )
    g_txt = (
        f"G={100 * row['g']:.3f}%" if "g" in row else status + (f" ({q.refusal[:50]})" if q.refusal else "")
    )
    print(f"  {phase:7s} {row['size_usd_at_oracle'] / 1e6:8.3f}M  {g_txt}")
    return row


def _points(st: dict[str, Any]) -> list[il.DepthPoint]:
    # a timeout says nothing about the market: excluded, neither passing nor failing
    return [
        il.DepthPoint(r["amount_in"], r["status"], r["amount_out"], r["gas_used"])
        for r in st["rows"]
        if r["status"] != "timeout"
    ]


def _stage_price(st: dict[str, Any], ctx: BlockContext) -> int:
    """Oracle price at the stage's block (stored when the stage was measured)."""
    return int(st.get("price") or ctx.price)


def _bracket(
    st: dict[str, Any], ctx: BlockContext, kind: str = "gas"
) -> tuple[il.DepthPoint | None, il.DepthPoint | None]:
    price = _stage_price(st, ctx)
    thr = il.bonus_threshold(ctx.lif)
    by_amount = {r["amount_in"]: r for r in st["rows"]}

    def threshold(x: il.DepthPoint) -> float:
        r = by_amount[x.amount_in]
        return (
            thr
            if kind == "bonus"
            else r.get("threshold_stress" if kind == "stress" else "threshold_gas", thr)
        )

    return il.q_max_bracket(_points(st), price, threshold)


def _shocks(
    cfg: ExperimentConfig,
    url: str,
    block: int,
    morpho_addr: str,
    oracle: str,
    setup: dict[str, str],
    borrowers: pd.DataFrame,
    ctx: BlockContext,
    lo: il.DepthPoint | None,
    hi: il.DepthPoint | None,
) -> tuple[list[dict[str, Any]], bool, int, Any, dict[str, Any] | None]:
    p = cfg.params
    shock_rows: list[dict[str, Any]] = []
    override_exact, set_mismatch, delta_star = True, 0, None
    target: dict[str, Any] | None = None
    with AnvilFork(
        url, block, extra_args=cfg.fork.anvil_args, startup_timeout_s=cfg.fork.startup_timeout_s
    ) as fork:
        assert fork.w3 is not None
        w3 = fork.w3
        fork.set_block_interval(12)
        morpho = MorphoBlue(w3, morpho_addr)
        accounts = list(borrowers.borrower)
        base_answer = (
            ctx.price // 10**18
        )  # base feed answer (18 dec) x SCALE_FACTOR (1e18) = price (1e36 scale)

        def shocked(delta: float) -> dict[str, Any]:
            answer = int(base_answer * (1 - delta))
            override_ojo_feed(fork, setup["ojo"], answer)
            p_shock = morpho.oracle_price(oracle)
            snap = read_health(w3, morpho_addr, cfg.market.market_id, accounts, accrue=True)
            liq = {a: s for a, s in snap.accounts.items() if s.borrowed > s.max_borrow}
            q_seiz = sum(
                min(s.collateral, s.borrowed * ctx.lif * il.ORACLE_PRICE_SCALE // (p_shock * 10**18))
                for s in liq.values()
            )
            return {
                "answer": answer,
                "price": p_shock,
                "liq": liq,
                "q_tot": sum(s.collateral for s in liq.values()),
                "q_seiz": q_seiz,
            }

        hf = dict(zip(borrowers.borrower, borrowers.health_factor, strict=True))
        # expected set from the book in Morpho's integer math (HF * (1 - delta) < 1 misjudges dust positions)
        raw = {
            a: (int(c), int(d))
            for a, c, d in zip(borrowers.borrower, borrowers.collateral_raw, borrowers.debt_raw, strict=True)
        }

        def expected_liq(price: int) -> set[str]:
            return {a for a, (c, d) in raw.items() if mm.max_borrow(c, price, ctx.params.lltv) < d}

        for delta in [
            float(x) for x in p.get("shocks", [0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2])
        ]:
            with fork.branch():
                s = shocked(delta)
            override_exact &= s["price"] == s["answer"] * 10**18
            set_mismatch += len(expected_liq(s["price"]) ^ set(s["liq"]))
            shock_rows.append(asdict(il.shock_row(delta, len(s["liq"]), s["q_tot"], s["q_seiz"], lo, hi)))

        # Delta*: walk the trigger shocks Delta_i = 1 - 1/HF_i in order; check each one with the override
        for trig in sorted({1 - 1 / h for h in hf.values() if 0 < h < float("inf")}):
            if trig < 0:
                continue
            with fork.branch():
                s = shocked(trig + 1e-9)
            if il.can_clear(s["q_seiz"], lo, hi) is not True:
                delta_star = {
                    "delta": trig + 1e-9,
                    "n_liquidatable": len(s["liq"]),
                    "q_seizable_pt": s["q_seiz"],
                    "q_total_pt": s["q_tot"],
                    "verdict": il.can_clear(s["q_seiz"], lo, hi),
                }
                who, acct = max(s["liq"].items(), key=lambda kv: kv[1].collateral)
                expected = min(
                    acct.collateral, acct.borrowed * ctx.lif * il.ORACLE_PRICE_SCALE // (s["price"] * 10**18)
                )
                target = {"borrower": who, "expected_seized_pt": expected}
                break

    return shock_rows, override_exact, set_mismatch, delta_star, target


def _end_to_end(
    cfg: ExperimentConfig,
    url: str,
    morpho_addr: str,
    oracle: str,
    setup: dict[str, str],
    ctx: BlockContext,
    delta_star: dict[str, Any],
    target: dict[str, Any],
    pt: str,
    usdc: str,
    loan_dec: int,
) -> dict[str, Any]:
    """At Delta*: a real `liquidate` of the largest liquidatable borrower, then the sale of the seized PT on a
    real route. The sale is quoted first and the fork taken right after, so quote and state match."""
    liquidator = Web3.to_checksum_address(Web3.keccak(text=f"lapeg-liquidator-{cfg.seed}")[-20:])
    sale_q = quote(pt, usdc, int(target["expected_seized_pt"]), liquidator)
    block, _ = _head(url)
    with AnvilFork(
        url, block, extra_args=cfg.fork.anvil_args, startup_timeout_s=cfg.fork.startup_timeout_s
    ) as fork:
        assert fork.w3 is not None
        w3 = fork.w3
        fork.set_block_interval(12)
        morpho = MorphoBlue(w3, morpho_addr)
        price = morpho.oracle_price(oracle)
        borrower = Web3.to_checksum_address(target["borrower"])
        # health drifts between blocks: deepen the shock until Morpho agrees the position is liquidatable
        acct, delta = None, float(delta_star["delta"])
        for _ in range(10):
            override_ojo_feed(fork, setup["ojo"], int(price // 10**18 * (1 - delta)))
            a = read_health(w3, morpho_addr, cfg.market.market_id, [borrower], accrue=True).accounts[borrower]
            if a.borrowed > a.max_borrow:
                acct = a
                break
            delta += 0.001
        if acct is None:
            return {"status": "borrower not liquidatable at this block", "block": block}
        need = acct.borrowed + 10**loan_dec
        fork.set_eth_balance(liquidator, 10**20)
        set_erc20_balance(fork, usdc, liquidator, need)
        erc_pt = w3.eth.contract(address=Web3.to_checksum_address(pt), abi=load_abi("ERC20"))
        erc_usdc = w3.eth.contract(address=Web3.to_checksum_address(usdc), abi=load_abi("ERC20"))
        with fork.impersonated(liquidator):
            erc_usdc.functions.approve(morpho_addr, need).transact({"from": liquidator})
            rc = w3.eth.wait_for_transaction_receipt(
                morpho.contract.functions.liquidate(
                    ctx.params.as_tuple(), borrower, 0, acct.borrow_shares, b""
                ).transact({"from": liquidator, "gas": 3_000_000})
            )
        seized = int(erc_pt.functions.balanceOf(liquidator).call())
        repaid = need - int(erc_usdc.functions.balanceOf(liquidator).call())
        sales = [execute_route(fork, r, pt, usdc, sale_q.amount_in) for r in sale_q.routes]
        ok = [x for x in sales if x.status == "ok"]
        sale = max(ok, key=lambda x: x.amount_out) if ok else None
        # the sale was quoted for the previous block's estimate; scale proceeds to the PT actually seized
        proceeds = (sale.amount_out * seized // sale_q.amount_in) if sale else 0
        gp = gas_price(w3, block)
        gas_total = int(rc["gasUsed"]) + (sale.gas_used if sale else 0)
        return {
            "block": block,
            "borrower": borrower,
            "delta": delta,
            "liquidate_status": int(rc["status"]),
            "liquidate_gas": int(rc["gasUsed"]),
            "seized_pt": seized,
            "quoted_pt": sale_q.amount_in,
            "repaid_usdc": repaid,
            "sale_status": sale.status if sale else (sale_q.refusal or "reverted"),
            "sale_usdc": proceeds,
            "gas_usd": gas_cost_usd(gas_total, gp),
            "profit_usdc": (proceeds - repaid) / 10**loan_dec - gas_cost_usd(gas_total, gp),
        }
