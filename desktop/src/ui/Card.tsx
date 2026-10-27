// "Стеклянная" карточка. hoverable: при наведении приподнимается и подсвечивает
// контур; glow: постоянное свечение (активный элемент); stagger: номер в
// списке для каскадного появления.

import { motion } from "motion/react";
import type { CSSProperties, ReactNode } from "react";
import { alpha, useMotionPrefs } from "./theme";

interface CardProps {
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
  padding?: number;
  hoverable?: boolean;
  glow?: boolean;
  glowColor?: string;
  stagger?: number;
  dimmed?: boolean;
  onClick?: () => void;
}

export function Card({
  children, className = "", style, padding, hoverable = false, glow = false,
  glowColor = "var(--primary)", stagger = -1, dimmed = false, onClick,
}: CardProps) {
  const m = useMotionPrefs();
  const enter = stagger >= 0 && m.on;
  const delay = m.rich ? Math.min(Math.max(stagger, 0), 12) * 0.045 : 0;
  return (
    <motion.div
      className={["card", hoverable && "card-hover", glow && "card-glow", onClick && "card-click", className].filter(Boolean).join(" ")}
      style={{
        padding,
        ...(glow ? { borderColor: alpha(glowColor, 0.55), boxShadow: `0 0 0 4px ${alpha(glowColor, 0.12)}` } : null),
        ...style,
      }}
      initial={enter ? { opacity: 0, y: 16 } : false}
      animate={{ opacity: dimmed ? 0.55 : 1, y: 0 }}
      transition={{ ...m.ease(420), delay }}
      onClick={onClick}
    >
      {children}
    </motion.div>
  );
}
