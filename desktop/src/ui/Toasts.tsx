// Стопка уведомлений в правом верхнем углу. Каждое въезжает справа,
// показывает полосу оставшегося времени и уходит само (наведение - пауза).

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { IconButton } from "./Button";
import { Icon } from "./Icon";
import { alpha, tone, useMotionPrefs } from "./theme";

interface Toast {
  id: number;
  kind: string;
  title: string;
  message: string;
  life: number;
}

const MAX_VISIBLE = 4;
let toasts: Toast[] = [];
let seq = 0;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

export function toast(kind: string, title: string, message = "") {
  const life = kind === "error" || kind === "warning" ? 7000 : 4500;
  toasts = [...toasts, { id: ++seq, kind: kind || "info", title, message, life }].slice(-MAX_VISIBLE);
  emit();
}

function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export function ToastHost() {
  const items = useSyncExternalStore(subscribe, () => toasts);
  const m = useMotionPrefs();
  return (
    <div className="toasts">
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <motion.div
            key={item.id}
            layout={m.on ? "position" : false}
            initial={{ opacity: 0, x: 420 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 420, transition: m.ease(220) }}
            transition={m.rich ? { type: "spring", stiffness: 320, damping: 28 } : m.ease(420)}
          >
            <ToastCard item={item} />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

function ToastCard({ item }: { item: Toast }) {
  const color = tone(item.kind === "info" ? "accent" : item.kind);
  const glyph = item.kind === "success" ? "circle-check" : item.kind === "error" ? "octagon-x"
    : item.kind === "warning" ? "triangle-alert" : "info";
  const [paused, setPaused] = useState(false);
  const left = useRef(item.life);
  const started = useRef(Date.now());

  useEffect(() => {
    if (paused) return;
    started.current = Date.now();
    const timer = window.setTimeout(() => dismiss(item.id), left.current);
    return () => {
      window.clearTimeout(timer);
      left.current -= Date.now() - started.current;
    };
  }, [paused, item.id]);

  return (
    <div
      className="toast"
      style={{ borderColor: alpha(color, 0.45) }}
      onPointerEnter={() => setPaused(true)}
      onPointerLeave={() => setPaused(false)}
    >
      <div className="toast-icon" style={{ background: alpha(color, 0.16), color }}>
        <Icon name={glyph} size={16} />
      </div>
      <div className="grow">
        <div className="semi wrap">{item.title}</div>
        {item.message && <div className="small dim clamp3">{item.message}</div>}
      </div>
      <IconButton icon="x" size={26} onClick={() => dismiss(item.id)} />
      <span
        className="toast-bar"
        style={{
          background: color,
          animationDuration: `${item.life}ms`,
          animationPlayState: paused ? "paused" : "running",
        }}
      />
    </div>
  );
}
