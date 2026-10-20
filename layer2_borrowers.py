"""
Layer 2: who gets liquidated under a price shock (a sudden change in price), and can the market absorb the shock

For each market:
1. pull every borrower's collateral and debt from Morpho's GraphQL API,
2. compute LTV at the oracle price,
3. sort borrowers by the shock that makes them liquidatable and build Q(Delta),
4. read S_o at Q(delta) from the layer 1 ladder and find Delta*,
5. compare the largest position and total collateral with q* and Pendle's refusal limit.

"""

import argparse, glob, os
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
from pendle_ladder_all import load_markets, eth_call, quote_usdc

MORPHO_API = "https://api.morpho.org/graphql"

# ------------1. data------------
def gql(query, variables=None):
    """POST one GraphQL query and return its 'data'. Raise on HTTP or GraphQL errors."""
    response = requests.post(MORPHO_API, json={"query": query, "variables": variables or {}}, timeout=60)
    try:
        body = response.json()
    except ValueError:
        response.raise_for_status()
        raise
    if body.get("errors"):
        raise RuntimeError("GraphQL error: " + "; ".join(e.get("message", str(e)) for e in body["errors"]))
    response.raise_for_status()
    return body["data"]

POSITIONS_QUERY = """
query($id: String!, $first: Int!, $skip: Int!) {
  marketPositions(first: $first, skip: $skip, orderBy: BorrowShares, orderDirection: Desc,
                  where: {marketUniqueKey_in: [$id], chainId_in: [1], borrowShares_gte: "1"}) {
    items {
      user { address }
      healthFactor
      state { timestamp collateral borrowAssets }
    }
    pageInfo { countTotal count }
  }
}"""


def fetch_positions(market_id, dec, page_size=500):
    """Page through all positions for one market.
    Returns a DataFrame: user, collateral_pt, borrow_usdc, health_factor_api, state_timestamp.
    Keep only rows with borrow > 0. Scale raw ints yourself (PT: 10**dec, USDC: 10**6)."""
    rows, skip, total = [], 0, None
    while total is None or skip < total:
        page = gql(POSITIONS_QUERY, {"id": market_id, "first": page_size, "skip": skip})["marketPositions"]
        total = page["pageInfo"]["countTotal"]
        items = page["items"] or []
        if not items:
            break
        for p in items:
            s = p["state"] or {}
            rows.append(dict(user=p["user"]["address"].lower(),
                             collateral_pt=int(s.get("collateral") or 0) / 10 ** dec,
                             borrow_usdc=int(s.get("borrowAssets") or 0) / 10 ** 6,
                             health_factor_api=p["healthFactor"],
                             state_timestamp=s.get("timestamp")))
        skip += len(items)
    df = pd.DataFrame(rows, columns=["user", "collateral_pt", "borrow_usdc", "health_factor_api", "state_timestamp"])
    if len(df) != total or df["user"].duplicated().any():
        raise RuntimeError(f"paging mismatch: got {len(df)} rows ({df['user'].nunique()} unique), API says {total}")
    return df[df["borrow_usdc"] > 0].reset_index(drop=True)

def check_totals(df, market_id, dec):
    """Sum of positions vs market-level collateralAssets / borrowAssets. Print the % gap."""
    q = "query($id: String!) { marketById(marketId: $id, chainId: 1) { state { collateralAssets borrowAssets } } }"
    state = gql(q, {"id": market_id})["marketById"]["state"]
    market_collateral = int(state["collateralAssets"]) / 10 ** dec
    market_borrow = int(state["borrowAssets"]) / 10 ** 6
    gaps = {"collateral_gap_pct": (df["collateral_pt"].sum() / market_collateral - 1) * 100,
            "borrow_gap_pct": (df["borrow_usdc"].sum() / market_borrow - 1) * 100}
    print(f"  collateral: positions {df['collateral_pt'].sum():,.0f} vs market {market_collateral:,.0f} PT "
          f"({gaps['collateral_gap_pct']:+.3f}%)")
    print(f"  borrow:     positions {df['borrow_usdc'].sum():,.0f} vs market {market_borrow:,.0f} USDC "
          f"({gaps['borrow_gap_pct']:+.3f}%)")
    return gaps


# ---------- 2. shock model ----------
def add_ltv(df, p_oracle, lltv):
    """Add ltv = borrow / (collateral * p_oracle) and delta_i = 1 - ltv / lltv.
    Sanity check: lltv / ltv should match health_factor_api."""
    df = df.copy()
    df["ltv"] = df["borrow_usdc"] / (df["collateral_pt"] * p_oracle)
    df["delta_i"] = 1 - df["ltv"] / lltv
    hf_gap = (lltv / df["ltv"] - df["health_factor_api"]).abs().max()
    print(f"  LTV check: max |LLTV/LTV - API health factor| = {hf_gap:.2e}, "
          f"already liquidatable: {(df['ltv'] > lltv).sum()}")
    return df


def shock_curve(df, p_oracle, lif):
    """Sort by delta_i. For each breakpoint, return:
    delta, n_liquidatable, Q_total_pt (cumulative collateral),
    Q_seizable_pt (cumulative min(c, b*LIF / (p_oracle*(1-delta))))."""
    d = df.sort_values("delta_i").reset_index(drop=True)
    c = d["collateral_pt"].to_numpy()
    b = d["borrow_usdc"].to_numpy()
    deltas = d["delta_i"].clip(lower=0).to_numpy()
    q_seiz = np.empty(len(d))
    for k, delta in enumerate(deltas):
        shocked_price = p_oracle * (1 - delta)
        q_seiz[k] = np.minimum(c[:k + 1], b[:k + 1] * lif / shocked_price).sum()
    return pd.DataFrame({"delta": deltas,
                         "user": d["user"],
                         "n_liquidatable": np.arange(1, len(d) + 1),
                         "Q_total_pt": c.cumsum(),
                         "Q_seizable_pt": q_seiz,
                         "Q_total_usd_shocked": c.cumsum() * p_oracle * (1 - deltas)})


# ---------- 3. link to Layer 1 ----------
def load_ladder(path, market, threshold):
    """Ladder rows for one market, in PT units (invariant to a proportional price shock).
    Returns pt and s_o arrays of successful quotes (sorted), the smallest refused pt_sold (or None),
    and the q* bracket in PT: last good size below the first failing one, and that failing size."""
    lad = pd.read_csv(path)
    lad = lad[lad["market"] == market].sort_values("pt_sold")
    if lad.empty:
        raise SystemExit(f"no ladder rows for {market} in {path}")
    quoted = lad.dropna(subset=["s_o"])
    refused = lad[lad["s_o"].isna()]
    refusal_pt = refused["pt_sold"].min() if len(refused) else None
    fails = lad[lad["s_o"].isna() | (lad["s_o"] >= threshold)]
    q_high = fails["pt_sold"].min() if len(fails) else None
    good = quoted[quoted["s_o"] < threshold]
    q_low = good[good["pt_sold"] < q_high]["pt_sold"].max() if q_high is not None else good["pt_sold"].max()
    kind = "none" if q_high is None else ("refused" if refusal_pt is not None and q_high == refusal_pt else "slippage")
    return dict(pt=quoted["pt_sold"].to_numpy(), s_o=quoted["s_o"].to_numpy(), refusal_pt=refusal_pt,
                qstar_low_pt=q_low, qstar_high_pt=q_high, qstar_kind=kind,
                snapshot=lad["snapshot_utc"].iloc[0])


def latest_ladder(folder, market):
    """Newest ladder_all_*.csv in `folder` that has rows for `market` (file names sort by UTC stamp)."""
    for path in sorted(glob.glob(os.path.join(folder, "ladder_all_*.csv")), reverse=True):
        if (pd.read_csv(path, usecols=["market"])["market"] == market).any():
            return path
    raise SystemExit(f"no ladder file with {market} in {folder}")


def s_o_at(q_pt, ladder):
    """Interpolate S_o over pt_sold. Return inf if q_pt >= refusal_pt,
    NaN if q_pt is beyond the largest successful quote (re-quote instead of extrapolating).
    Below the smallest quote, use the smallest quote's S_o."""
    if ladder["refusal_pt"] is not None and q_pt >= ladder["refusal_pt"]:
        return np.inf
    if len(ladder["pt"]) == 0 or q_pt > ladder["pt"].max():
        return np.nan
    return float(np.interp(q_pt, ladder["pt"], ladder["s_o"]))


def add_quote(ladder, q_pt, s_o):
    """Insert a fresh quote into the ladder (s_o=None means refused)."""
    if s_o is None:
        ladder["refusal_pt"] = q_pt if ladder["refusal_pt"] is None else min(ladder["refusal_pt"], q_pt)
        return
    order = np.argsort(np.append(ladder["pt"], q_pt))
    ladder["pt"] = np.append(ladder["pt"], q_pt)[order]
    ladder["s_o"] = np.append(ladder["s_o"], s_o)[order]


def find_delta_star(curve, ladder, threshold, q_col, requote=None):
    """Smallest delta where s_o_at(Q) >= threshold or the sale is refused.
    If Q falls outside the ladder, re-quote it live when `requote` is given, else stop and say so."""
    for _, row in curve.iterrows():
        q = row[q_col]
        s = s_o_at(q, ladder)
        if np.isnan(s) and requote is not None:
            add_quote(ladder, q, requote(q))
            s = s_o_at(q, ladder)
        if np.isnan(s) and len(ladder["s_o"]) and ladder["s_o"].max() >= threshold:
            status = "slippage (inferred: Q is beyond the ladder and a smaller sale already fails)"
        elif np.isnan(s):
            status = "unknown: Q beyond ladder (run with --requote)"
        elif s >= threshold:
            status = "refused" if np.isinf(s) else "slippage"
        else:
            continue
        return dict(delta_star=row["delta"], q_pt_at_star=q, n_liquidatable=int(row["n_liquidatable"]),
                    trigger_user=row["user"], s_o_at_star=s, status=status)
    return dict(delta_star=None, q_pt_at_star=None, n_liquidatable=None, trigger_user=None,
                s_o_at_star=None, status="no crossing: every borrower can be liquidated profitably")


def make_requoter(m, log):
    """Live PT -> USDC quote at q_pt (read-only, rate-limited by pendle_ladder_all). Returns S_o or None."""
    def requote(q_pt):
        usdc_out, note, _ = quote_usdc(m, q_pt * m["price"])
        s = None if usdc_out is None else 1 - (usdc_out / q_pt) / m["price"]
        log.append(dict(market=m["name"], pt_sold=q_pt, size_usd=q_pt * m["price"], usdc_out=usdc_out,
                        s_o=s, note=note, quoted_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")))
        print(f"    re-quote {q_pt / 1e6:.3f}M PT: " + ("refused" if s is None else f"S_o = {100 * s:.3f}%"))
        return s
    return requote


def short_address(addr):
    """0x6b28fb...be5b -> 0x6b28...be5b: still unique per borrower, but not the full wallet."""
    return addr if not isinstance(addr, str) or len(addr) < 12 else f"{addr[:6]}...{addr[-4:]}"


# ---------- 4. main ----------
def run_market(m, a, market_id, stamp, requotes):
    """Positions, shock curve and Delta* for one market. Returns (positions, curve, summary row)."""
    name, lltv, price, thr = m["name"], m["lltv"], m["price"], m["threshold"]
    lif = min(1.15, 1 / (0.3 * lltv + 0.7))
    print(f"\n{name}: oracle {price:.6f}, LLTV {lltv}, LIF {lif:.4f}, threshold {100 * thr:.2f}%")

    pos = fetch_positions(market_id, m["dec"])
    if pos.empty:
        print("  no borrowers with debt: skipped")
        return None
    gaps = check_totals(pos, market_id, m["dec"])
    pos = add_ltv(pos, price, lltv)
    curve = shock_curve(pos, price, lif)
    ladder_path = a.ladder or latest_ladder(a.outdir, name)
    ladder = load_ladder(ladder_path, name, thr)
    print(f"  ladder: {os.path.basename(ladder_path)}")
    requote = make_requoter(m, requotes) if a.requote else None

    res = {}
    for q_col, tag in [("Q_total_pt", "total"), ("Q_seizable_pt", "seizable")]:
        res[tag] = find_delta_star(curve, ladder, thr, q_col, requote)
        curve[f"s_o_{tag}"] = [s_o_at(q, ladder) for q in curve[q_col]]
        r = res[tag]
        if r["delta_star"] is None:
            print(f"  Delta* ({tag}): {r['status']}")
        else:
            print(f"  Delta* ({tag}): {100 * r['delta_star']:.2f}%  Q = {r['q_pt_at_star'] / 1e6:.2f}M PT, "
                  f"{r['n_liquidatable']} borrowers, {r['status']}")

    top = pos.sort_values("collateral_pt", ascending=False)
    largest = top.iloc[0]
    q_low = ladder["qstar_low_pt"]
    total_c = pos["collateral_pt"].sum()
    row = dict(market=name, snapshot_utc=stamp, ladder_snapshot_utc=ladder["snapshot"],
               positions_timestamp=int(pos["state_timestamp"].astype("int64").max()), oracle_price=price, lltv=lltv,
               lif=lif, threshold=thr, n_borrowers=len(pos), borrow_usdc=pos["borrow_usdc"].sum(),
               collateral_pt=total_c, collateral_usd=total_c * price,
               collateral_gap_pct=gaps["collateral_gap_pct"], borrow_gap_pct=gaps["borrow_gap_pct"],
               qstar_kind=ladder["qstar_kind"], qstar_low_pt=q_low, qstar_high_pt=ladder["qstar_high_pt"],
               refusal_pt=ladder["refusal_pt"],
               largest_user=largest["user"], largest_collateral_pt=largest["collateral_pt"],
               largest_delta_i=largest["delta_i"],
               largest_over_qstar=None if q_low is None else largest["collateral_pt"] / q_low,
               top5_share=top["collateral_pt"].head(5).sum() / total_c,
               collateral_over_refusal=None if ladder["refusal_pt"] is None else total_c / ladder["refusal_pt"],
               max_seizable_pt=curve["Q_seizable_pt"].iloc[-1],
               min_delta_i=pos["delta_i"].min())
    for tag in ("total", "seizable"):
        row.update({f"{k}_{tag}": v for k, v in res[tag].items()})
    print(f"  largest position {largest['collateral_pt'] / 1e6:.2f}M PT "
          f"(trigger {100 * largest['delta_i']:.2f}%) vs q* {q_low / 1e6:.2f}M PT ({ladder['qstar_kind']}); "
          f"top-5 share {100 * row['top5_share']:.1f}%")
    if ladder["refusal_pt"] is not None:
        print(f"  borrower collateral {total_c / 1e6:.2f}M PT vs refusal at {ladder['refusal_pt'] / 1e6:.2f}M PT "
              f"({row['collateral_over_refusal']:.2f}x)")
    return pos.assign(market=name, snapshot_utc=stamp), curve.assign(market=name, snapshot_utc=stamp), row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="results/morpho_pt_markets_clean.csv")
    ap.add_argument("--ladder", help="ladder CSV to use (default: newest one in --outdir per market)")
    ap.add_argument("--outdir", default="results")
    ap.add_argument("--only")
    ap.add_argument("--requote", action="store_true", help="live Pendle quote when Q is beyond the ladder")
    ap.add_argument("--short-addresses", action="store_true", help="write wallets as 0x1234...abcd")
    a = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    markets = load_markets(a.data, live=True)        # fresh oracle price, same moment as positions
    ids = pd.read_csv(a.data).set_index("market")["market_id"].to_dict()
    if a.only:
        markets = [m for m in markets if m["name"] == a.only]

    all_pos, all_curves, summary, requotes, failed = [], [], [], [], []
    for m in markets:
        try:
            out = run_market(m, a, ids[m["name"]], stamp, requotes)
        except Exception as e:                       # one market failing should not lose the others
            print(f"  FAILED: {type(e).__name__}: {e}")
            failed.append(m["name"])
            continue
        if out is not None:
            all_pos.append(out[0]); all_curves.append(out[1]); summary.append(out[2])

    if not summary:
        raise SystemExit("no market produced results")
    pos, curves, sm = pd.concat(all_pos), pd.concat(all_curves), pd.DataFrame(summary)
    if a.short_addresses:
        pos["user"] = pos["user"].map(short_address)
        curves["user"] = curves["user"].map(short_address)
        for col in ("largest_user", "trigger_user_total", "trigger_user_seizable"):
            sm[col] = sm[col].map(short_address)
    os.makedirs(a.outdir, exist_ok=True)
    pos.to_csv(os.path.join(a.outdir, f"positions_{stamp}.csv"), index=False)
    curves.to_csv(os.path.join(a.outdir, f"shock_curve_{stamp}.csv"), index=False)
    sm.to_csv(os.path.join(a.outdir, f"delta_star_{stamp}.csv"), index=False)
    if requotes:
        pd.DataFrame(requotes).to_csv(os.path.join(a.outdir, f"requotes_{stamp}.csv"), index=False)
    print(f"\nwrote positions_{stamp}.csv, shock_curve_{stamp}.csv, delta_star_{stamp}.csv to {a.outdir}")
    if failed:
        raise SystemExit(f"finished with failures: {', '.join(failed)}")


if __name__ == "__main__":
    main()