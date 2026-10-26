/** Dollars in the shortest form a reader can say out loud: $940, $12k, $13.2M. */
export function usd(x: number, digits = 1): string {
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(digits)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(digits)}M`;
  if (a >= 1e4) return `$${Math.round(x / 1e3)}k`;
  if (a >= 1) return `$${Math.round(x).toLocaleString("en-US")}`;
  if (a === 0) return "$0";
  return "under $1";
}

/** A share as a percentage, with more precision for small values. */
export function pct(x: number, digits?: number): string {
  const v = x * 100;
  const d = digits ?? (Math.abs(v) < 1 ? 2 : 1);
  return `${v.toFixed(d)}%`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

export function minutes(m: number): string {
  if (m < 1) return "under a minute";
  if (m < 90) return plural(Math.round(m), "minute");
  return `${(m / 60).toFixed(1)} hours`;
}
