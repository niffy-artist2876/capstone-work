export type DepthPoint = { usd: number; loss: number; stage: string; block: number };

export type Overview = {
  market: {
    name: string;
    maturity: string;
    borrowers: number;
    debt_usd: number;
    collateral_usd: number;
    price: number;
    max_ltv: number;
    bonus: number;
    break_even_loss: number;
  };
  measured: { block: number; run_utc: string; gas_usd: number };
  depth: DepthPoint[];
  capacity: { low_usd: number; high_usd: number };
  shocks: { drop: number; loans: number; to_sell_usd: number; backlog: number }[];
  break_point: { drop: number; loans: number; to_sell_usd: number } | null;
  end_to_end: { block: number; seized_usd: number; repaid_usd: number; sold_usd: number; gas_usd: number } | null;
  aug25: { liquidations: number; repaid_usd: number; bad_debt_usd: number; minutes: number; liquidators: number };
};

export type LoanStatus = "safe" | "rescued" | "stuck" | "loss";
export type Loan = { debt_usd: number; health: number; status: LoanStatus };

export type Outcome = {
  summary: {
    rescued: number;
    stuck: number;
    loss: number;
    safe: number;
    repaid_usd: number;
    stuck_debt_usd: number;
    bad_debt_usd: number;
    minutes: number;
    settled: boolean;
  };
  loans: Loan[];
  timeline: { minute: number; price_drop: number; sold_usd: number; rescued: number }[];
};

export type WhatIf = Outcome & { refill_half_life_min: number | null };
export type GuardAnswer = { action: "PASS" | "DELAY"; added_risk_usd: number; without: Outcome; with: Outcome };

export type LabLoan = {
  borrower: string;
  collateral_pt: number;
  debt_usd: number;
  health: number | null;
  liquidatable: boolean;
};
export type LabState =
  | { running: false }
  | {
      running: true;
      block: number;
      price: number;
      start_price: number;
      can_undo: boolean;
      history: string[];
      log: { action: string; detail: string; block: number }[];
      loans: LabLoan[];
    };

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (typeof j.detail === "string") detail = j.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  overview: () => call<Overview>("/api/overview"),
  whatif: (b: { drop?: number; sale_usd?: number; refill: number; lag: boolean }) => call<WhatIf>("/api/whatif", b),
  guard: (b: { sale_usd: number; refill: number }) => call<GuardAnswer>("/api/guard", b),
  lab: () => call<LabState>("/api/lab"),
  labStart: () => call<LabState>("/api/lab/start", {}),
  labStop: () => call<LabState>("/api/lab/stop", {}),
  labDrop: (drop: number) => call<LabState>("/api/lab/drop", { drop }),
  labWait: (minutes: number) => call<LabState>("/api/lab/wait", { minutes }),
  labUndo: () => call<LabState>("/api/lab/undo", {}),
  labSell: (amount_pt: number) =>
    call<{ result: Record<string, unknown>; state: LabState }>("/api/lab/sell", { amount_pt }),
  labLiquidate: (borrower: string) =>
    call<{ result: Record<string, unknown>; state: LabState }>("/api/lab/liquidate", { borrower }),
};
