// Мелкие элементы: метка, точка статуса, подзаголовок, пустое состояние.

import { motion } from "motion/react";
import type { ReactNode } from "react";
import { Button } from "./Button";
import { Icon } from "./Icon";
import { alpha, statusColor, tone as toneColor, useMotionPrefs } from "./theme";

interface BadgeProps {
  text: ReactNode;
  tone?: string;
  icon?: string;
  tint?: string;
  solid?: boolean;
}

/** Цветная метка-таблетка: <Badge text="готово" tone="success" /> */
export function Badge({ text, tone = "muted", icon = "", tint, solid = false }: BadgeProps) {
  const color = tint ?? toneColor(tone);
  return (
    <span
      className="badge"
      style={{
        color: solid ? "var(--bg)" : color,
        background: solid ? color : alpha(color, 0.14),
        borderColor: solid ? "transparent" : alpha(color, 0.35),
      }}
    >
      {icon && <Icon name={icon} size={12} />}
      <span className="ellipsis">{text}</span>
    </span>
  );
}

/** Точка статуса; для "работает" - расходящиеся круги пульса. */
export function StatusDot({ status, size = 8 }: { status: string; size?: number }) {
  const color = statusColor(status);
  return (
    <span className={`status-dot${status === "running" ? " is-live" : ""}`} style={{ width: size, height: size, color }}>
      <span className="status-dot-core" style={{ background: color }} />
    </span>
  );
}

interface SectionTitleProps {
  text: ReactNode;
  icon?: string;
  hint?: string;
  children?: ReactNode;
}

/** Подзаголовок секции внутри страницы или карточки. */
export function SectionTitle({ text, icon = "", hint = "", children }: SectionTitleProps) {
  return (
    <div className="section-title">
      {icon && <Icon name={icon} size={16} color="var(--violet-soft)" />}
      <div className="grow">
        <div className="h3 ellipsis">{text}</div>
        {hint && <div className="small mute">{hint}</div>}
      </div>
      {children && <div className="row">{children}</div>}
    </div>
  );
}

interface EmptyProps {
  icon?: string;
  title?: string;
  text?: string;
  actionText?: string;
  actionIcon?: string;
  onAction?: () => void;
}

/** Пустое состояние: парящая иконка в градиентном круге, текст и действие. */
export function EmptyState({ icon = "sparkles", title = "", text = "", actionText = "", actionIcon = "plus", onAction }: EmptyProps) {
  const m = useMotionPrefs();
  return (
    <motion.div
      className="empty"
      initial={m.on ? { opacity: 0, y: 12 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={m.ease(420)}
    >
      <div className="empty-orb rich-only">
        <Icon name={icon} size={26} color="var(--violet-soft)" />
      </div>
      {title && <div className="h3">{title}</div>}
      {text && <div className="dim empty-text">{text}</div>}
      {actionText && (
        <Button variant="primary" icon={actionIcon} onClick={onAction} className="empty-action">
          {actionText}
        </Button>
      )}
    </motion.div>
  );
}

/** Строка-подсказка с иконкой. */
export function Hint({ icon, children, color = "var(--violet-soft)" }: { icon: string; children: ReactNode; color?: string }) {
  return (
    <div className="hint-row">
      <Icon name={icon} size={16} color={color} />
      <div className="small dim grow">{children}</div>
    </div>
  );
}

/** Плашка с сообщением: предупреждение, ошибка, успех. */
export function Notice({ tone = "warning", icon, children, action }: { tone?: string; icon?: string; children: ReactNode; action?: ReactNode }) {
  const color = toneColor(tone);
  const glyph = icon ?? (tone === "error" ? "octagon-x" : tone === "success" ? "circle-check" : tone === "info" || tone === "accent" ? "info" : "triangle-alert");
  return (
    <div className="notice" style={{ background: alpha(color, 0.08), borderColor: alpha(color, 0.35) }}>
      <Icon name={glyph} size={16} color={color} />
      <div className="grow small wrap">{children}</div>
      {action}
    </div>
  );
}

/** Круглый аватар с градиентом и иконкой или буквой. */
export function Avatar({ icon, letter, size = 40, from = "var(--violet)", to = "var(--cyan)", round = true }: {
  icon?: string; letter?: string; size?: number; from?: string; to?: string; round?: boolean;
}) {
  return (
    <div
      className="avatar"
      style={{
        width: size, height: size, borderRadius: round ? "50%" : Math.round(size * 0.3),
        background: `linear-gradient(135deg, ${from}, ${to})`,
      }}
    >
      {icon ? <Icon name={icon} size={Math.round(size * 0.44)} color="white" /> : <span className="bold">{letter}</span>}
    </div>
  );
}

/** Квадратная плашка с иконкой в цвет тона. */
export function IconTile({ icon, color = "var(--violet-soft)", size = 38, bg }: { icon: string; color?: string; size?: number; bg?: string }) {
  return (
    <div className="icon-tile" style={{ width: size, height: size, background: bg ?? alpha(color, 0.14), color }}>
      <Icon name={icon} size={Math.round(size * 0.45)} />
    </div>
  );
}
