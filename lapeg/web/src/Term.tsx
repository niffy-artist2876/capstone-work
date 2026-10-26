import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export const GLOSSARY = {
  collateral: {
    word: "Collateral",
    text: "What a borrower locks up to get a loan. If the loan goes bad, the collateral is sold to repay it, like a pawn shop selling a pawned watch.",
  },
  pt: {
    word: "PT-reUSD",
    text: "The collateral in this market. It is a token that can be redeemed for one reUSD (a token meant to track the US dollar) on December 10, 2026, so it trades a little below a dollar until then.",
  },
  usdc: {
    word: "USDC",
    text: "A digital dollar. Borrowers here borrow USDC.",
  },
  oracle: {
    word: "Price feed",
    text: "The price the lending app trusts for the collateral. In this market it follows a 15-minute average of the trading price, so it reacts to a crash with a delay. Technically called an oracle.",
  },
  liquidation: {
    word: "Liquidation",
    text: "The lending app's safety valve. When a loan gets too close to the value of its collateral, anyone may repay it and take the collateral at a small discount. They then sell it, and the discount is their reward.",
  },
  liquidator: {
    word: "Liquidator",
    text: "Whoever performs a liquidation. Usually a trading bot that only acts when it makes a profit.",
  },
  depth: {
    word: "Market depth",
    text: "How much can be sold at once before the price gets much worse. A deep market absorbs a big sale; a shallow one drops sharply.",
  },
  stuck: {
    word: "Stuck loan",
    text: "A loan that is allowed to be liquidated, but where nobody would make money doing it, because selling the collateral would bring in less than the debt to repay. It just sits there while the risk grows.",
  },
  loss: {
    word: "Loss",
    text: "Debt that can no longer be repaid because the borrower's collateral has run out. The lenders lose this money. Technically called bad debt.",
  },
  block: {
    word: "Block",
    text: "Ethereum records transactions in batches called blocks, one every 12 seconds. Transactions waiting for the next block can be checked before they go in.",
  },
  fork: {
    word: "Live copy",
    text: "A private copy of the real Ethereum blockchain running on this computer. It starts from real data, but nothing done in it reaches the real network, and no real money is used. Technically called a fork.",
  },
} as const;

export type TermKey = keyof typeof GLOSSARY;

/** A word with a definition that opens on click or keyboard focus. */
export function Term({ k, children }: { k: TermKey; children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  const g = GLOSSARY[k];
  return (
    <span className="term" ref={ref}>
      <button type="button" className="term-word" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        {children ?? (g.word === g.word.toUpperCase() || g.word.includes("-") ? g.word : g.word.toLowerCase())}
      </button>
      {open && (
        <span role="note" id={id} className="term-note">
          <strong>{g.word}.</strong> {g.text}
        </span>
      )}
    </span>
  );
}
