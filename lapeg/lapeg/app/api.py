"""HTTP API behind the LAPEG web app (lapeg/web).

Two kinds of endpoints:
  - Recorded (`/api/overview`, `/api/whatif`, `/api/guard`): the fast tier and guard v0 on the latest Phase 2
    measurement. No network, milliseconds per call.
  - Live lab (`/api/lab/...`): the Playground session on a local anvil fork. Slow (seconds to minutes per
    call); one fork at a time, guarded by a lock.

Run: uvicorn lapeg.app.api:app --port 8000  (serves the built web app at / when lapeg/web/dist exists)
"""

from __future__ import annotations

import json
import math
import threading
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lapeg import PROJECT_ROOT
from lapeg.app.session import Playground
from lapeg.cascade.fast import Params, Result, simulate
from lapeg.config import load_dotenv
from lapeg.guard.experiment import Recorded, load_recorded
from lapeg.guard.guard import PendingTx, assess
from lapeg.metrics import illiquidity as il

MARKET_NAME = "PT-reUSD-10DEC2026"
D = PROJECT_ROOT / "data" / "discovered"
R = PROJECT_ROOT / "data" / "results"
WEB = PROJECT_ROOT / "web" / "dist"
BLOCK_S = 12

load_dotenv()
app = FastAPI(title="LAPEG")


@lru_cache(maxsize=1)
def recorded() -> Recorded:
    markets = json.loads((D / "markets.json").read_text())["markets"]
    market = next(m for m in markets if m["name"] == MARKET_NAME)
    return load_recorded(R, market["market_id"], MARKET_NAME)


def _usd(raw: float, rec: Recorded) -> float:
    return raw / 10**rec.loan_dec


def _pt_usd(raw_pt: float, rec: Recorded) -> float:
    return raw_pt * rec.model.oracle_price / il.ORACLE_PRICE_SCALE / 10**rec.loan_dec


# ---------- recorded ----------
@app.get("/api/overview")
def overview() -> dict[str, Any]:
    rec = recorded()
    rep = rec.report
    oracle0 = rec.model.oracle_price / il.ORACLE_PRICE_SCALE
    q = rep["q_max"]["bonus_gas"]
    market_id = rep["market"]["id"]
    events = json.loads((D / "aug25_event.json").read_text())["events"]
    aug = [e for e in events if e["market_id"] == market_id]
    times = [e["timestamp"] for e in aug] or [0]
    e2e = rep.get("end_to_end") or {}
    return {
        "market": {
            "name": MARKET_NAME,
            "maturity": "2026-12-10",
            "borrowers": len(rec.positions),
            "debt_usd": _usd(sum(p.debt for p in rec.positions), rec),
            "collateral_usd": _pt_usd(sum(p.collateral for p in rec.positions), rec),
            "price": oracle0,
            "max_ltv": rec.model.lltv / 1e18,
            "bonus": rep["lif"] - 1,
            "break_even_loss": rep["bonus_threshold"],
        },
        "measured": {
            "block": rep["fork_block"],
            "run_utc": rep["run_utc"],
            "gas_usd": rec.model.gas_cost / 10**rec.loan_dec,
        },
        "depth": [
            {"usd": float(r.size_usd_at_oracle), "loss": float(r.g), "stage": r.stage, "block": int(r.block)}
            for r in rec.depth.itertuples()
        ],
        "capacity": {"low_usd": q["last_ok"]["usd_at_oracle"], "high_usd": q["first_fail"]["usd_at_oracle"]},
        "shocks": [
            {
                "drop": s["shock"],
                "loans": s["n_liquidatable"],
                "to_sell_usd": _pt_usd(s["q_seizable_pt"], rec),
                "backlog": s["backlog_ratio"],
            }
            for s in rep["shocks"]
        ],
        "break_point": {
            "drop": rep["delta_star"]["delta"],
            "loans": rep["delta_star"]["n_liquidatable"],
            "to_sell_usd": _pt_usd(rep["delta_star"]["q_seizable_pt"], rec),
        }
        if rep.get("delta_star")
        else None,
        "end_to_end": {
            "block": e2e.get("block"),
            "seized_usd": _pt_usd(e2e["seized_pt"], rec),
            "repaid_usd": e2e["repaid_usdc"] / 10**rec.loan_dec,
            "sold_usd": e2e["sale_usdc"] / 10**rec.loan_dec,
            "gas_usd": e2e["gas_usd"],
        }
        if e2e.get("liquidate_status") == 1
        else None,
        "aug25": {
            "liquidations": len(aug),
            "repaid_usd": sum(int(e["repaid_assets"]) for e in aug) / 10**rec.loan_dec,
            "bad_debt_usd": sum(int(e["bad_debt_assets"]) for e in aug) / 10**rec.loan_dec,
            "minutes": (max(times) - min(times)) / 60,
            "liquidators": len({e["liquidator"] for e in aug}),
        },
    }


class Scenario(BaseModel):
    drop: float = Field(0.0, ge=0, le=0.5, description="oracle price drop, fraction")
    sale_usd: float = Field(0.0, ge=0, le=1e8, description="PT sold at once, USD at the oracle price")
    refill: float = Field(0.0, ge=0, le=0.2, description="share of the sold amount bought back per block")
    lag: bool = Field(True, description="oracle follows a 15-minute average (True) or reacts at once")


def _params(s: Scenario) -> Params:
    return Params(twap_blocks=75 if s.lag else 1, replenish_per_block=s.refill)


def _loans(res: Result, rec: Recorded) -> list[dict[str, Any]]:
    """Every loan, riskiest first, with what happened to it."""
    rescued = {x.account for x in res.liquidations}
    rows = []
    for r in rec.book.sort_values("health_factor").itertuples():
        a = r.borrower
        status = (
            "loss"
            if a in res.bad_debt_by_account
            else "stuck"
            if a in res.stuck
            else "rescued"
            if a in rescued
            else "safe"
        )
        rows.append({"debt_usd": float(r.debt), "health": float(r.health_factor), "status": status})
    return rows


def _outcome(res: Result, rec: Recorded) -> dict[str, Any]:
    oracle0 = rec.model.oracle_price / il.ORACLE_PRICE_SCALE
    loans = _loans(res, rec)
    return {
        "summary": {
            "rescued": sum(x["status"] == "rescued" for x in loans),
            "stuck": sum(x["status"] == "stuck" for x in loans),
            "loss": sum(x["status"] == "loss" for x in loans),
            "safe": sum(x["status"] == "safe" for x in loans),
            "repaid_usd": _usd(res.liquidated_repaid, rec),
            "stuck_debt_usd": _usd(res.stuck_debt, rec),
            "bad_debt_usd": _usd(res.bad_debt, rec),
            "minutes": res.blocks * BLOCK_S / 60,
            "settled": res.fixed_point,
        },
        "loans": loans,
        "timeline": [
            {
                "minute": t["block"] * BLOCK_S / 60,
                "price_drop": 1 - t["oracle"] / oracle0,
                "sold_usd": _pt_usd(t["sold_pt"], rec),
                "rescued": t["n_liquidated"],
            }
            for t in res.timeline
        ],
    }


@app.post("/api/whatif")
def whatif(s: Scenario) -> dict[str, Any]:
    rec = recorded()
    sold = rec.to_pt(s.sale_usd)
    res = simulate(rec.positions, rec.model, _params(s), shock_pt_sold=sold, oracle_shock=s.drop)
    return {**_outcome(res, rec), "refill_half_life_min": _half_life(s.refill)}


def _half_life(refill: float) -> float | None:
    return None if refill <= 0 else math.log(0.5) / math.log(1 - refill) * BLOCK_S / 60


class GuardRequest(BaseModel):
    sale_usd: float = Field(..., gt=0, le=1e8)
    refill: float = Field(0.0, ge=0, le=0.2)


@app.post("/api/guard")
def guard(g: GuardRequest) -> dict[str, Any]:
    rec = recorded()
    tx = PendingTx("pending_sale", "sell_pt", pt_sold=rec.to_pt(g.sale_usd), note=f"sell ${g.sale_usd:,.0f}")
    risk, without, with_tx = assess(rec.positions, rec.model, tx, Params(replenish_per_block=g.refill))
    return {
        "action": "PASS" if risk <= 0 else "DELAY",
        "added_risk_usd": _usd(risk, rec),
        "without": _outcome(without, rec),
        "with": _outcome(with_tx, rec),
    }


# ---------- live lab ----------
_lab = Playground(MARKET_NAME)
_lock = threading.Lock()


def _need_lab() -> None:
    if not _lab.running:
        raise HTTPException(409, "Start the live copy first.")


def _lab_state() -> dict[str, Any]:
    if not _lab.running:
        return {"running": False}
    pos = _lab.positions()
    return {
        "running": True,
        "block": _lab.block,
        "price": _lab.oracle_price(),
        "start_price": _lab.price0 / il.ORACLE_PRICE_SCALE,
        "can_undo": bool(_lab.history),
        "history": [label for _, label in _lab.history],
        "log": _lab.log[-30:],
        "loans": [
            {
                "borrower": r.borrower,
                "collateral_pt": float(r.collateral_pt),
                "debt_usd": float(r.debt_usdc),
                "health": float(r.health_factor) if math.isfinite(r.health_factor) else None,
                "liquidatable": bool(r.liquidatable),
            }
            for r in pos.itertuples()
        ],
    }


@app.get("/api/lab")
def lab_state() -> dict[str, Any]:
    with _lock:
        return _lab_state()


class LabStart(BaseModel):
    block: int | None = None


@app.post("/api/lab/start")
def lab_start(b: LabStart) -> dict[str, Any]:
    with _lock:
        try:
            _lab.start(b.block)
        except Exception as e:  # upstream RPC, anvil missing, ...
            _lab.stop()
            raise HTTPException(502, f"Could not start the live copy: {e}") from e
        return _lab_state()


@app.post("/api/lab/stop")
def lab_stop() -> dict[str, Any]:
    with _lock:
        _lab.stop()
        return _lab_state()


class LabDrop(BaseModel):
    drop: float = Field(..., ge=0, le=0.5)


@app.post("/api/lab/drop")
def lab_drop(b: LabDrop) -> dict[str, Any]:
    with _lock:
        _need_lab()
        _lab.shock_oracle(b.drop)
        return _lab_state()


class LabWait(BaseModel):
    minutes: int = Field(..., ge=1, le=24 * 60)


@app.post("/api/lab/wait")
def lab_wait(b: LabWait) -> dict[str, Any]:
    with _lock:
        _need_lab()
        _lab.advance(b.minutes)
        return _lab_state()


class LabSell(BaseModel):
    amount_pt: float = Field(..., gt=0)
    via: str = Field("pendle", pattern="^(pendle|aggregator)$")


@app.post("/api/lab/sell")
def lab_sell(b: LabSell) -> dict[str, Any]:
    with _lock:
        _need_lab()
        result = _lab.sell(b.amount_pt, via=b.via)
        return {"result": result, "state": _lab_state()}


class LabLiquidate(BaseModel):
    borrower: str


@app.post("/api/lab/liquidate")
def lab_liquidate(b: LabLiquidate) -> dict[str, Any]:
    with _lock:
        _need_lab()
        result = _lab.liquidate(b.borrower)
        return {"result": result, "state": _lab_state()}


@app.post("/api/lab/undo")
def lab_undo() -> dict[str, Any]:
    with _lock:
        _need_lab()
        _lab.undo()
        return _lab_state()


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
