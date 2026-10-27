// Модальное окно внутри приложения: затемнение, "всплытие" с пружиной,
// закрытие по Esc и щелчку мимо. Плюс подтверждение действия.

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useT } from "../api/hooks";
import { Button, IconButton } from "./Button";
import { IconTile } from "./Bits";
import { useMotionPrefs } from "./theme";

interface SheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  icon?: string;
  width?: number;
  busy?: boolean;
  footer?: ReactNode;
  children?: ReactNode;
}

export function Sheet({ open, onClose, title, subtitle = "", icon = "", width = 560, busy = false, footer, children }: SheetProps) {
  const m = useMotionPrefs();
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy && !e.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open, busy, onClose]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="sheet-overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={m.ease(220)}
          onPointerDown={(e) => {
            if (e.target === e.currentTarget && !busy) onClose();
          }}
        >
          <motion.div
            className="sheet"
            style={{ width: `min(${width}px, calc(100vw - 48px))` }}
            role="dialog"
            aria-modal
            initial={{ opacity: 0, scale: 0.94 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.97, transition: m.ease(140) }}
            transition={m.rich ? { type: "spring", stiffness: 380, damping: 24 } : m.ease(420)}
          >
            <div className="sheet-head">
              {icon && <IconTile icon={icon} size={40} />}
              <div className="grow">
                <div className="h2">{title}</div>
                {subtitle && <div className="small dim wrap">{subtitle}</div>}
              </div>
              <IconButton icon="x" disabled={busy} onClick={onClose} />
            </div>
            <div className="sheet-body">{children}</div>
            {footer && <div className="sheet-foot">{footer}</div>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

// Подтверждение: askConfirm({...}) из любого места, окно одно на приложение.

interface ConfirmRequest {
  title: string;
  text?: string;
  yesText?: string;
  danger?: boolean;
  onYes: () => void;
}

let confirmState: ConfirmRequest | null = null;
const confirmListeners = new Set<() => void>();

export function askConfirm(request: ConfirmRequest) {
  confirmState = { danger: true, ...request };
  confirmListeners.forEach((l) => l());
}

function closeConfirm() {
  confirmState = null;
  confirmListeners.forEach((l) => l());
}

const subscribeConfirm = (l: () => void) => {
  confirmListeners.add(l);
  return () => confirmListeners.delete(l);
};

export function ConfirmHost() {
  const t = useT();
  const request = useSyncExternalStore(subscribeConfirm, () => confirmState);
  const danger = request?.danger ?? true;
  return (
    <Sheet
      open={request !== null}
      onClose={closeConfirm}
      title={request?.title ?? ""}
      subtitle={request?.text}
      icon={danger ? "triangle-alert" : "circle-alert"}
      width={440}
      footer={
        <>
          <span className="spacer" />
          <Button variant="ghost" onClick={closeConfirm}>{t("common.cancel")}</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            icon={danger ? "trash-2" : "check"}
            onClick={() => {
              const yes = request?.onYes;
              closeConfirm();
              yes?.();
            }}
          >
            {request?.yesText || t("common.delete")}
          </Button>
        </>
      }
    />
  );
}
