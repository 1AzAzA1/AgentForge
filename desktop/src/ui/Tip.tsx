// Подсказка в стиле темы: появляется с задержкой и "всплывает" над элементом.
// Рисуется поверх всего окна, поэтому её не обрезают карточки и прокрутка.

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMotionPrefs } from "./theme";

const DELAY = 450;

export function useTip(text: string, enabled = true) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = (el: HTMLElement) => {
    if (!text || !enabled) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setAnchor(el.getBoundingClientRect()), DELAY);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setAnchor(null);
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!enabled) hide();
  }, [enabled]);
  const node = <TipBubble anchor={enabled ? anchor : null} text={text} />;
  return {
    node,
    bind: {
      onPointerEnter: (e: React.PointerEvent<HTMLElement>) => show(e.currentTarget),
      onPointerLeave: hide,
      onPointerDown: hide,
    },
  };
}

function TipBubble({ anchor, text }: { anchor: DOMRect | null; text: string }) {
  const motion_ = useMotionPrefs();
  return createPortal(
    <AnimatePresence>
      {anchor && text && (
        <motion.div
          className="tip"
          style={{ left: anchor.left + anchor.width / 2, top: anchor.top - 8 }}
          initial={{ opacity: 0, scale: 0.92, x: "-50%", y: "-100%" }}
          animate={{ opacity: 1, scale: 1, x: "-50%", y: "-100%" }}
          exit={{ opacity: 0, x: "-50%", y: "-100%" }}
          transition={motion_.ease(140)}
        >
          {text}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

/** Обёртка для произвольного содержимого: <Tip text="...">...</Tip> */
export function Tip({ text, children }: { text: string; children: ReactNode }) {
  const tip = useTip(text);
  return (
    <span className="tip-anchor" {...tip.bind}>
      {children}
      {tip.node}
    </span>
  );
}
