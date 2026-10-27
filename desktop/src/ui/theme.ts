// Цвета и движение для кода. Сами токены лежат в styles/theme.css.

import { useMemo } from "react";
import type { Transition } from "motion/react";
import { useCtl } from "../api/hooks";

export const C = {
  bg: "var(--bg)",
  text: "var(--text)",
  textDim: "var(--text-dim)",
  textMute: "var(--text-mute)",
  violet: "var(--violet)",
  violetSoft: "var(--violet-soft)",
  indigo: "var(--indigo)",
  teal: "var(--teal)",
  cyan: "var(--cyan)",
  magenta: "var(--magenta)",
  success: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  info: "var(--info)",
};

export function tone(name: string): string {
  switch (name) {
    case "success": return C.success;
    case "warning": return C.warning;
    case "error":
    case "danger": return C.danger;
    case "info": return C.info;
    case "accent":
    case "tool": return C.cyan;
    case "violet": return C.violetSoft;
    case "muted": return C.textMute;
    default: return C.textDim;
  }
}

export function statusColor(status: string): string {
  switch (status) {
    case "running": return C.cyan;
    case "done": return C.success;
    case "review": return C.violetSoft;
    case "rework": return C.warning;
    case "paused":
    case "stopped": return "#F59E0B";
    case "error":
    case "failed": return C.danger;
    default: return C.textMute;
  }
}

/** Цвет с прозрачностью: alpha("var(--violet)", 0.2). */
export function alpha(color: string, a: number): string {
  return `color-mix(in srgb, ${color} ${Math.round(a * 100)}%, transparent)`;
}

export type MotionLevel = "full" | "reduced" | "off";

export interface MotionPrefs {
  level: MotionLevel;
  /** полное движение: свет под курсором, дрейф фона, каскады */
  rich: boolean;
  /** хоть какое-то движение */
  on: boolean;
  /** длительность с учётом уровня, в секундах */
  dur(ms: number): number;
  /** упругий отклик на действие человека */
  spring: Transition;
  /** мягкая пружина для крупных перемещений */
  springSoft: Transition;
  ease(ms: number): Transition;
}

const NONE: Transition = { duration: 0 };

export function makeMotion(level: MotionLevel): MotionPrefs {
  const on = level !== "off";
  const rich = level === "full";
  const dur = (ms: number) => (level === "off" ? 0 : level === "reduced" ? (ms * 0.6) / 1000 : ms / 1000);
  return {
    level,
    rich,
    on,
    dur,
    // При полном движении пружина слегка перелетает цель, при сдержанном
    // останавливается без колебаний.
    spring: on ? { type: "spring", stiffness: 520, damping: rich ? 17 : 40, mass: 1 } : NONE,
    springSoft: on ? { type: "spring", stiffness: 340, damping: rich ? 26 : 44 } : NONE,
    ease: (ms: number) => (on ? { duration: dur(ms), ease: [0.22, 1, 0.36, 1] } : NONE),
  };
}

export function useMotionPrefs(): MotionPrefs {
  const level = (useCtl("backend").motion ?? "full") as MotionLevel;
  return useMemo(() => makeMotion(level), [level]);
}
