import { scaleLinear } from "d3-scale";
import { curveStepAfter, line } from "d3-shape";
import type { Outcome } from "./api";
import { pct } from "./format";
import { useWidth } from "./useWidth";

/** The price the lending app sees over the simulated hour, as a drop below today's price. */
export function PriceTrace({ timeline }: { timeline: Outcome["timeline"] }) {
  const [box, width] = useWidth<HTMLDivElement>(480);
  const height = 150;
  const m = { top: 12, right: 10, bottom: 26, left: 44 };
  const maxDrop = Math.max(0.01, ...timeline.map((t) => t.price_drop));
  const x = scaleLinear().domain([0, 60]).range([m.left, width - m.right]);
  const y = scaleLinear().domain([0, maxDrop]).nice(4).range([m.top, height - m.bottom]);
  const last = timeline[timeline.length - 1];
  const pts = last && last.minute < 60 ? [...timeline, { ...last, minute: 60 }] : timeline;
  const path = line<(typeof pts)[number]>()
    .x((d) => x(d.minute))
    .y((d) => y(d.price_drop))
    .curve(curveStepAfter)(pts);
  return (
    <div ref={box} className="trace">
      <svg width={width} height={height} role="img" aria-label={`Price feed over the next hour, ending ${pct(last?.price_drop ?? 0)} below today`}>
        {y.ticks(4).map((t) => (
          <g key={t}>
            <line x1={m.left} x2={width - m.right} y1={y(t)} y2={y(t)} className="trace-grid" />
            <text x={m.left - 6} y={y(t) + 4} textAnchor="end" className="profile-tick">
              {t === 0 ? "today" : `−${pct(t, t < 0.1 ? 1 : 0)}`}
            </text>
          </g>
        ))}
        {[0, 15, 30, 45, 60].map((t) => (
          <text key={t} x={x(t)} y={height - 8} textAnchor={t === 60 ? "end" : "middle"} className="profile-tick">
            {t === 0 ? "now" : `${t} min`}
          </text>
        ))}
        <path d={path ?? ""} className="trace-line" />
      </svg>
    </div>
  );
}
