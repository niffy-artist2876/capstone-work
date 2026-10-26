import { scaleLinear, scalePow } from "d3-scale";
import { area, curveMonotoneX, line } from "d3-shape";
import { useId, useMemo, useState } from "react";
import type { DepthPoint } from "./api";
import { pct, usd } from "./format";
import { useWidth } from "./useWidth";

type Props = {
  points: DepthPoint[];
  breakEven: number;
  capacity: { low_usd: number; high_usd: number };
};

/** Loss at a sale size, interpolated between measured sales. */
export function lossAt(points: { usd: number; loss: number }[], x: number): number {
  const pts = [{ usd: 0, loss: 0 }, ...points];
  for (let i = 1; i < pts.length; i++) {
    if (x <= pts[i].usd) {
      const a = pts[i - 1];
      const b = pts[i];
      return a.loss + ((b.loss - a.loss) * (x - a.usd)) / (b.usd - a.usd);
    }
  }
  return pts[pts.length - 1].loss;
}

/**
 * The measured market depth drawn as a seabed profile: sale size runs across, price lost runs down.
 * The shelf is where buyers absorb the sale; the drop-off is where they run out.
 */
export function DepthProfile({ points, breakEven, capacity }: Props) {
  const [box, width] = useWidth<HTMLDivElement>();
  const narrow = width < 560;
  const height = narrow ? 300 : 400;
  const m = { top: 28, right: narrow ? 12 : 24, bottom: 44, left: narrow ? 44 : 56 };
  const maxUsd = points[points.length - 1]?.usd ?? 30e6;
  const [size, setSize] = useState(5e6);
  const patternId = useId();
  const gradId = useId();

  const x = useMemo(
    () => scaleLinear().domain([0, maxUsd]).range([m.left, width - m.right]),
    [maxUsd, width, m.left, m.right],
  );
  // square-root depth: small losses stay readable, the drop-off still plunges
  const y = useMemo(() => scalePow().exponent(0.5).domain([0, 0.6]).range([m.top, height - m.bottom]), [height, m.top, m.bottom]);
  const data = useMemo(() => [{ usd: 0, loss: 0 }, ...points], [points]);
  const seabed = area<{ usd: number; loss: number }>()
    .x((d) => x(d.usd))
    .y0(height - m.bottom)
    .y1((d) => y(d.loss))
    .curve(curveMonotoneX)(data);
  const water = area<{ usd: number; loss: number }>()
    .x((d) => x(d.usd))
    .y0(m.top)
    .y1((d) => y(d.loss))
    .curve(curveMonotoneX)(data);
  const floor = line<{ usd: number; loss: number }>()
    .x((d) => x(d.usd))
    .y((d) => y(d.loss))
    .curve(curveMonotoneX)(data);

  const loss = lossAt(points, size);
  const xTicks = (narrow ? [0, 10e6, 20e6, 30e6] : [0, 5e6, 10e6, 15e6, 20e6, 25e6, 30e6]).filter((t) => t <= maxUsd * 1.02);
  const yTicks = [0.005, breakEven, 0.1, 0.3, 0.5];
  const cliffX = x(capacity.high_usd);

  return (
    <figure className="profile">
      <div ref={box} className="profile-box">
        <svg width={width} height={height} role="img" aria-labelledby={`${gradId}-t`}>
          <title id={`${gradId}-t`}>
            Price lost against amount sold at once. The loss stays above the break-even line up to{" "}
            {usd(capacity.low_usd)}, crosses it by {usd(capacity.high_usd)}, and reaches{" "}
            {pct(points[points.length - 1]?.loss ?? 0, 0)} at {usd(maxUsd)}.
          </title>
          <defs>
            <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0" stopColor="var(--shallows)" />
              <stop offset="1" stopColor="var(--deep)" stopOpacity="0.55" />
            </linearGradient>
            <pattern id={patternId} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(35)">
              <line x1="0" y1="0" x2="0" y2="7" stroke="var(--seabed-line)" strokeWidth="1" />
            </pattern>
          </defs>

          <path d={water ?? ""} fill={`url(#${gradId})`} />
          <path d={seabed ?? ""} fill="var(--seabed)" />
          <path d={seabed ?? ""} fill={`url(#${patternId})`} />
          <path d={floor ?? ""} className="profile-floor" pathLength={1} />

          {/* surface: the listed price */}
          <line x1={m.left} x2={width - m.right} y1={m.top} y2={m.top} className="profile-surface" />
          <text x={m.left + 6} y={m.top - 9} className="profile-label">
            Listed price (no loss)
          </text>

          {/* break-even depth */}
          <line x1={m.left} x2={width - m.right} y1={y(breakEven)} y2={y(breakEven)} className="profile-breakeven" />
          <text x={width - m.right - 6} y={y(breakEven) - 7} textAnchor="end" className="profile-label profile-label-deep">
            {narrow ? "Liquidators' break-even" : "Below this line, a liquidator loses money on the sale"}
          </text>

          {/* the drop-off */}
          <line x1={cliffX} x2={cliffX} y1={m.top} y2={height - m.bottom} className="profile-hazard" />
          <text x={cliffX - 8} y={y(0.2)} textAnchor="end" className="profile-label profile-label-hazard">
            Drop-off at {usd(capacity.high_usd)}
          </text>

          {/* sounding line */}
          <line x1={x(size)} x2={x(size)} y1={m.top} y2={y(loss)} className="profile-sounding" />
          <circle cx={x(size)} cy={y(loss)} r={5} className="profile-lead" />

          {xTicks.map((t) => (
            <text key={t} x={x(t)} y={height - m.bottom + 20} textAnchor="middle" className="profile-tick">
              {t === 0 ? "$0" : usd(t, 0)}
            </text>
          ))}
          {yTicks.map((t) => (
            <text key={t} x={m.left - 8} y={y(t) + 4} textAnchor="end" className="profile-tick">
              {pct(t, t < 0.01 ? 1 : t === breakEven ? 2 : 0)}
            </text>
          ))}
          <text x={(m.left + width - m.right) / 2} y={height - 6} textAnchor="middle" className="profile-axis">
            Amount sold in one go
          </text>
          {!narrow && (
            <text transform={`translate(12 ${(m.top + height - m.bottom) / 2}) rotate(-90)`} textAnchor="middle" className="profile-axis">
              Price lost
            </text>
          )}
        </svg>
      </div>
      <div className="sounding">
        <label htmlFor={`${patternId}-r`}>Drop a sounding line: sell</label>
        <output htmlFor={`${patternId}-r`} className="sounding-value">
          {usd(size)}
        </output>
        <input
          id={`${patternId}-r`}
          type="range"
          min={0.1e6}
          max={maxUsd}
          step={0.1e6}
          value={size}
          onChange={(e) => setSize(Number(e.target.value))}
        />
        <p className="sounding-read" aria-live="polite">
          Selling {usd(size)} at once gets {pct(loss)} less than the listed price
          {loss * size >= 1 ? `, about ${usd(loss * size)} lost` : ""}.{" "}
          {loss < breakEven
            ? "Still above the break-even line: liquidating a loan this size pays."
            : "Below the break-even line: no liquidator will take a loan this size."}
        </p>
      </div>
      <figcaption>
        Each point is a real sale, executed on a copy of Ethereum at block{" "}
        {[...new Set(points.map((p) => p.block))].sort().join(", ")}, through the best route Pendle's trading API
        offered at the time.
      </figcaption>
    </figure>
  );
}
