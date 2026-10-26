"""Phases 4 and 5 (shortened scope) on measured data: fast-tier cascades and guard v0 decisions.

Inputs (no network): the latest Phase 2 report for the market, with its executed depth curve and gas, the
position book at the same block, and the oracle structure in data/discovered/oracles.json.

Runs:
  1. Consistency with Phase 2: the fast tier with no feedback (instant oracle, one block) must reproduce the
     number of liquidatable borrowers Morpho itself reported under the oracle override, at every shock.
  2. Oracle-shock sweep with feedback (instant oracle and 15-minute TWAP): liquidations, stuck debt, bad debt.
  3. Sale-shock sweep: PT sold into the market, propagating through the TWAP oracle. C*: the smallest sale
     that leaves unclearable (stuck) debt or bad debt.
  4. Guard v0: a block of synthetic pending sales; PASS or DELAY with counterfactuals (DROP off).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from lapeg import PROJECT_ROOT
from lapeg.cascade.fast import DepthCurve, MarketModel, Params, Position, simulate
from lapeg.config import ExperimentConfig
from lapeg.guard.guard import PendingTx, Policy
from lapeg.guard.guard import run as run_guard

SCALE = 10**36
YEAR = 365 * 24 * 3600


def _latest(pattern: str, results: Any, need: str | None = None) -> Any:
    """Newest report matching `pattern` (that has key `need`, if given)."""
    for f in sorted(results.glob(pattern), reverse=True):
        if need is None or need in json.loads(f.read_text()):
            return f
    raise FileNotFoundError(f"no {pattern} in {results}; run the Phase 2 experiment first")


def linear_cap_at(oracles: dict[str, Any], market: str, timestamp: int) -> int | None:
    """The linear-discount ceiling at `timestamp`, from the verified formula and its recorded parameters:
    1 - baseDiscountPerYear * timeLeft / 365 days (PendleSparkLinearDiscountOracle)."""

    def find(node: dict[str, Any]) -> dict[str, Any] | None:
        if str(node.get("verified_name", "")).startswith("PendleSparkLinearDiscountOracle"):
            return node
        for child in node.get("children", {}).values():
            if isinstance(child, dict) and "children" in child and (hit := find(child)):
                return hit
        return None

    node = find(oracles["markets"][market]["tree"])
    if node is None:
        return None
    v = node["values"]
    left = max(0, int(v["maturity"]) - timestamp)
    return (10**18 - int(v["baseDiscountPerYear"]) * left // YEAR) * 10**18  # 1e36 scale


@dataclass
class Recorded:
    """The fast tier's inputs, built from the latest Phase 2 run and its position book."""

    report_path: Path
    report: dict[str, Any]
    depth: pd.DataFrame  # executed sales (status "ok"), by size
    book: pd.DataFrame  # borrowers with debt
    model: MarketModel
    positions: list[Position]
    loan_dec: int
    max_sellable: int | None

    def to_pt(self, usd: float) -> int:
        return int(usd * 10**self.loan_dec * SCALE // self.model.oracle_price)


def coherent_depth(ok: pd.DataFrame) -> pd.DataFrame:
    """One depth curve from stages measured at different blocks. A later stage replaces earlier points inside
    its size range (it re-measured that range), and points where G falls below a smaller size's G are dropped
    (selling more never gets a better average price). Mixing blocks otherwise puts near-identical sizes at
    different prices, which makes the marginal price jump."""
    if "stage" not in ok:
        return ok.sort_values("amount_in")
    order = list(dict.fromkeys(ok.stage))  # stages in measurement order
    keep = []
    for i, st in enumerate(order):
        rows = ok[ok.stage == st]
        later = ok[ok.stage.isin(order[i + 1 :])]
        if len(later):
            lo, hi = later.amount_in.astype(int).min(), later.amount_in.astype(int).max()
            rows = rows[(rows.amount_in.astype(int) < lo) | (rows.amount_in.astype(int) > hi)]
        keep.append(rows)
    out = pd.concat(keep).sort_values("amount_in")
    return out[out.g >= out.g.cummax()]


def load_recorded(results: Path, market_id: str, market_name: str) -> Recorded:
    rep_path = _latest("phase2_liquidity_*.json", results, need="outputs")  # not the position-book report
    rep = json.loads(rep_path.read_text())
    block = rep["fork_block"]
    depth = pd.read_parquet(results / rep["outputs"][0])
    book = pd.read_parquet(results / rep["position_book"]["parquet"])
    oracles = json.loads((PROJECT_ROOT / "data" / "discovered" / "oracles.json").read_text(encoding="utf-8"))

    ok = coherent_depth(depth[depth.status == "ok"])
    failing = depth[depth.status != "ok"]
    max_sellable = int(failing.amount_in.min()) if len(failing) else None
    curve = DepthCurve([int(x) for x in ok.amount_in], [int(x) for x in ok.amount_out], max_sellable)
    price = int(round(rep["oracle_price"] * SCALE))
    debtors = book[book.borrow_shares_raw != "0"]
    loan_dec = round(math.log10(int(debtors.debt_raw.iloc[0]) / float(debtors.debt.iloc[0])))
    gas_raw = float(ok.gas_usd.median()) * 10**loan_dec if "gas_usd" in ok else 0.0
    markets = json.loads((PROJECT_ROOT / "data" / "discovered" / "markets.json").read_text())["markets"]
    lltv = int(next(m for m in markets if m["market_id"] == market_id)["lltv_wad"])
    # block timestamp: from the position-book report written for the same block (Phase 1, or Phase 2's own)
    book_reports = [
        json.loads(f.read_text())
        for pattern in ("phase1_position_book_*.json", "phase2_liquidity_*.json")
        for f in results.glob(pattern)
    ]
    same_block = [r for r in book_reports if r.get("fork_block") == block and r.get("block_timestamp")]
    block_ts = same_block[0]["block_timestamp"] if same_block else None
    cap = linear_cap_at(oracles, market_name, int(block_ts)) if block_ts else None
    model = MarketModel(lltv, price, curve, cap, gas_raw)
    positions = [Position(r.borrower, int(r.collateral_raw), int(r.debt_raw)) for r in debtors.itertuples()]
    return Recorded(rep_path, rep, ok, debtors, model, positions, loan_dec, max_sellable)


def run_cascade_and_guard(cfg: ExperimentConfig, write_results: bool = True) -> dict[str, Any]:
    t0 = time.monotonic()
    p = cfg.params
    results = cfg.path(cfg.results_dir)
    rec = load_recorded(results, cfg.market.market_id, cfg.market.name)
    rep_path, rep, ok, model, positions = rec.report_path, rec.report, rec.depth, rec.model, rec.positions
    block, max_sellable, gas_raw, cap = rep["fork_block"], rec.max_sellable, model.gas_cost, model.linear_cap

    instant = Params(twap_blocks=1, latency_blocks=0, horizon_blocks=1)
    feedback_instant = Params(
        twap_blocks=1,
        latency_blocks=int(p.get("latency_blocks", 1)),
        horizon_blocks=int(p.get("horizon_blocks", 300)),
    )
    feedback_twap = Params(
        twap_blocks=int(p.get("twap_blocks", 75)),
        latency_blocks=int(p.get("latency_blocks", 1)),
        horizon_blocks=int(p.get("horizon_blocks", 300)),
        replenish_per_block=float(p.get("replenish_per_block", 0.0)),
    )

    # 1. consistency with Morpho's own liquidatable sets (Phase 2 override)
    consistency = []
    for row in rep["shocks"]:
        r = simulate(positions, model, instant, oracle_shock=row["shock"])
        n_fast = int(r.timeline[0]["n_liquidatable"])
        consistency.append(
            {
                "shock": row["shock"],
                "n_liquidatable_morpho": row["n_liquidatable"],
                "n_liquidatable_fast": n_fast,
                "match": n_fast == row["n_liquidatable"],
            }
        )

    # 2. oracle-shock sweep with feedback
    sweep = []
    for d in [float(x) for x in p.get("oracle_shocks", [0.01, 0.02, 0.03, 0.05, 0.1])]:
        for label, prm in (("instant", feedback_instant), ("twap", feedback_twap)):
            s = simulate(positions, model, prm, oracle_shock=d).summary()
            sweep.append({"kind": "oracle_shock", "size": d, "oracle_mode": label, **s})

    # 3. sale-shock sweep and C*
    to_pt = rec.to_pt
    c_star = None
    for usd in [float(x) for x in p.get("sale_shocks_usd", [1e6, 2e6, 5e6, 1e7, 2e7])]:
        q = to_pt(usd)
        s = simulate(positions, model, feedback_twap, shock_pt_sold=q).summary()
        sweep.append({"kind": "sale_shock", "size": usd, "oracle_mode": "twap", **s})
        if c_star is None and (s["stuck_debt"] or s["bad_debt"]):
            c_star = {"usd": usd, "pt": q, **s}

    # 4. guard v0 on synthetic pending sales
    pending = [
        PendingTx(f"sell_{u / 1e6:g}M", "sell_pt", pt_sold=to_pt(u), note=f"sell ${u / 1e6:.1f}M of PT")
        for u in [float(x) for x in p.get("guard_sales_usd", [5e5, 5e6, 2e7])]
    ]
    log = run_guard(
        positions, model, [pending], Policy(max_delays=int(p.get("max_delays", 2)), params=feedback_twap)
    )
    decisions = log.rows()

    report: dict[str, Any] = {
        "experiment": cfg.name,
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ"),
        "inputs": {
            "phase2_report": rep_path.name,
            "fork_block": block,
            "n_borrowers": len(positions),
            "depth_points": len(ok),
            "max_sellable_pt": max_sellable,
            "gas_raw": gas_raw,
            "linear_cap": cap / SCALE if cap else None,
        },
        "consistency_with_morpho": consistency,
        "sweep": sweep,
        "c_star": c_star,
        "guard_decisions": decisions,
        "checks": {
            "fast_tier_matches_morpho_liquidatable_sets": all(c["match"] for c in consistency),
            "guard_passes_small_sale": any(d["action"] == "PASS" for d in decisions),
        },
        "seconds": round(time.monotonic() - t0, 2),
    }
    report["passed"] = all(report["checks"].values())
    if write_results:
        results.mkdir(parents=True, exist_ok=True)
        stem = f"{cfg.name}_{report['run_utc']}"
        (results / f"{stem}.json").write_text(json.dumps(report, indent=1, default=str))
        pd.DataFrame(decisions).to_parquet(results / f"{stem}_guard_log.parquet", index=False)
    return report
