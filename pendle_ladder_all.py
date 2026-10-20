#!/usr/bin/env python3
"""
Automatic q* search for every PT market in your dataset.

For each market it:
  1. reads PT address, decimals, LLTV, borrow and oracle price from the CSV
     (optionally re-reads price() and decimals() live from Ethereum with --live),
  2. doubles the sale size until a PT -> USDC quote is unprofitable or refused,
  3. bisects between the last profitable and first failing size,
  4. writes every quote and a summary of q* per market.

Profitable means  S_o(q) = 1 - P_exec(q)/P_oracle  <  1 - 1/LIF,
with LIF = min(1.15, 1/(0.3*LLTV + 0.7))  (Morpho Blue docs).

Usage:
    pip install requests pandas matplotlib
    python pendle_ladder_all.py --probe                 # dump one raw API response and stop
    python pendle_ladder_all.py                         # all markets
    python pendle_ladder_all.py --live                  # refresh oracle price and decimals on-chain
    python pendle_ladder_all.py --only PT-USD3-17DEC2026 --plot
    python pendle_ladder_all.py --mock                  # offline test with a synthetic curve

No API key is needed on the free tier (200 CU/min, 200k CU/week per IP); the script paces itself from
the rate-limit headers. Optional env vars: PENDLE_API_KEY (Bearer token), ETH_RPC_URL (for --live).
Only a combined PT -> USDC route is measured; refusals are recorded, not hidden.
"""
import argparse, json, math, os, sys, time
from datetime import datetime, timezone
import pandas as pd
import requests

CHAIN_ID = 1
CONVERT_URL = f"https://api-v2.pendle.finance/core/v3/sdk/{CHAIN_ID}/convert"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_DEC = 6
LOAN_DEC = 6
# Any valid address works: we only read quotes and never send a transaction.
# The API rejects 0x000...001 as invalid, so default to a well-known public address.
DUMMY_RECEIVER = os.getenv("RECEIVER", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
RPC_URL = os.getenv("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")

START_USD = 100e3        # first size tried
GROWTH = 2.0             # bracket by doubling
MAX_BORROW_MULT = 1.5    # stop bracketing beyond 1.5x outstanding borrow
REL_TOL = 0.02           # stop bisecting when the interval is within 2%
MAX_BISECT = 12
SLEEP = 0.5              # base delay; real pacing comes from rate-limit headers


# ---------- helpers ----------
def liq_threshold(lltv):
    lif = min(1.15, 1 / (0.3 * lltv + 0.7))
    return 1 - 1 / lif


def eth_call(to, data):
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]}
    r = requests.post(RPC_URL, json=body, timeout=30)
    r.raise_for_status()
    return int(r.json()["result"], 16)


def load_markets(path, live):
    df = pd.read_csv(path)
    markets = []
    for _, r in df.iterrows():
        m = dict(name=r["market"], pt=r["collateral_token"], oracle=r["oracle"],
                 lltv=float(r["lltv"]), borrow=float(r["outstanding_loans_usd"]),
                 dec=int(r["collateral_decimals"]), price=float(r["oracle_price_usdc_per_pt"]))
        if live:
            m["dec"] = eth_call(m["pt"], "0x313ce567")                  # decimals()
            raw = eth_call(m["oracle"], "0xa035b1fe")                   # price()
            m["price"] = raw / 10 ** (36 + LOAN_DEC - m["dec"])
        m["threshold"] = liq_threshold(m["lltv"])
        markets.append(m)
    return markets


def find_usdc_amount(node):
    """Recursively find {'token': USDC, 'amount': ...} anywhere in the response."""
    if isinstance(node, dict):
        tok = node.get("token")
        if isinstance(tok, str) and tok.lower() == USDC.lower() and "amount" in node:
            return int(node["amount"]) / 10 ** USDC_DEC
        for v in node.values():
            out = find_usdc_amount(v)
            if out is not None:
                return out
    elif isinstance(node, list):
        for v in node:
            out = find_usdc_amount(v)
            if out is not None:
                return out
    return None


def parse_routes(j):
    """Best (highest USDC) route across all returned routes/aggregators, with Pendle's impact split."""
    best = None
    for route in j.get("routes", []):
        for o in route.get("outputs", []):
            if str(o.get("token", "")).lower() == USDC.lower():
                amt = int(o["amount"]) / 10 ** USDC_DEC
                if best is None or amt > best["usdc_out"]:
                    d = route.get("data", {}) or {}
                    br = d.get("priceImpactBreakDown", {}) or {}
                    best = dict(usdc_out=amt, aggregator=d.get("aggregatorType"),
                                pendle_impact=d.get("priceImpact"),
                                internal_impact=br.get("internalPriceImpact"),
                                external_impact=br.get("externalPriceImpact"),
                                gas_used=d.get("gasUsed"), fee_usd=(d.get("fee") or {}).get("usd"))
    if best is not None:
        best["n_routes"] = len(j.get("routes", []))
    return best


def shorten(node, n=70):
    if isinstance(node, dict):
        return {k: shorten(v, n) for k, v in node.items()}
    if isinstance(node, list):
        return [shorten(v, n) for v in node]
    if isinstance(node, str) and len(node) > n:
        return node[:n] + f"...({len(node)} chars)"
    return node


def min_out_fallback(j, slippage=0.5):
    """Derive the expected output from minTokenOut = expected * (1 - slippage)."""
    try:
        for route in j.get("routes", []):
            for p in route["contractParamInfo"]["contractCallParams"]:
                if isinstance(p, dict) and "minTokenOut" in p:
                    return int(p["minTokenOut"]) / (1 - slippage) / 10 ** USDC_DEC
    except (KeyError, TypeError, ValueError):
        pass
    return None


RATE = {"cost": 25.0, "remaining": None, "reset": None}   # free tier: 200 CU/min, 200k CU/week


def update_rate(r):
    h = r.headers
    try:
        if "X-Computing-Unit" in h:
            RATE["cost"] = float(h["X-Computing-Unit"])
        if "X-RateLimit-Remaining" in h:
            RATE["remaining"] = float(h["X-RateLimit-Remaining"])
        if "X-RateLimit-Reset" in h:
            RATE["reset"] = float(h["X-RateLimit-Reset"])
    except ValueError:
        pass


def wait_if_needed(force=False):
    """Sleep until the per-minute window resets if the next call would not fit."""
    rem, reset = RATE["remaining"], RATE["reset"]
    if force or (rem is not None and reset is not None and rem < 1.5 * RATE["cost"]):
        wait = (max(0.0, reset - time.time()) + 1) if reset else 65
        print(f"    (rate limit: waiting {min(wait, 70):.0f}s for the window to reset)")
        time.sleep(min(wait, 70))
        RATE["remaining"] = None


def raw_quote(pt, base_units, slippage=0.5):
    body = {"receiver": DUMMY_RECEIVER, "slippage": slippage,
            "inputs": [{"token": pt, "amount": str(base_units)}],
            "outputs": [USDC], "enableAggregator": True}
    headers = {"Authorization": f"Bearer {os.environ['PENDLE_API_KEY']}"} if os.getenv("PENDLE_API_KEY") else {}
    return requests.post(CONVERT_URL, json=body, headers=headers, timeout=120)


def mock_usdc_out(m, size):
    """Synthetic curve with a cliff at 12% of borrow, to test the search logic offline."""
    cliff = 0.12 * m["borrow"]
    s = 0.0003 * (size / 1e6) ** 0.6
    if size > cliff:
        s += 0.5 * (size / cliff - 1)
    if size > 1.4 * cliff:
        return None, "mock refusal"
    return size * (1 - s), ""


def quote_usdc(m, size, mock=False, probe=False):
    """Return (usdc_out or None, note, extra dict)."""
    if mock:
        out, note = mock_usdc_out(m, size)
        return out, note, {}
    pt_units = size / m["price"]
    base = int(round(pt_units * 10 ** m["dec"]))
    last_note = ""
    for attempt in range(3):
        wait_if_needed()
        try:
            r = raw_quote(m["pt"], base)
            update_rate(r)
        except requests.RequestException as e:
            last_note = f"request error: {e}"
            time.sleep(3)
            continue
        if probe:
            print(r.status_code, json.dumps(shorten(r.json()), indent=2))
        if r.status_code == 429:
            last_note = "HTTP 429 rate limited"
            wait_if_needed(force=True)
            continue
        if not (200 <= r.status_code < 300):
            last_note = f"HTTP {r.status_code}: {r.text[:160]}"
            if attempt == 0:          # retry a refusal once to rule out flakiness
                time.sleep(2)
                continue
            return None, last_note, {}
        best = parse_routes(r.json())
        if best is not None:
            return best["usdc_out"], "", best
        out = find_usdc_amount(r.json())
        if out is None:
            out = min_out_fallback(r.json())
            if out is not None and not getattr(quote_usdc, "warned", False):
                print("    (note: no explicit output amount found, deriving it from minTokenOut / (1 - slippage))")
                quote_usdc.warned = True
        if out is None:
            raise SystemExit("Could not find the USDC output amount in the response. "
                             "Run with --probe and adjust find_usdc_amount().")
        return out, "", {}
    return None, last_note, {}


def measure(m, size, phase, rows, mock):
    usdc_out, note, extra = quote_usdc(m, size, mock)
    pt_units = size / m["price"]
    if usdc_out is None:
        row = dict(market=m["name"], phase=phase, size_usd=size, pt_sold=pt_units, usdc_out=None,
                   p_exec=None, s_o=None, threshold=m["threshold"], ok=False, note=note)
    else:
        p_exec = usdc_out / pt_units
        s_o = 1 - p_exec / m["price"]
        row = dict(market=m["name"], phase=phase, size_usd=size, pt_sold=pt_units, usdc_out=usdc_out,
                   p_exec=p_exec, s_o=s_o, threshold=m["threshold"], ok=s_o < m["threshold"], note=note)
    row.update({k: extra.get(k) for k in ("aggregator", "n_routes", "pendle_impact",
                                          "internal_impact", "external_impact", "gas_used", "fee_usd")})
    rows.append(row)
    shown = "refused" if row["s_o"] is None else f"{100 * row['s_o']:.3f}%"
    agg = f"  via {extra['aggregator']}" if extra.get("aggregator") else ""
    print(f"  {phase:8s} ${size / 1e6:9.3f}M   S_o = {shown:>9s}   (threshold {100 * m['threshold']:.2f}%){agg}  {note[:60]}")
    if not mock:
        time.sleep(SLEEP)
    return row["ok"]


def find_q_star(m, rows, mock):
    lo, hi, size = None, None, START_USD
    while size <= MAX_BORROW_MULT * m["borrow"]:
        if measure(m, size, "bracket", rows, mock):
            lo, size = size, size * GROWTH
        else:
            hi = size
            break
    cap = MAX_BORROW_MULT * m["borrow"]
    if hi is None and lo is not None and lo < 0.999 * cap:
        if measure(m, cap, "cap", rows, mock):
            lo = cap
        else:
            hi = cap
    def refused_at(size):
        return any(r["market"] == m["name"] and r["size_usd"] == size and r["s_o"] is None for r in rows)

    if hi is None:
        return lo, None, "no crossing up to %.1fx borrow" % MAX_BORROW_MULT
    if lo is None:
        return None, hi, ("refused" if refused_at(hi) else "unprofitable") + " even at the starting size"
    for _ in range(MAX_BISECT):
        if (hi - lo) / lo <= REL_TOL:
            break
        mid = (lo + hi) / 2
        if measure(m, mid, "bisect", rows, mock):
            lo = mid
        else:
            hi = mid
    return lo, hi, "refusal boundary" if refused_at(hi) else "slippage crossing"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="morpho_pt_markets_clean.csv")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--only", help="market name to run, e.g. PT-USD3-17DEC2026")
    ap.add_argument("--live", action="store_true", help="re-read oracle price and decimals on-chain")
    ap.add_argument("--probe", action="store_true", help="print one raw API response and stop")
    ap.add_argument("--mock", action="store_true", help="offline test with a synthetic curve")
    ap.add_argument("--plot", action="store_true")
    a = ap.parse_args()

    markets = load_markets(a.data, a.live)
    if a.only:
        markets = [m for m in markets if m["name"] == a.only]
    if a.probe:
        quote_usdc(markets[0], 1e6, probe=True)
        return

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    rows, summary = [], []
    for m in markets:
        print(f"\n{m['name']}: oracle {m['price']:.6f}, {m['dec']} decimals, "
              f"threshold {100 * m['threshold']:.2f}%, borrow ${m['borrow'] / 1e6:.2f}M")
        lo, hi, status = find_q_star(m, rows, a.mock)
        summary.append(dict(market=m["name"], q_star_low_usd=lo, q_star_high_usd=hi,
                            borrow_usd=m["borrow"], threshold=m["threshold"],
                            low_pct_of_borrow=None if lo is None else 100 * lo / m["borrow"],
                            high_pct_of_borrow=None if hi is None else 100 * hi / m["borrow"],
                            s_o_at_start_pct=next((100 * r["s_o"] for r in rows
                                                   if r["market"] == m["name"] and r["s_o"] is not None), None),
                            status=status, oracle_price=m["price"], snapshot_utc=stamp))
    os.makedirs(a.outdir, exist_ok=True)
    ladder = pd.DataFrame(rows)
    ladder["snapshot_utc"] = stamp
    ladder.to_csv(os.path.join(a.outdir, f"ladder_all_{stamp}.csv"), index=False)
    sm = pd.DataFrame(summary)
    sm.to_csv(os.path.join(a.outdir, f"qstar_summary_{stamp}.csv"), index=False)
    print("\n=== q* summary (USD) ===")
    print(sm[["market", "q_star_low_usd", "q_star_high_usd", "low_pct_of_borrow",
              "high_pct_of_borrow", "status"]].round(1).to_string(index=False))

    if a.plot:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 5))
        for name, g in ladder.dropna(subset=["s_o"]).groupby("market"):
            g = g.sort_values("size_usd")
            ax.plot(g.size_usd, 100 * g.s_o, marker="o", label=name)
        for m in markets:
            ax.axhline(100 * m["threshold"], ls="--", lw=0.8, color="grey")
        ax.set_xscale("log"); ax.set_yscale("symlog", linthresh=0.1); ax.axhline(0, color="black", lw=0.6)
        ax.set_xlabel("sale size q (USD)"); ax.set_ylabel("S_o(q) (%)"); ax.legend()
        fig.savefig(os.path.join(a.outdir, f"so_curves_{stamp}.png"), dpi=150, bbox_inches="tight")


if __name__ == "__main__":
    main()
