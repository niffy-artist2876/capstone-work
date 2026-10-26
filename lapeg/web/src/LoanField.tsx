import type { Loan, LoanStatus } from "./api";
import { pct, usd } from "./format";

export const STATUS_TEXT: Record<LoanStatus, string> = {
  safe: "Safe",
  rescued: "Liquidated in time",
  stuck: "Stuck",
  loss: "Ended in a loss",
};

/** One dot per loan, riskiest first, area proportional to the debt. */
export function LoanField({ loans, label }: { loans: Loan[]; label: string }) {
  const maxDebt = Math.max(...loans.map((l) => l.debt_usd), 1);
  return (
    <div className="field" role="img" aria-label={label}>
      {loans.map((l, i) => {
        const d = 4 + 26 * Math.sqrt(l.debt_usd / maxDebt);
        const room = 1 - 1 / l.health;
        return (
          <span
            key={i}
            className={`dot dot-${l.status}`}
            style={{ width: d, height: d }}
            title={`Loan ${i + 1}: ${usd(l.debt_usd)} borrowed. ${
              room > 0 ? `Can be liquidated after a ${pct(room)} price drop.` : "Can be liquidated now."
            } ${STATUS_TEXT[l.status]}.`}
          />
        );
      })}
    </div>
  );
}

export function FieldKey({ counts }: { counts?: Partial<Record<LoanStatus, number>> }) {
  return (
    <ul className="field-key">
      {(Object.keys(STATUS_TEXT) as LoanStatus[]).map((s) => (
        <li key={s}>
          <span className={`dot dot-${s}`} aria-hidden="true" />
          {STATUS_TEXT[s]}
          {counts?.[s] !== undefined && <span className="field-count">{counts[s]}</span>}
        </li>
      ))}
    </ul>
  );
}
