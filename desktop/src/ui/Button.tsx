// Кнопки. Движение отвечает только на действия человека и подчиняется уровню
// анимаций из настроек:
// * нажатие - упругое "вдавливание" с отскоком (пружина, а не таймер);
// * основная кнопка - мягкий свет под курсором и надпись, которая чуть
//   тянется к указателю (только при полном движении);
// * загрузка - иконка сменяется индикатором, ширина меняется пружиной.

import {
  AnimatePresence,
  motion,
  useMotionTemplate,
  useMotionValue,
  useSpring,
} from "motion/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import { Spinner } from "./Spinner";
import { useMotionPrefs } from "./theme";
import { useTip } from "./Tip";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "success";

interface ButtonProps {
  variant?: Variant;
  icon?: string;
  loading?: boolean;
  compact?: boolean;
  disabled?: boolean;
  block?: boolean;
  title?: string;
  className?: string;
  onClick?: () => void;
  children?: ReactNode;
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export function Button({
  variant = "secondary", icon = "", loading = false, compact = false, disabled = false,
  block = false, title, className = "", onClick, children,
}: ButtonProps) {
  const m = useMotionPrefs();
  const primary = variant === "primary";
  const magnetic = primary && m.rich;
  const [hot, setHot] = useState(false);

  // Свет под курсором идёт за указателем с небольшой задержкой пружины.
  const gx = useMotionValue(50);
  const gy = useMotionValue(50);
  const sgx = useSpring(gx, { stiffness: 260, damping: 28 });
  const sgy = useSpring(gy, { stiffness: 260, damping: 28 });
  const glow = useMotionTemplate`radial-gradient(circle at ${sgx}% ${sgy}%, rgb(255 255 255 / 0.34), rgb(255 255 255 / 0) 60%)`;
  // Смещение надписи к курсору: несколько пикселей, не больше.
  const px = useMotionValue(0);
  const py = useMotionValue(0);
  const spx = useSpring(px, { stiffness: 420, damping: 18 });
  const spy = useSpring(py, { stiffness: 420, damping: 18 });

  const move = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (!magnetic || disabled) return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    gx.set((x / r.width) * 100);
    gy.set((y / r.height) * 100);
    px.set(clamp(((x - r.width / 2) / r.width) * 9, -3.5, 3.5));
    py.set(clamp(((y - r.height / 2) / r.height) * 5, -2, 2));
  };
  const leave = () => {
    setHot(false);
    px.set(0);
    py.set(0);
  };

  const box = compact ? 14 : 16;
  const shown = loading || icon !== "";
  const gap = children ? 8 : 0;
  const tip = useTip(title ?? "");
  const cls = ["btn", `btn-${variant}`, compact && "btn-compact", block && "btn-block", loading && "is-loading", className]
    .filter(Boolean)
    .join(" ");

  return (
    <motion.button
      type="button"
      className={cls}
      disabled={disabled}
      aria-busy={loading}
      whileTap={disabled || loading ? undefined : { scale: 0.955 }}
      transition={m.spring}
      onPointerEnter={(e) => { setHot(true); tip.bind.onPointerEnter(e); }}
      onPointerMove={move}
      onPointerLeave={() => { leave(); tip.bind.onPointerLeave(); }}
      onClick={() => !loading && onClick?.()}
    >
      {primary && <span className="btn-fill" />}
      {magnetic && (
        <motion.span
          className="btn-glow"
          style={{ background: glow }}
          animate={{ opacity: hot && !disabled ? 1 : 0 }}
          transition={m.ease(220)}
        />
      )}
      <motion.span className="btn-content" style={{ x: spx, y: spy }}>
        <motion.span
          className="btn-glyph"
          initial={false}
          animate={{ width: shown ? box + gap : 0 }}
          transition={m.springSoft}
          style={{ height: box }}
        >
          <AnimatePresence initial={false}>
            {loading ? (
              <motion.span
                key="spinner"
                className="btn-glyph-item"
                initial={{ opacity: 0, scale: 0.3 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.3 }}
                transition={m.spring}
              >
                <Spinner size={box - 2} color={primary ? "white" : "var(--violet-soft)"} />
              </motion.span>
            ) : icon ? (
              <motion.span
                key={`icon-${icon}`}
                className="btn-glyph-item"
                initial={{ opacity: 0, scale: 0.3 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.3 }}
                transition={m.spring}
              >
                <Icon name={icon} size={box} />
              </motion.span>
            ) : null}
          </AnimatePresence>
        </motion.span>
        {children !== undefined && children !== "" && <span className="btn-label">{children}</span>}
      </motion.span>
      {tip.node}
    </motion.button>
  );
}

interface IconButtonProps {
  icon: string;
  tip?: string;
  danger?: boolean;
  size?: number;
  /** иконка-подтверждение: после щелчка ненадолго заменяет основную */
  doneIcon?: string;
  disabled?: boolean;
  className?: string;
  onClick?: () => void;
}

export function IconButton({ icon, tip = "", danger = false, size = 32, doneIcon = "", disabled = false, className = "", onClick }: IconButtonProps) {
  const m = useMotionPrefs();
  const [done, setDone] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const tooltip = useTip(tip, !done);
  const click = () => {
    onClick?.();
    if (doneIcon) {
      setDone(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setDone(false), 1400);
    }
  };
  const glyph = Math.round(size * 0.5);
  return (
    <motion.button
      type="button"
      className={["icon-btn", danger && "icon-btn-danger", done && "is-done", className].filter(Boolean).join(" ")}
      style={{ width: size, height: size }}
      disabled={disabled}
      aria-label={tip}
      whileTap={disabled ? undefined : { scale: 0.86 }}
      transition={m.spring}
      onClick={click}
      {...tooltip.bind}
    >
      <AnimatePresence initial={false} mode="popLayout">
        <motion.span
          key={done ? "done" : icon}
          className="icon-btn-glyph"
          initial={{ opacity: 0, scale: 0.4 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.4 }}
          transition={m.spring}
        >
          <Icon name={done ? doneIcon : icon} size={glyph} />
        </motion.span>
      </AnimatePresence>
      {tooltip.node}
    </motion.button>
  );
}
