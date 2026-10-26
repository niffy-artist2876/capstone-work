import { useEffect, useId, useState } from "react";
import { api, type Overview, type Outcome, type WhatIf as WhatIfResult } from "./api";
import { minutes, pct, plural, usd } from "./format";
import { FieldKey, LoanField } from "./LoanField";
import { PriceTrace } from "./PriceTrace";
import { Term } from "./Term";
import { useDebounced } from "./useDebounced";

type Mode = "drop" | "sale";

export function halfLife(refill: number): number | null {
  return refill <= 0 ? null : ((Math.log(0.5) / Math.log(1 - refill)) * 12) / 60;
}

export function refillText(refill: number): string {
  const h = halfLife(refill);
  return h === null ? "Never: nobody buys back within the hour" : `Half of what was sold is bought back within ${minutes(h)}`;
}

/** One plain-language sentence for what happened. */
export function verdict(s: Outcome["summary"]): string {
  const parts: string[] = [];
  if (s.stuck === 0 && s.bad_debt_usd < 1) {
    parts.push(
      s.rescued === 0
        ? "No loan gets into trouble."
        : `${plural(s.rescued, "loan")} ${s.rescued === 1 ? "gets" : "get"} into trouble and ${
            s.rescued === 1 ? "is" : "are"
          } liquidated in time. Nobody loses money.`,
    );
  } else {
    if (s.rescued) parts.push(`${plural(s.rescued, "loan")} ${s.rescued === 1 ? "is" : "are"} liquidated in time.`);
    if (s.stuck)
      parts.push(
        `${plural(s.stuck, "loan")} holding ${usd(s.stuck_debt_usd)} of debt ${s.stuck === 1 ? "is" : "are"} stuck: allowed to be liquidated, but nobody would profit from doing it.`,
      );
    if (s.bad_debt_usd >= 1) parts.push(`Lenders lose ${usd(s.bad_debt_usd)}.`);
  }
  if (!s.settled) parts.push("After an hour, the market still has not settled.");
  return parts.join(" ");
}

export function WhatIf({ overview }: { overview: Overview }) {
  const id = useId();
  const [mode, setMode] = useState<Mode>("drop");
  const [drop, setDrop] = useState(0.02);
  const [sale, setSale] = useState(10e6);
  const [refill, setRefill] = useState(0);
  const [lag, setLag] = useState(true);
  const [res, setRes] = useState<WhatIfResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const q = useDebounced({ mode, drop, sale, refill, lag });

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .whatif({
        drop: q.mode === "drop" ? q.drop : 0,
        sale_usd: q.mode === "sale" ? q.sale : 0,
        refill: q.refill,
        lag: q.lag,
      })
      .then((r) => live && (setRes(r), setErr(null)))
      .catch((e: Error) => live && setErr(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [q]);

  return (
    <section id="what-if" className="section" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`}>What if?</h2>
      <p className="lede">
        Pick a shock and watch all {overview.market.borrowers} loans react over the next hour. The simulation uses the
        real loans and the market depth measured above, and follows the lending app's own rules.
      </p>

      <div className="whatif">
        <form className="controls" onSubmit={(e) => e.preventDefault()}>
          <fieldset className="choice">
            <legend>What happens?</legend>
            <label>
              <input type="radio" name={`${id}-mode`} checked={mode === "drop"} onChange={() => setMode("drop")} />
              The <Term k="oracle">price feed</Term> drops
            </label>
            <label>
              <input type="radio" name={`${id}-mode`} checked={mode === "sale"} onChange={() => setMode("sale")} />
              Someone sells a lot at once
            </label>
          </fieldset>

          {mode === "drop" ? (
            <div className="control">
              <label htmlFor={`${id}-drop`}>Price drop</label>
              <output htmlFor={`${id}-drop`}>{pct(drop, 1)}</output>
              <input id={`${id}-drop`} type="range" min={0} max={0.15} step={0.0025} value={drop} onChange={(e) => setDrop(Number(e.target.value))} />
            </div>
          ) : (
            <div className="control">
              <label htmlFor={`${id}-sale`}>Amount sold in one go</label>
              <output htmlFor={`${id}-sale`}>{usd(sale)}</output>
              <input id={`${id}-sale`} type="range" min={0.5e6} max={30e6} step={0.5e6} value={sale} onChange={(e) => setSale(Number(e.target.value))} />
            </div>
          )}

          <div className="control">
            <label htmlFor={`${id}-refill`}>How fast do buyers come back?</label>
            <output htmlFor={`${id}-refill`}>{refillText(refill)}</output>
            <input id={`${id}-refill`} type="range" min={0} max={0.03} step={0.0005} value={refill} onChange={(e) => setRefill(Number(e.target.value))} />
          </div>

          <label className="check">
            <input type="checkbox" checked={lag} onChange={(e) => setLag(e.target.checked)} />
            The price feed averages over 15 minutes, as this market's does
          </label>
        </form>

        <div className={`result${loading && res ? " is-stale" : ""}`} aria-live="polite" aria-busy={loading}>
          {err && <p className="error">The simulation did not run: {err}. Check that the API is running.</p>}
          {!res && !err && <p className="muted">Running the simulation…</p>}
          {res && (
            <>
              <p className="verdict">{verdict(res.summary)}</p>
              <LoanField loans={res.loans} label={`${res.summary.rescued} liquidated, ${res.summary.stuck} stuck, ${res.summary.loss} with a loss, ${res.summary.safe} safe`} />
              <FieldKey counts={res.summary} />
              <h3 className="minor">The price the lending app sees, next hour</h3>
              <PriceTrace timeline={res.timeline} />
            </>
          )}
        </div>
      </div>

      <aside className="caveat">
        <p>
          Buyers coming back is the big unknown. With the slider at “never”, every sale stays in the market for the
          whole hour, which is the worst case. Real buyers do return: on August 25, 2026, this same market liquidated{" "}
          {usd(overview.aug25.repaid_usd)} in about {minutes(overview.aug25.minutes)} with{" "}
          {overview.aug25.bad_debt_usd > 0 ? `${usd(overview.aug25.bad_debt_usd)} lost` : "no losses"}:{" "}
          {(overview.aug25.repaid_usd / overview.capacity.low_usd).toFixed(1)} times what one sale can absorb today.
          Measuring how fast buyers return is the next stage of this project.
        </p>
      </aside>
    </section>
  );
}
