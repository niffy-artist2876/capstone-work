"""Interactive playground session: a user acts on a live mainnet fork and LAPEG answers.

Every action calls the same functions the experiments use (oracle override, executed sales, Morpho's own
`liquidate`, HealthReader, the fast tier and guard v0), so what the user sees is what is tested. Every
state-changing action pushes a fork snapshot first, so it can be undone. Nothing leaves the local fork:
accounts are impersonated, and no keys or real funds are involved.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from web3 import Web3

from lapeg import PROJECT_ROOT
from lapeg.cascade.fast import MarketModel, Params, Position
from lapeg.chain.abi import load_abi
from lapeg.chain.health_reader import read_health
from lapeg.chain.morpho import MorphoBlue, load_morpho_address
from lapeg.config import ANVIL_RETRY_ARGS, PUBLIC_ARCHIVE_RPC
from lapeg.fork.anvil import AnvilFork
from lapeg.guard.experiment import linear_cap_at, load_recorded
from lapeg.guard.guard import PendingTx, assess
from lapeg.liquidity.capacity import gas_cost_usd, gas_price
from lapeg.liquidity.execute import execute_route, liquidate_position, sell_pt_for_sy
from lapeg.liquidity.pendle_api import quote
from lapeg.market import morpho_math as mm
from lapeg.oracle.override import override_ojo_feed

D = PROJECT_ROOT / "data" / "discovered"
R = PROJECT_ROOT / "data" / "results"
SCALE = mm.ORACLE_PRICE_SCALE


@dataclass
class Playground:
    market_name: str = "PT-reUSD-10DEC2026"
    fork: AnvilFork | None = None
    block: int | None = None
    history: list[tuple[str, str]] = field(default_factory=list)  # (snapshot id, action label)
    log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.market = next(
            m
            for m in json.loads((D / "markets.json").read_text())["markets"]
            if m["name"] == self.market_name
        )
        tree = json.loads((D / "oracles.json").read_text(encoding="utf-8"))["markets"][self.market_name][
            "tree"
        ]
        feed = tree["children"]["BASE_FEED_1"]
        self.oracles = json.loads((D / "oracles.json").read_text(encoding="utf-8"))
        self.ojo = feed["address"]
        self.pendle_market = feed["children"]["FEED_1"]["children"]["market"]["address"]
        self.router = json.loads((D / "pendle_router.json").read_text())["router"]
        self.morpho_address = load_morpho_address(D / "morpho_blue.json")
        self.user = Web3.to_checksum_address(Web3.keccak(text="lapeg-playground-user")[-20:])
        self.url = os.environ.get("ARCHIVE_RPC_URL") or PUBLIC_ARCHIVE_RPC

    # ---------- lifecycle ----------
    @property
    def running(self) -> bool:
        return self.fork is not None and self.fork.running

    def start(self, block: int | None = None) -> int:
        self.stop()
        if block is None:
            block = int(Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": 60})).eth.block_number)
        self.fork = AnvilFork(self.url, block, extra_args=ANVIL_RETRY_ARGS).start()
        assert self.fork.w3 is not None
        self.w3 = self.fork.w3
        self.block = block
        self.latest_at_start = block
        self.morpho = MorphoBlue(self.w3, self.morpho_address)
        self.params = self.morpho.market_params(self.market["market_id"])
        self.price0 = self.morpho.oracle_price(self.market["oracle"])
        mk = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.pendle_market), abi=load_abi("PendleMarket")
        )
        self.sy = mk.functions.readTokens().call()[0]
        self.sy_rate = int(
            self.w3.eth.contract(address=self.sy, abi=load_abi("PendleSY")).functions.exchangeRate().call()
        )
        self.gas = gas_price(self.w3, block)
        self.accounts = self._accounts()
        self.history, self.log = [], []
        self._note("start", f"Started a live copy at block {block:,}")
        return block

    def stop(self) -> None:
        if self.fork is not None:
            self.fork.stop()
        self.fork = None

    def _accounts(self) -> list[str]:
        """Every borrower LAPEG knows about: all recorded position books plus the Aug 25 liquidations.
        Morpho reports zero for accounts with no position at the chosen block."""
        names: set[str] = set()
        for f in R.glob("position_book_*.parquet"):
            names |= set(pd.read_parquet(f, columns=["borrower"]).borrower)
        ev = D / "aug25_event.json"
        if ev.exists():
            names |= {
                e["borrower"]
                for e in json.loads(ev.read_text())["events"]
                if e["market_id"] == self.market["market_id"]
            }
        return sorted(Web3.to_checksum_address(a) for a in names)

    def _note(self, action: str, detail: str, **extra: Any) -> None:
        self.log.append({"action": action, "detail": detail, "block": self.w3.eth.block_number, **extra})

    def _push(self, label: str) -> None:
        assert self.fork is not None
        self.history.append((self.fork.snapshot(), label))

    def undo(self) -> str | None:
        if not self.history or self.fork is None:
            return None
        snap, label = self.history.pop()
        self.fork.revert(snap)
        self._note("undo", f"Undid: {label}")
        return label

    # ---------- reads ----------
    def oracle_price(self) -> float:
        return self.morpho.oracle_price(self.market["oracle"]) / SCALE

    def positions(self) -> pd.DataFrame:
        snap = read_health(self.w3, self.morpho_address, self.market["market_id"], self.accounts, accrue=True)
        rows = [
            {
                "borrower": a,
                "collateral_pt": s.collateral / 1e6,
                "debt_usdc": s.borrowed / 1e6,
                "health_factor": mm.health_factor(s.borrowed, s.max_borrow),
                "collateral_raw": s.collateral,
                "debt_raw": s.borrowed,
                "borrow_shares": s.borrow_shares,
            }
            for a, s in snap.accounts.items()
            if s.borrow_shares > 0
        ]
        df = pd.DataFrame(rows)
        if len(df):
            df["liquidatable"] = df.health_factor < 1
            df = df.sort_values("health_factor").reset_index(drop=True)
        return df

    def market_spot(self) -> float | None:
        """Marginal Pendle price for a tiny sale (100 PT), in USDC per PT at the SY's own exchange rate."""
        assert self.fork is not None
        ex = sell_pt_for_sy(
            self.fork,
            self.router,
            self.pendle_market,
            self.market["collateral_token"],
            self.sy,
            100 * 10**6,
            self.user,
        )
        return ex.amount_out * self.sy_rate / 1e18 / (100 * 10**6) if ex.status == "ok" else None

    # ---------- actions ----------
    def shock_oracle(self, delta: float) -> float:
        assert self.fork is not None
        self._push(f"lower the price feed by {100 * delta:.1f}%")
        override_ojo_feed(self.fork, self.ojo, int(self.price0 // 10**18 * (1 - delta)))
        p = self.oracle_price()
        self._note("shock", f"Price feed set to ${p:.4f}, {100 * delta:.1f}% below the start")
        return p

    def advance(self, minutes: int) -> None:
        assert self.fork is not None
        self._push(f"skip ahead {minutes} minutes")
        self.fork.increase_time(minutes * 60)
        self.fork.mine(1)
        self._note("advance", f"Skipped ahead {minutes} minutes; price feed now ${self.oracle_price():.4f}")

    def sell(self, amount_pt: float, via: str = "pendle", push: bool = True) -> dict[str, Any]:
        """Sell PT and keep the result on the fork. via: 'pendle' (Pendle pool only, any block) or
        'aggregator' (live Convert API route; only meaningful when the fork is at the latest block)."""
        assert self.fork is not None
        amt = int(amount_pt * 10**6)
        oracle = self.morpho.oracle_price(self.market["oracle"])
        if push:
            self._push(f"sell {amount_pt:,.0f} PT")
        if via == "aggregator":
            q = quote(self.market["collateral_token"], self.market["loan_token"], amt, self.user)
            if q.refusal or not q.routes:
                if push:
                    self.undo()
                return {"status": "refused", "reason": q.refusal or "no route"}
            ex = execute_route(
                self.fork,
                q.routes[0],
                self.market["collateral_token"],
                self.market["loan_token"],
                amt,
                keep=True,
            )
            value_out = ex.amount_out
        else:
            ex = sell_pt_for_sy(
                self.fork,
                self.router,
                self.pendle_market,
                self.market["collateral_token"],
                self.sy,
                amt,
                self.user,
                keep=True,
            )
            value_out = int(ex.amount_out * self.sy_rate / 10**18)
        if ex.status != "ok":
            if push:
                self.undo()
            return {"status": "reverted", "reason": ex.error}
        g = 1 - value_out * SCALE / (amt * oracle)
        out = {
            "status": "ok",
            "via": via,
            "pt_sold": amount_pt,
            "value_out_usdc": value_out / 1e6,
            "g": g,
            "gas_usd": gas_cost_usd(ex.gas_used, self.gas),
        }
        side = "below" if g >= 0 else "above"
        detail = f"Sold {amount_pt:,.0f} PT for ${value_out / 1e6:,.0f}, {100 * abs(g):.2f}% {side} the feed"
        self._note("sell", detail, **out)
        return out

    def liquidate(self, borrower: str, sell_via: str = "pendle") -> dict[str, Any]:
        """Liquidate the borrower with Morpho's own liquidate, then sell the seized PT. Repays the whole debt;
        an underwater position (whole debt x LIF worth more than its collateral) has all its collateral seized
        instead, and Morpho writes off the debt left over as bad debt."""
        assert self.fork is not None
        borrower = Web3.to_checksum_address(borrower)
        snap = read_health(self.w3, self.morpho_address, self.market["market_id"], [borrower], accrue=True)
        acct = snap.accounts[borrower]
        if acct.borrowed <= acct.max_borrow:
            return {"status": "healthy", "reason": "Morpho would refuse: position is healthy"}
        # collateral Morpho would seize for the whole debt, with its own rounding (Morpho.liquidate)
        price = self.morpho.oracle_price(self.market["oracle"])
        lif = mm.liquidation_incentive_factor(self.params.lltv)
        underwater = mm.mul_div_down(mm.w_mul_down(acct.borrowed, lif), SCALE, price) > acct.collateral
        self._push(f"liquidate {borrower[:6]}…{borrower[-4:]}")
        res = liquidate_position(
            self.fork,
            self.morpho,
            self.params,
            borrower,
            self.user,
            0 if underwater else acct.borrow_shares,
            acct.borrowed + 10**6,
            keep=True,
            seized_assets=acct.collateral if underwater else 0,
        )
        if res.status != "ok":
            self.undo()
            return {"status": "reverted", "reason": res.error}
        after = read_health(self.w3, self.morpho_address, self.market["market_id"], [borrower], accrue=True)
        left = after.accounts[borrower]
        # with no collateral left, Morpho writes the remaining debt off (bad debt), a loss to lenders
        bad_debt = max(0, acct.borrowed - res.repaid) if left.collateral == 0 and left.borrowed == 0 else 0
        sale = self.sell(res.seized / 1e6, via=sell_via, push=False)  # same undo step as the liquidation
        proceeds = sale.get("value_out_usdc", 0.0)
        gas = gas_cost_usd(res.gas_used, self.gas) + sale.get("gas_usd", 0.0)
        out = {
            "status": "ok",
            "borrower": borrower,
            "seized_pt": res.seized / 1e6,
            "repaid_usdc": res.repaid / 1e6,
            "sale": sale,
            "profit_usdc": proceeds - res.repaid / 1e6 - gas,
            "gas_usd": gas,
            "underwater": underwater,
            "bad_debt_usdc": bad_debt / 1e6,
        }
        loss = f"; lenders lose ${bad_debt / 1e6:,.0f}" if bad_debt else ""
        self._note(
            "liquidate",
            f"Liquidated {borrower[:6]}…{borrower[-4:]}: repaid ${res.repaid / 1e6:,.0f}, "
            f"took {res.seized / 1e6:,.0f} PT, profit ${out['profit_usdc']:,.0f}{loss}",
            **{k: v for k, v in out.items() if k != "sale"},
        )
        return out

    # ---------- guard ----------
    def guard_check(self, sale_usd: float) -> dict[str, Any]:
        """Ask guard v0 about a pending sale of PT worth `sale_usd`, against the fork's current state."""
        try:
            rec = load_recorded(R, self.market["market_id"], self.market_name)
        except FileNotFoundError:
            return {"status": "unavailable", "reason": "no Phase 2 depth curve recorded yet"}
        rep = rec.report
        oracle = self.morpho.oracle_price(self.market["oracle"])
        ts = int(self.w3.eth.get_block("latest")["timestamp"])
        model = MarketModel(
            self.params.lltv,
            oracle,
            rec.model.depth,
            linear_cap_at(self.oracles, self.market_name, ts),
            rec.model.gas_cost,
        )
        pos = self.positions()
        positions = [Position(r.borrower, int(r.collateral_raw), int(r.debt_raw)) for r in pos.itertuples()]
        amt = int(sale_usd * 1e6 * SCALE // oracle)
        tx = PendingTx("user_sale", "sell_pt", pt_sold=amt, note=f"sell ${sale_usd:,.0f} of PT")
        risk, without, with_tx = assess(positions, model, tx, Params())
        action = "PASS" if risk <= 0 else "DELAY"
        out = {
            "status": "ok",
            "action": action,
            "risk_usdc": risk / 1e6,
            "without_tx": without.summary(),
            "with_tx": with_tx.summary(),
            "depth_block": rep["fork_block"],
            "fork_block": self.block,
            "with_tx_liquidations": [vars(x) for x in with_tx.liquidations][:50],
        }
        self._note(
            "guard",
            f"pending sale of ${sale_usd:,.0f}: {action} (adds {risk / 1e6:,.0f} USDC of unclearable debt)",
        )
        return out
