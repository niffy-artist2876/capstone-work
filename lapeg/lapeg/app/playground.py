"""LAPEG playground: act on a live mainnet fork of the PT-reUSD market and see what LAPEG predicts.

Run from the lapeg/ folder:   streamlit run lapeg/app/playground.py
Everything happens on a local anvil fork with impersonated accounts: no wallet, keys or real funds.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from lapeg import PROJECT_ROOT
from lapeg.app.session import Playground

D = PROJECT_ROOT / "data" / "discovered"
R = PROJECT_ROOT / "data" / "results"

st.set_page_config(page_title="LAPEG playground", layout="wide")


# ---------- recorded data ----------
@st.cache_data
def aug25() -> dict[str, Any] | None:
    f = D / "aug25_event.json"
    return json.loads(f.read_text()) if f.exists() else None


@st.cache_data
def latest(pattern: str, need: str | None = None) -> dict[str, Any] | None:
    """Newest report matching `pattern` (that has key `need`, if given)."""
    for f in sorted(R.glob(pattern), reverse=True):
        rep = json.loads(f.read_text())
        if need is None or need in rep:
            return rep
    return None


def presets() -> dict[str, int | None]:
    out: dict[str, int | None] = {"Latest block (live; aggregator routes available)": None}
    p1 = latest("phase1_position_book_*.json")
    if p1:
        out[f"Recorded block {p1['fork_block']} (Phase 1 position book)"] = int(p1["fork_block"])
    ev = aug25()
    if ev:
        mid = "0x1e9d614631a7df0ec07fb05b2c8cb2491575fd1a63a33bf187a6afb295a4fc64"
        first = min(e["block"] for e in ev["events"] if e["market_id"] == mid)
        out[f"Aug 25, 2026: block {first - 1}, just before the first liquidation"] = first - 1
    return out


def pg() -> Playground:
    if "pg" not in st.session_state:
        st.session_state.pg = Playground()
    return st.session_state.pg


# ---------- sidebar: fork control ----------
play = pg()
with st.sidebar:
    st.header("Fork")
    choice = st.radio("Start from", list(presets()) + ["Custom block"], index=0)
    custom = st.number_input("Custom block", min_value=0, value=0, step=1, disabled=choice != "Custom block")
    block = int(custom) if choice == "Custom block" else presets()[choice]
    c1, c2 = st.columns(2)
    if c1.button("Start fork", type="primary"):
        with st.spinner("Forking mainnet (the first reads can take a minute on the free endpoint)..."):
            play.start(block)
    if c2.button("Stop", disabled=not play.running):
        play.stop()
    st.caption("Local anvil fork. Accounts are impersonated; nothing reaches the real chain.")
    if play.running:
        st.success(f"Running at block {play.block}")
        st.subheader("History")
        if st.button("Undo last action", disabled=not play.history):
            st.toast(f"Undid: {play.undo()}")
        for _, label in reversed(play.history[-10:]):
            st.write(f"- {label}")

st.title("LAPEG playground")
st.caption("PT-reUSD-10DEC2026 / USDC on Morpho Blue. Act on real chain state; LAPEG shows what follows.")

tabs = st.tabs(["Positions", "Shock the oracle", "Trade and time", "Liquidate", "Guard", "Recorded runs"])

# ---------- recorded runs (no fork needed) ----------
with tabs[5]:
    rep = latest("phase2_liquidity_*.json", need="outputs")  # skips the position-book report of the same run
    if rep:
        st.subheader(f"Depth curve at block {rep['fork_block']}")
        depth = pd.read_parquet(R / rep["outputs"][0])
        ok = depth[depth.status == "ok"].copy()
        ok["G (%)"] = 100 * ok["g"]
        fig = px.line(
            ok.sort_values("size_usd_at_oracle"),
            x="size_usd_at_oracle",
            y="G (%)",
            markers=True,
            log_x=True,
            labels={"size_usd_at_oracle": "sale size (USD at oracle value)"},
        )
        fig.add_hline(y=100 * rep["bonus_threshold"], line_dash="dash", annotation_text="profit threshold")
        st.plotly_chart(fig, use_container_width=True)
        st.json(
            {k: rep[k] for k in ("q_max", "delta_star", "end_to_end", "checks") if k in rep}, expanded=False
        )
        if rep.get("shocks"):
            st.subheader("Oracle shocks (decided by Morpho's own code)")
            st.dataframe(pd.DataFrame(rep["shocks"]), use_container_width=True)
    else:
        st.info("No Phase 2 run recorded yet.")
    ev = aug25()
    if ev:
        st.subheader("Aug 25, 2026: the liquidations in this market")
        mid = "0x1e9d614631a7df0ec07fb05b2c8cb2491575fd1a63a33bf187a6afb295a4fc64"
        e = pd.DataFrame([x for x in ev["events"] if x["market_id"] == mid])
        e["repaid (USDC M)"] = e.repaid_assets.astype(float) / 1e12
        e["liquidator"] = e.liquidator.str[:10]
        st.plotly_chart(
            px.scatter(
                e, x="block", y="repaid (USDC M)", size="repaid (USDC M)", color="liquidator", size_max=40
            ),
            use_container_width=True,
        )
    g = latest("phase45_cascade_guard_*.json")
    if g:
        st.subheader("Guard v0 decisions on recorded data")
        st.dataframe(
            pd.DataFrame(g["guard_decisions"])[["tx_id", "block", "action", "reason", "risk"]],
            use_container_width=True,
        )

if not play.running:
    for t in tabs[:5]:
        with t:
            st.info("Start a fork in the sidebar to interact.")
    st.stop()

# ---------- header ----------
pos = play.positions()
m1, m2, m3, m4 = st.columns(4)
m1.metric(
    "Oracle price (USDC per PT)",
    f"{play.oracle_price():.6f}",
    f"{100 * (play.oracle_price() / (play.price0 / 1e36) - 1):+.2f}% vs start",
)
m2.metric("Borrowers", len(pos))
m3.metric("Liquidatable now", int(pos.liquidatable.sum()) if len(pos) else 0)
m4.metric("Total debt (USDC)", f"{pos.debt_usdc.sum():,.0f}" if len(pos) else "0")

with tabs[0]:
    st.write(
        "Every borrower in the market, with health factors computed by Morpho's own code at the fork's "
        "current state. Below 1 means anyone may liquidate the position."
    )
    st.plotly_chart(
        px.histogram(
            pos, x="health_factor", nbins=60, range_x=[0.8, 2.5], title="Health factor distribution"
        ),
        use_container_width=True,
    )
    st.dataframe(
        pos[["borrower", "collateral_pt", "debt_usdc", "health_factor", "liquidatable"]],
        use_container_width=True,
        height=400,
    )

with tabs[1]:
    st.write(
        "Force the Morpho oracle down, independently of the market (a feed failure, a depeg, or a fast "
        "market move once the TWAP catches up). Morpho's own code then decides who can be liquidated."
    )
    delta = st.slider("Oracle drop vs the start of the fork (%)", 0.0, 20.0, 2.0, 0.5) / 100
    if st.button("Apply oracle shock", type="primary"):
        st.toast(f"Oracle now {play.shock_oracle(delta):.6f}")
        st.rerun()
    if len(pos):
        liq = pos[pos.liquidatable]
        st.metric("Collateral now liquidatable (PT)", f"{liq.collateral_pt.sum():,.0f}")
        st.dataframe(
            liq[["borrower", "collateral_pt", "debt_usdc", "health_factor"]], use_container_width=True
        )

with tabs[2]:
    st.write(
        "Sell PT into the market and keep the result on the fork. The oracle follows a 15-minute TWAP of "
        "the Pendle market, so it moves only as time passes: advance time to see it catch up."
    )
    c1, c2 = st.columns(2)
    amount = c1.number_input("PT to sell", min_value=1_000.0, value=1_000_000.0, step=100_000.0)
    via = c2.radio(
        "Route",
        ["pendle", "aggregator"],
        help="pendle: the Pendle pool only (any block). aggregator: Pendle's live best route across "
        "venues (only meaningful at the latest block).",
    )
    if st.button("Sell", type="primary"):
        st.json(play.sell(amount, via))
    minutes = st.slider("Advance time (minutes)", 1, 60, 15)
    if st.button("Advance time"):
        play.advance(minutes)
        st.rerun()
    spot = play.market_spot()
    st.metric("Pendle marginal price (USDC per PT, tiny sale)", f"{spot:.6f}" if spot else "n/a")

with tabs[3]:
    st.write(
        "Liquidate a position yourself with Morpho's real `liquidate`, then sell the seized PT. Profit = "
        "sale proceeds - debt repaid - gas. It is positive only while slippage stays below the bonus."
    )
    liq = pos[pos.liquidatable] if len(pos) else pos
    if len(liq) == 0:
        st.info("No position is liquidatable. Shock the oracle or sell PT and advance time first.")
    else:
        who = st.selectbox(
            "Borrower",
            liq.borrower,
            format_func=lambda a: f"{a}  (HF {float(liq.set_index('borrower').loc[a, 'health_factor']):.4f})",
        )
        how = st.radio("Sell the seized PT via", ["pendle", "aggregator"], horizontal=True)
        if st.button("Liquidate and sell", type="primary"):
            st.json(play.liquidate(who, how))

with tabs[4]:
    st.write(
        "Submit a pending transaction: a sale of PT into the market. Guard v0 runs the cascade estimator "
        "with and without it on the fork's current state. If including it would leave debt that liquidators "
        "cannot clear profitably, it answers DELAY (DROP is off by default)."
    )
    st.caption(
        "v0: the estimator is not yet validated against Aug 25 (Phase 3), so this shows the architecture, "
        "not proven effectiveness."
    )
    sale = st.number_input(
        "Pending sale (USD at oracle value)", min_value=10_000.0, value=5_000_000.0, step=500_000.0
    )
    if st.button("Ask the guard", type="primary"):
        res = play.guard_check(sale)
        if res["status"] != "ok":
            st.warning(res["reason"])
        else:
            (st.success if res["action"] == "PASS" else st.error)(
                f"{res['action']}: adds {res['risk_usdc']:,.0f} USDC of debt that cannot be cleared"
            )
            cmp = pd.DataFrame({"without the transaction": res["without_tx"], "with it": res["with_tx"]})
            st.dataframe(cmp, use_container_width=True)
            if res["depth_block"] != res["fork_block"]:
                st.caption(
                    f"Depth curve measured at block {res['depth_block']}; fork at {res['fork_block']}."
                )
            if res["with_tx_liquidations"]:
                lq = pd.DataFrame(res["with_tx_liquidations"])
                fig = go.Figure(go.Bar(x=lq.block, y=lq.repaid / 1e6, name="repaid (USDC)"))
                fig.update_layout(
                    title="Predicted liquidations if the transaction is included",
                    xaxis_title="block after inclusion",
                    yaxis_title="repaid (USDC)",
                )
                st.plotly_chart(fig, use_container_width=True)

st.divider()
st.subheader("Action log")
st.dataframe(pd.DataFrame(play.log), use_container_width=True)
