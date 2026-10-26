import { useEffect, useId, useState } from "react";
import { api, type LabState } from "./api";
import { pct, usd } from "./format";
import { Term } from "./Term";

type Busy = { what: string } | null;

function num(x: unknown): number {
  return typeof x === "number" ? x : 0;
}

/** A sentence for the outcome of a sale or a liquidation on the live copy. */
function describe(kind: "sell" | "liquidate", r: Record<string, unknown>): string {
  if (r.status !== "ok") return `It did not go through: ${String(r.reason ?? r.status)}.`;
  if (kind === "sell") {
    const g = num(r.g);
    const vs =
      g >= 0
        ? `${pct(g)} below the price feed`
        : `${pct(-g)} above the price feed, because the feed was pushed down and the market was not`;
    return `Sold ${num(r.pt_sold).toLocaleString("en-US")} PT for ${usd(num(r.value_out_usdc))}, ${vs}.`;
  }
  const profit = num(r.profit_usdc);
  return `You repaid ${usd(num(r.repaid_usdc))}, took ${num(r.seized_pt).toLocaleString("en-US", { maximumFractionDigits: 0 })} PT and sold it. ${
    profit >= 0 ? `Profit: ${usd(profit)}.` : `Loss: ${usd(-profit)}. A real liquidator would not have done this.`
  }`;
}

export function Lab() {
  const id = useId();
  const [state, setState] = useState<LabState | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [drop, setDrop] = useState(5);
  const [wait, setWait] = useState(15);
  const [sell, setSell] = useState(1_000_000);
  const [all, setAll] = useState(false);

  useEffect(() => {
    api.lab().then(setState, (e: Error) => setErr(e.message));
  }, []);

  async function run<T>(what: string, f: () => Promise<T>, after?: (r: T) => void) {
    setBusy({ what });
    setErr(null);
    setMsg(null);
    try {
      const r = await f();
      after?.(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const running = state?.running === true;
  const loans = running ? state.loans : [];
  const shown = all ? loans : loans.slice(0, 25);

  return (
    <main id="main" className="page lab">
      <h1>Live lab</h1>
      <p className="lede">
        Here you act on a <Term k="fork">live copy</Term> of Ethereum running on this computer, starting from the latest
        block. Every number comes from the lending app's real code. Nothing you do reaches the real network, and no
        real money is involved.
      </p>

      {!running && (
        <div className="lab-start">
          <button type="button" className="primary" disabled={!!busy} onClick={() => run("start", api.labStart, setState)}>
            Start the live copy
          </button>
          <p className="hint">
            Starting downloads the market's current state, which takes about a minute. It needs the archive RPC address
            in lapeg/.env.
          </p>
        </div>
      )}

      {busy && (
        <p className="busy" role="status">
          {busy.what === "start" ? "Starting the live copy…" : "Working on the live copy…"} The first actions fetch data
          from the network and can take a while.
        </p>
      )}
      {err && <p className="error">{err}</p>}
      {msg && (
        <p className="notice" role="status">
          {msg}
        </p>
      )}

      {running && (
        <>
          <dl className="lab-facts">
            <div>
              <dt>Block</dt>
              <dd>{state.block.toLocaleString("en-US")}</dd>
            </div>
            <div>
              <dt>Price feed</dt>
              <dd>
                ${state.price.toFixed(4)}{" "}
                {state.price !== state.start_price && <span className="muted">({pct(state.price / state.start_price - 1)} from start)</span>}
              </dd>
            </div>
            <div>
              <dt>Loans that can be liquidated</dt>
              <dd>{loans.filter((l) => l.liquidatable).length}</dd>
            </div>
          </dl>

          <div className="lab-actions">
            <form onSubmit={(e) => (e.preventDefault(), run("drop", () => api.labDrop(drop / 100), setState))}>
              <label htmlFor={`${id}-drop`}>Push the price feed down by</label>
              <span className="inline">
                <input id={`${id}-drop`} type="number" min={0} max={50} step={0.5} value={drop} onChange={(e) => setDrop(Number(e.target.value))} />
                %
              </span>
              <button type="submit" disabled={!!busy}>
                Lower the price
              </button>
            </form>
            <form onSubmit={(e) => (e.preventDefault(), run("wait", () => api.labWait(wait), setState))}>
              <label htmlFor={`${id}-wait`}>Skip ahead</label>
              <span className="inline">
                <input id={`${id}-wait`} type="number" min={1} max={1440} value={wait} onChange={(e) => setWait(Number(e.target.value))} />
                minutes
              </span>
              <button type="submit" disabled={!!busy}>
                Skip ahead
              </button>
            </form>
            <form
              onSubmit={(e) => (
                e.preventDefault(),
                run(
                  "sell",
                  () => api.labSell(sell),
                  (r) => (setState(r.state), setMsg(describe("sell", r.result))),
                )
              )}
            >
              <label htmlFor={`${id}-sell`}>Sell PT on Pendle's market</label>
              <span className="inline">
                <input id={`${id}-sell`} type="number" min={1} step={1000} value={sell} onChange={(e) => setSell(Number(e.target.value))} />
                PT
              </span>
              <button type="submit" disabled={!!busy}>
                Sell
              </button>
            </form>
            <div className="lab-undo">
              <button type="button" disabled={!!busy || !state.can_undo} onClick={() => run("undo", api.labUndo, setState)}>
                Undo last step
              </button>
              <button type="button" className="quiet" disabled={!!busy} onClick={() => run("stop", api.labStop, setState)}>
                Stop the live copy
              </button>
            </div>
          </div>

          <h2 className="minor">Loans, closest to liquidation first</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Borrower</th>
                  <th scope="col" className="num">
                    Borrowed
                  </th>
                  <th scope="col" className="num">
                    Collateral
                  </th>
                  <th scope="col">Room before liquidation</th>
                  <th scope="col">
                    <span className="sr-only">Action</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.map((l) => {
                  const room = l.health ? Math.max(0, 1 - 1 / l.health) : 1;
                  return (
                    <tr key={l.borrower} className={l.liquidatable ? "row-hot" : undefined}>
                      <td className="addr" title={l.borrower}>
                        {l.borrower.slice(0, 6)}…{l.borrower.slice(-4)}
                      </td>
                      <td className="num">{usd(l.debt_usd)}</td>
                      <td className="num">{l.collateral_pt.toLocaleString("en-US", { maximumFractionDigits: 0 })} PT</td>
                      <td>
                        <span className="room">
                          <span className="room-bar" style={{ width: `${Math.min(100, room * 1000)}%` }} />
                        </span>
                        {l.liquidatable
                          ? "None: can be liquidated"
                          : room < 0.0001
                            ? "Under 0.01% price drop"
                            : `${pct(room)} price drop`}
                      </td>
                      <td>
                        {l.liquidatable && (
                          <button
                            type="button"
                            disabled={!!busy}
                            onClick={() =>
                              run(
                                "liquidate",
                                () => api.labLiquidate(l.borrower),
                                (r) => (setState(r.state), setMsg(describe("liquidate", r.result))),
                              )
                            }
                          >
                            Liquidate
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {loans.length > 25 && (
            <button type="button" className="quiet" onClick={() => setAll(!all)}>
              {all ? "Show the first 25" : `Show all ${loans.length} loans`}
            </button>
          )}

          {state.log.length > 0 && (
            <>
              <h2 className="minor">What you did</h2>
              <ol className="log">
                {[...state.log].reverse().map((e, i) => (
                  <li key={i}>
                    <span className="muted">block {e.block.toLocaleString("en-US")}</span> {e.detail}
                  </li>
                ))}
              </ol>
            </>
          )}
        </>
      )}
    </main>
  );
}
