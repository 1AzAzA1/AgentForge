// Графика: живой фон, знак приложения, кольцо прогресса, бегущие числа,
// кривая расхода, полосы по агентам и по статусам.

import { animate, motion } from "motion/react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { statusColor, useMotionPrefs } from "./theme";

/** Живой фон: крупные размытые "сияния" медленно дрейфуют под интерфейсом.
 *  На сдержанном уровне движения замирают, оставаясь мягким градиентом. */
export function Aurora({ intensity = 1 }: { intensity?: number }) {
  return (
    <div className="aurora" style={{ opacity: intensity }} aria-hidden>
      <span className="aurora-glow rich-only" style={{ ["--c" as string]: "var(--violet)", ["--a" as string]: "0.34", width: 1040, height: 1040, left: "18%", top: "10%", animationDuration: "26s" }} />
      <span className="aurora-glow rich-only" style={{ ["--c" as string]: "var(--teal)", ["--a" as string]: "0.2", width: 920, height: 920, left: "92%", top: "22%", animationDuration: "30s", animationDirection: "reverse" }} />
      <span className="aurora-glow rich-only" style={{ ["--c" as string]: "var(--cyan)", ["--a" as string]: "0.14", width: 840, height: 840, left: "70%", top: "98%", animationDuration: "34s" }} />
      <span className="aurora-glow rich-only" style={{ ["--c" as string]: "var(--magenta)", ["--a" as string]: "0.12", width: 760, height: 760, left: "5%", top: "95%", animationDuration: "38s", animationDirection: "reverse" }} />
      <span className="aurora-vignette" />
    </div>
  );
}

/** Знак приложения: центральный узел и спутники-агенты на орбитах. */
export function OrbitLogo({ size = 40, speed = 1 }: { size?: number; speed?: number }) {
  const r1 = 30;
  const r2 = 44;
  const sat = (orbit: number, phase: number, color: string, dot: number, period: number) => (
    <g className="orbit-sat rich-only" style={{ animationDuration: `${period / speed}ms`, ["--phase" as string]: `${phase}deg` }}>
      <circle cx={50 + orbit} cy={50} r={dot / 2} fill={color} />
    </g>
  );
  return (
    <svg className="orbit-logo" width={size} height={size} viewBox="0 0 100 100" aria-hidden>
      <defs>
        <linearGradient id="orbit-core" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--violet-soft)" />
          <stop offset="1" stopColor="var(--indigo)" />
        </linearGradient>
      </defs>
      <circle cx="50" cy="50" r={r1} fill="none" stroke="var(--violet-soft)" strokeOpacity="0.28" strokeWidth="2" strokeDasharray="2 3" />
      <circle cx="50" cy="50" r={r2} fill="none" stroke="var(--cyan)" strokeOpacity="0.22" strokeWidth="2" />
      <circle cx="50" cy="50" r="28.5" fill="none" stroke="var(--violet)" strokeOpacity="0.25" strokeWidth="2.5" />
      <circle cx="50" cy="50" r="15" fill="url(#orbit-core)" />
      {sat(r1, 20, "var(--teal)", 12, 7000)}
      {sat(r2, 150, "var(--cyan)", 10, 11000)}
      {sat(r2, 270, "var(--magenta)", 9, 13000)}
    </svg>
  );
}

function mix(a: number[], b: number[], t: number) {
  t = Math.max(0, Math.min(1, t));
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(" ")})`;
}

/** Кольцевой индикатор: дуга дорастает до значения, цвет по пути переходит
 *  от фиолетового к сине-зелёному. */
export function ProgressRing({ value, size = 64, thickness = 6, label = "" }: { value: number; size?: number; thickness?: number; label?: string }) {
  const m = useMotionPrefs();
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  const v = Math.max(0, Math.min(1, value || 0));
  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface3)" strokeWidth={thickness} />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          strokeWidth={thickness}
          strokeLinecap="round"
          strokeDasharray={c}
          initial={false}
          animate={{ strokeDashoffset: c * (1 - v), stroke: v > 0.002 ? mix([139, 92, 246], [45, 212, 191], v) : "rgb(0 0 0 / 0)" }}
          transition={m.ease(840)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <span className="ring-label bold">{label}</span>
    </div>
  );
}

export function formatUnit(v: number, unit: string): string {
  if (unit === "usd") {
    if (v >= 100) return "$" + Math.round(v);
    if (v >= 1) return "$" + v.toFixed(2);
    if (v === 0) return "$0";
    return "$" + v.toFixed(v >= 0.01 ? 3 : 4);
  }
  if (unit === "tokens") {
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e4) return (v / 1e3).toFixed(1) + "k";
    return Math.round(v).toLocaleString("ru-RU");
  }
  if (unit === "percent") return Math.round(v * 100) + "%";
  return String(Math.round(v));
}

/** Число, которое "досчитывает" до нового значения. */
export function Ticker({ value, unit = "int", total = "", className = "", style }: {
  value: number; unit?: string; total?: string; className?: string; style?: React.CSSProperties;
}) {
  const m = useMotionPrefs();
  const [shown, setShown] = useState(value);
  const last = useRef(value);
  useEffect(() => {
    const from = last.current;
    last.current = value;
    if (!m.on || from === value) {
      setShown(value);
      return;
    }
    const controls = animate(from, value, { duration: m.dur(840), ease: [0.22, 1, 0.36, 1], onUpdate: setShown });
    return () => controls.stop();
  }, [value, m]);
  return (
    <span className={className} style={style}>
      {formatUnit(shown, unit)}
      {total ? ` / ${total}` : ""}
    </span>
  );
}

function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setSize({ w: entry.contentRect.width, h: entry.contentRect.height }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

interface Point {
  label: string;
  value: number;
}

/** Кривая нарастающего итога: градиентная заливка, прорисовка слева направо
 *  при обновлении и перекрестие с подсказкой при наведении. */
export function LineChart({ points, unit, color, fill, emptyText }: {
  points: Point[]; unit: string; color: string; fill: string; emptyText: string;
}) {
  const m = useMotionPrefs();
  const [ref, { w, h }] = useSize<HTMLDivElement>();
  const [hover, setHover] = useState(-1);
  const n = points.length;
  const max = Math.max(0, ...points.map((p) => p.value));
  const top = max > 0 ? max * 1.12 : 1;
  const padL = 8;
  const padB = 18;
  const px = (i: number) => padL + (n > 1 ? i / (n - 1) : 0.5) * (w - padL * 2);
  const py = (v: number) => h - padB - (v / top) * (h - padB - 10);
  const line = points.map((p, i) => `${i ? "L" : "M"}${px(i).toFixed(1)},${py(p.value).toFixed(1)}`).join(" ");
  const area = n > 1 ? `${line} L${px(n - 1).toFixed(1)},${h - padB} L${px(0).toFixed(1)},${h - padB} Z` : "";
  const gid = `lc-${unit}`;
  const revealKey = `${n}-${max}`;

  return (
    <div
      ref={ref}
      className="linechart"
      onPointerMove={(e) => {
        if (n < 2) return;
        const r = e.currentTarget.getBoundingClientRect();
        const t = (e.clientX - r.left - padL) / (r.width - padL * 2);
        setHover(Math.max(0, Math.min(n - 1, Math.round(t * (n - 1)))));
      }}
      onPointerLeave={() => setHover(-1)}
    >
      {w > 0 && (
        <svg width={w} height={h}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor={fill} stopOpacity="0.35" />
              <stop offset="1" stopColor={fill} stopOpacity="0" />
            </linearGradient>
            <clipPath id={`${gid}-clip`}>
              <motion.rect
                key={revealKey}
                x={0}
                y={0}
                height={h}
                initial={{ width: m.rich && n > 1 ? 0 : w }}
                animate={{ width: w }}
                transition={m.rich ? { duration: 0.9, ease: [0.22, 1, 0.36, 1] } : { duration: 0 }}
              />
            </clipPath>
          </defs>
          {[0, 1, 2, 3].map((g) => (
            <line key={g} x1={padL} x2={w - padL} y1={py((max * g) / 3)} y2={py((max * g) / 3)} stroke="rgb(255 255 255 / 0.05)" />
          ))}
          {n > 1 && (
            <g clipPath={`url(#${gid}-clip)`}>
              <path d={area} fill={`url(#${gid})`} />
              <path d={line} fill="none" stroke={color} strokeWidth={2.2} strokeLinejoin="round" />
            </g>
          )}
          {hover >= 0 && n > 1 && (
            <>
              <line x1={px(hover)} x2={px(hover)} y1={0} y2={h - padB} stroke="rgb(238 235 250 / 0.18)" />
              <circle cx={px(hover)} cy={py(points[hover].value)} r={5} fill={color} stroke="var(--bg)" strokeWidth={2} />
            </>
          )}
        </svg>
      )}
      {n > 1 ? (
        <>
          <span className="lc-max micro mute mono">{formatUnit(max, unit)}</span>
          <span className="lc-first micro mute">{points[0].label}</span>
          <span className="lc-last micro mute">{points[n - 1].label}</span>
          {hover >= 0 && (
            <span className="lc-tip small" style={{ left: Math.min(Math.max(0, px(hover) - 70), w - 140) }}>
              {points[hover].label} · {formatUnit(points[hover].value, unit)}
            </span>
          )}
        </>
      ) : (
        <span className="lc-empty mute">{emptyText}</span>
      )}
    </div>
  );
}

interface Bar {
  label: string;
  value: number;
  text: string;
  cost: string;
  supervisor: boolean;
}

/** Горизонтальные полосы "кто сколько потратил"; полосы дорастают плавно. */
export function BarList({ bars, emptyText }: { bars: Bar[]; emptyText: string }) {
  const m = useMotionPrefs();
  const max = Math.max(1, ...bars.map((b) => b.value));
  if (!bars.length) return <div className="mute center-text">{emptyText}</div>;
  return (
    <div className="barlist">
      {bars.map((b, i) => (
        <div key={`${b.label}-${i}`} className="col" style={{ gap: 5 }}>
          <div className="row small">
            <Icon name={b.supervisor ? "shield-check" : "bot"} size={13} color={b.supervisor ? "var(--violet-soft)" : "var(--text-mute)"} />
            <span className="grow ellipsis">{b.label}</span>
            <span className="mono dim">{b.text}</span>
            <span className="mono mute">{b.cost}</span>
          </div>
          <div className="track" style={{ height: 8 }}>
            <motion.div
              className="track-fill"
              style={{
                background: b.supervisor
                  ? "linear-gradient(90deg, var(--magenta), var(--violet-soft))"
                  : "linear-gradient(90deg, var(--violet), var(--cyan))",
              }}
              initial={{ width: 0 }}
              animate={{ width: `${(b.value / max) * 100}%` }}
              transition={m.ease(840)}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Полоса прогресса по статусам с легендой под ней. */
export function SegmentBar({ segments }: { segments: { status: string; title: string; count: number }[] }) {
  const m = useMotionPrefs();
  const total = segments.reduce((s, x) => s + x.count, 0);
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="track segbar" style={{ height: 10 }}>
        {segments.map((s) => (
          <motion.div
            key={s.status}
            className="segbar-part"
            style={{ background: statusColor(s.status) }}
            initial={{ width: 0 }}
            animate={{ width: total ? `${(s.count / total) * 100}%` : 0 }}
            transition={m.ease(840)}
          />
        ))}
      </div>
      {segments.length > 0 && (
        <div className="wrap-row" style={{ gap: 14 }}>
          {segments.map((s) => (
            <span key={s.status} className="row small dim" style={{ gap: 6 }}>
              <span className="dot" style={{ background: statusColor(s.status) }} />
              {s.title} · {s.count}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
