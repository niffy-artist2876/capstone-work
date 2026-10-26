import { useEffect, useState } from "react";
import { api, type Overview } from "./api";
import { DepthProfile } from "./DepthProfile";
import { minutes, pct, plural, usd } from "./format";
import { Guard } from "./Guard";
import { Lab } from "./Lab";
import { Term } from "./Term";
import { WhatIf } from "./WhatIf";

function useView(): "story" | "lab" {
  const read = (): "story" | "lab" => (window.location.hash === "#lab" ? "lab" : "story");
  const [view, setView] = useState(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return view;
}

function Masthead({ view }: { view: "story" | "lab" }) {
  return (
    <header className="masthead">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <a className="wordmark" href="#top">
        LAPEG
      </a>
      <nav aria-label="Sections">
        <a href="#top" aria-current={view === "story" ? "page" : undefined}>
          The finding
        </a>
        <a href="#what-if">What if?</a>
        <a href="#guard">The guard</a>
        <a href="#lab" aria-current={view === "lab" ? "page" : undefined}>
          Live lab
        </a>
      </nav>
    </header>
  );
}

function Story({ o }: { o: Overview }) {
  const m = o.market;
  const bp = o.break_point;
  const e2e = o.end_to_end;
  return (
    <main id="main" className="page">
      <section className="hero" aria-labelledby="hero-h">
        <h1 id="hero-h">How much selling can a {usd(m.debt_usd, 0)} lending market take?</h1>
        <p className="lede">
          We made a copy of Ethereum and sold this market's <Term k="collateral" /> in larger and larger amounts,
          using real trades. Up to about {usd(o.capacity.low_usd)}, buyers absorb it. Past {usd(o.capacity.high_usd)},
          the floor drops away.
        </p>
        <DepthProfile points={o.depth} breakEven={m.break_even_loss} capacity={o.capacity} />
      </section>

      <section className="section" aria-labelledby="loan-h">
        <h2 id="loan-h">How a loan here works</h2>
        <ol className="steps">
          <li>
            {plural(m.borrowers, "borrower")} lock up <Term k="pt" />, which the lending app values at $
            {m.price.toFixed(3)} each.
          </li>
          <li>
            Against it they borrow <Term k="usdc" />, up to {pct(m.max_ltv, 1)} of the collateral's value.{" "}
            {usd(m.debt_usd)} is borrowed in total.
          </li>
          <li>
            If the price falls and a loan crosses that limit, anyone can repay it and take the collateral at a{" "}
            {pct(m.bonus, 1)} discount. They then sell it. This is a <Term k="liquidation" />, and it keeps lenders
            whole.
          </li>
        </ol>
        <p className="measure">
          The catch is that the discount only covers a loss of {pct(m.break_even_loss, 2)} on that sale. When there are
          not enough buyers, selling the collateral brings in less than the debt to repay, so no{" "}
          <Term k="liquidator" /> steps in. The loan is <Term k="stuck">stuck</Term>, and if prices keep falling it ends
          in a <Term k="loss" />. That is why the <Term k="depth" /> above matters.
        </p>
      </section>

      <section className="section" aria-labelledby="sound-h">
        <h2 id="sound-h">What we measured</h2>
        <dl className="soundings">
          <div>
            <dt>{usd(o.capacity.low_usd)}</dt>
            <dd>
              can be sold in one go before liquidating stops paying. That is {pct(o.capacity.low_usd / m.debt_usd, 0)}{" "}
              of everything borrowed.
            </dd>
          </div>
          {bp && (
            <div>
              <dt>{pct(bp.drop, 1)}</dt>
              <dd>
                price drop is enough to put {plural(bp.loans, "loan")} up for liquidation at once, with{" "}
                {usd(bp.to_sell_usd)} of collateral to sell: more than one sale can absorb.
              </dd>
            </div>
          )}
          {e2e && (
            <div>
              <dt>{usd(e2e.repaid_usd)}</dt>
              <dd>
                of debt repaid in one real liquidation we ran start to finish on the copy, with the collateral sold for{" "}
                {usd(e2e.sold_usd)}. Only the price feed was pushed down for this test, not the market, so the gap
                between those two is wider than a real liquidator would see.
              </dd>
            </div>
          )}
          <div>
            <dt>{usd(o.aug25.repaid_usd)}</dt>
            <dd>
              was liquidated in this market on August 25, 2026, over about {minutes(o.aug25.minutes)}, by{" "}
              {plural(o.aug25.liquidators, "liquidator")}, with{" "}
              {o.aug25.bad_debt_usd > 0 ? `${usd(o.aug25.bad_debt_usd)} of losses` : "no losses"}.
            </dd>
          </div>
        </dl>
      </section>

      <WhatIf overview={o} />
      <Guard />

      <footer className="colophon">
        <h2 className="minor">How this was measured</h2>
        <p className="measure">
          Market: {m.name} on Morpho, a lending app on Ethereum. Loans and prices are read from the chain at block{" "}
          {o.measured.block.toLocaleString("en-US")}; every health check uses Morpho's own code. Sales were executed
          on a local copy of the chain, so the losses above are what really came out of each trade, not quotes. A
          liquidation costs {o.measured.gas_usd < 1 ? "less than a dollar" : `about ${usd(o.measured.gas_usd)}`} in
          network fees at the measured gas price, too little to change any result. The source and the full method are in the project repository.
        </p>
        <p className="muted">Measured {o.measured.run_utc.replace("T", " ").replace(/(\d{2})(\d{2})(\d{2})Z/, "$1:$2 UTC")}.</p>
      </footer>
    </main>
  );
}

export function App() {
  const view = useView();
  const [o, setO] = useState<Overview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.overview().then(setO, (e: Error) => setErr(e.message));
  }, []);
  useEffect(() => {
    if (view === "lab") window.scrollTo(0, 0);
  }, [view]);

  return (
    <div id="top">
      <Masthead view={view} />
      {view === "lab" ? (
        <Lab />
      ) : err ? (
        <main id="main" className="page">
          <p className="error">
            The results could not be loaded: {err}. Start the API with{" "}
            <code>uvicorn lapeg.app.api:app --port 8000</code> from the lapeg folder.
          </p>
        </main>
      ) : o ? (
        <Story o={o} />
      ) : (
        <main id="main" className="page">
          <p className="muted" role="status">
            Loading the measurements…
          </p>
        </main>
      )}
    </div>
  );
}
