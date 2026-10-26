import { useEffect, useId, useState } from "react";
import { api, type GuardAnswer } from "./api";
import { usd } from "./format";
import { FieldKey, LoanField } from "./LoanField";
import { Term } from "./Term";
import { useDebounced } from "./useDebounced";
import { refillText, verdict } from "./WhatIf";

export function Guard() {
  const id = useId();
  const [sale, setSale] = useState(20e6);
  const [refill, setRefill] = useState(0);
  const [res, setRes] = useState<GuardAnswer | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const q = useDebounced({ sale, refill });

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .guard({ sale_usd: q.sale, refill: q.refill })
      .then((r) => live && (setRes(r), setErr(null)))
      .catch((e: Error) => live && setErr(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [q]);

  const held = res?.action === "DELAY";
  return (
    <section id="guard" className="section" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`}>The guard</h2>
      <p className="lede">
        Transactions wait a few seconds before they enter the next <Term k="block" />. LAPEG checks each waiting
        transaction in that gap: it simulates the next hour with and without it, and holds back a transaction that
        would leave loans stuck.
      </p>

      <div className="guard">
        <form className="controls" onSubmit={(e) => e.preventDefault()}>
          <div className="control">
            <label htmlFor={`${id}-sale`}>A waiting transaction sells</label>
            <output htmlFor={`${id}-sale`}>{usd(sale)}</output>
            <input id={`${id}-sale`} type="range" min={0.5e6} max={30e6} step={0.5e6} value={sale} onChange={(e) => setSale(Number(e.target.value))} />
          </div>
          <div className="control">
            <label htmlFor={`${id}-refill`}>How fast do buyers come back?</label>
            <output htmlFor={`${id}-refill`}>{refillText(refill)}</output>
            <input id={`${id}-refill`} type="range" min={0} max={0.03} step={0.0005} value={refill} onChange={(e) => setRefill(Number(e.target.value))} />
          </div>
        </form>

        <div className={`decision${loading && res ? " is-stale" : ""}`} aria-live="polite" aria-busy={loading}>
          {err && <p className="error">The guard did not answer: {err}. Check that the API is running.</p>}
          {!res && !err && <p className="muted">Asking the guard…</p>}
          {res && (
            <>
              <p className={`signal ${held ? "signal-hold" : "signal-pass"}`}>
                {held ? "Hold it for one block" : "Let it through"}
              </p>
              <p>
                {held
                  ? `Letting this sale in now would add ${usd(res.added_risk_usd)} of debt to loans that nobody can clear.`
                  : "This sale does not leave any loan stuck, so it goes in as normal."}
              </p>
            </>
          )}
        </div>
      </div>

      {res && (
        <div className={`compare${loading ? " is-stale" : ""}`}>
          <div>
            <h3 className="minor">If it waits</h3>
            <p>{verdict(res.without.summary)}</p>
            <LoanField loans={res.without.loans} label="Loans if the sale waits" />
          </div>
          <div>
            <h3 className="minor">If it goes in now</h3>
            <p>{verdict(res.with.summary)}</p>
            <LoanField loans={res.with.loans} label="Loans if the sale goes in now" />
          </div>
        </div>
      )}
      {res && <FieldKey />}

      <aside className="caveat">
        <p>
          This is version 0 of the guard. It can hold a transaction back, but it does not yet model the market
          recovering while the transaction waits, so a held sale is released after a set number of blocks with a
          warning attached.
          It never deletes a transaction: that would be censorship, and stays switched off.
        </p>
      </aside>
    </section>
  );
}
