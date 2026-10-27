// Поля и переключатели. Все управляемые: значение приходит сверху, изменения
// уходят через onChange.

import { AnimatePresence, motion } from "motion/react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from "react";
import { createPortal } from "react-dom";
import { IconButton } from "./Button";
import { Icon } from "./Icon";
import { useMotionPrefs } from "./theme";

function Label({ text }: { text?: string }) {
  return text ? <div className="field-label">{text}</div> : null;
}

function Note({ error, hint }: { error?: string; hint?: string }) {
  if (error) return <div className="field-note is-error">{error}</div>;
  if (hint) return <div className="field-note">{hint}</div>;
  return null;
}

interface FieldProps {
  value: string;
  onChange?: (value: string) => void;
  label?: string;
  hint?: string;
  error?: string;
  icon?: string;
  placeholder?: string;
  password?: boolean;
  mono?: boolean;
  autoFocus?: boolean;
  readOnly?: boolean;
  inputRef?: Ref<HTMLInputElement>;
  onEnter?: () => void;
  /** поле потеряло фокус или нажат Enter: сохранить значение */
  onCommit?: (value: string) => void;
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
  className?: string;
}

/** Поле ввода с подписью, иконкой, ошибкой и светящимся фокусом. */
export function Field({
  value, onChange, label, hint, error, icon, placeholder, password = false, mono = false,
  autoFocus = false, readOnly = false, inputRef, onEnter, onCommit, onKeyDown, className = "",
}: FieldProps) {
  const [reveal, setReveal] = useState(false);
  return (
    <label className={`field ${className}`}>
      <Label text={label} />
      <div className={`field-box${error ? " is-error" : ""}`}>
        {icon && <Icon name={icon} size={16} className="field-icon" />}
        <input
          ref={inputRef}
          className={`field-input${mono ? " mono" : ""}${icon ? " has-icon" : ""}${password ? " has-reveal" : ""}`}
          type={password && !reveal ? "password" : "text"}
          value={value}
          placeholder={placeholder}
          autoFocus={autoFocus}
          readOnly={readOnly}
          spellCheck={false}
          autoComplete="off"
          onChange={(e) => onChange?.(e.target.value)}
          onBlur={(e) => onCommit?.(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              onEnter?.();
              onCommit?.((e.target as HTMLInputElement).value);
            }
            onKeyDown?.(e);
          }}
        />
        {password && (
          <IconButton
            className="field-reveal"
            size={28}
            icon={reveal ? "eye-off" : "eye"}
            onClick={() => setReveal(!reveal)}
          />
        )}
      </div>
      <Note error={error} hint={hint} />
    </label>
  );
}

interface TextBoxProps {
  value: string;
  onChange?: (value: string) => void;
  label?: string;
  hint?: string;
  placeholder?: string;
  mono?: boolean;
  minHeight?: number;
  autoFocus?: boolean;
  onCommit?: (value: string) => void;
}

/** Многострочное поле с тем же фокусом, что у Field. */
export function TextBox({ value, onChange, label, hint, placeholder, mono = false, minHeight = 120, autoFocus, onCommit }: TextBoxProps) {
  return (
    <label className="field">
      <Label text={label} />
      <div className="field-box">
        <textarea
          className={`field-area${mono ? " mono" : ""}`}
          style={{ minHeight }}
          value={value}
          placeholder={placeholder}
          autoFocus={autoFocus}
          spellCheck={false}
          onChange={(e) => onChange?.(e.target.value)}
          onBlur={(e) => onCommit?.(e.target.value)}
        />
      </div>
      <Note hint={hint} />
    </label>
  );
}

export interface Option {
  value: any;
  title: string;
}

interface SelectProps {
  options: Option[] | string[];
  value: any;
  onChange?: (value: any) => void;
  label?: string;
  icon?: string;
  placeholder?: string;
  /** можно вписать своё значение (например, имя модели) */
  editable?: boolean;
  className?: string;
}

function normalize(options: Option[] | string[]): Option[] {
  return (options as any[]).map((o) => (typeof o === "object" ? o : { value: o, title: String(o) }));
}

/** Выпадающий список. Список рисуется поверх окна и не обрезается диалогом. */
export function Select({ options, value, onChange, label, icon, placeholder = "", editable = false, className = "" }: SelectProps) {
  const m = useMotionPrefs();
  const list = normalize(options);
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState(-1);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const current = list.findIndex((o) => o.value === value);
  const title = current >= 0 ? list[current].title : value == null ? "" : String(value);

  const toggle = (next = !open) => {
    if (next && boxRef.current) setRect(boxRef.current.getBoundingClientRect());
    setHover(current);
    setOpen(next);
  };
  const pick = (o: Option) => {
    onChange?.(o.value);
    setOpen(false);
  };

  useLayoutEffect(() => {
    if (!open) return;
    const update = () => boxRef.current && setRect(boxRef.current.getBoundingClientRect());
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!boxRef.current?.contains(t) && !popRef.current?.contains(t)) setOpen(false);
    };
    window.addEventListener("pointerdown", away, true);
    return () => window.removeEventListener("pointerdown", away, true);
  }, [open]);

  const keys = (e: KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) toggle(true);
      else setHover((h) => Math.min(h + 1, list.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHover((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter" && open && hover >= 0 && list[hover]) {
      e.preventDefault();
      pick(list[hover]);
    } else if (e.key === "Escape" && open) {
      // Esc закрывает только список, а не диалог, в котором он лежит.
      e.preventDefault();
      setOpen(false);
    }
  };

  const below = rect ? window.innerHeight - rect.bottom > 240 || rect.top < 260 : true;

  return (
    <div className={`field ${className}`}>
      <Label text={label} />
      <div
        ref={boxRef}
        className={`field-box select-box${open ? " is-open" : ""}`}
        onClick={() => !editable && toggle()}
        onKeyDown={keys}
        tabIndex={editable ? -1 : 0}
        role="combobox"
        aria-expanded={open}
      >
        {icon && <Icon name={icon} size={16} className="field-icon" />}
        {editable ? (
          <input
            className={`field-input${icon ? " has-icon" : ""} has-reveal`}
            value={value ?? ""}
            placeholder={placeholder}
            spellCheck={false}
            onChange={(e) => onChange?.(e.target.value)}
            onFocus={() => list.length && toggle(true)}
          />
        ) : (
          <div className={`field-input select-value${icon ? " has-icon" : ""}${title ? "" : " is-placeholder"}`}>
            <span className="ellipsis">{title || placeholder}</span>
          </div>
        )}
        <motion.span
          className="select-chevron"
          animate={{ rotate: open ? 180 : 0 }}
          transition={m.ease(220)}
          onClick={(e) => {
            if (editable) {
              e.stopPropagation();
              toggle();
            }
          }}
        >
          <Icon name="chevron-down" size={16} />
        </motion.span>
      </div>
      {createPortal(
        <AnimatePresence>
          {open && rect && list.length > 0 && (
            <motion.div
              ref={popRef}
              className="select-pop"
              style={{
                left: rect.left,
                width: rect.width,
                ...(below ? { top: rect.bottom + 6 } : { bottom: window.innerHeight - rect.top + 6 }),
              }}
              initial={{ opacity: 0, y: below ? -6 : 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={m.ease(180)}
            >
              {list.map((o, i) => (
                <div
                  key={`${o.value}-${i}`}
                  className={`select-item${i === hover ? " is-hover" : ""}${i === current ? " is-current" : ""}`}
                  onPointerEnter={() => setHover(i)}
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => pick(o)}
                >
                  <span className="grow ellipsis">{o.title}</span>
                  {i === current && <Icon name="check" size={14} />}
                </div>
              ))}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </div>
  );
}

interface ToggleProps {
  checked: boolean;
  onChange?: (next: boolean) => void;
  label?: string;
  hint?: string;
  disabled?: boolean;
}

/** Переключатель с "пружинящим" бегунком и подписью слева. Показывает то, что
 *  пришло сверху: если сохранение не прошло, бегунок вернётся сам. */
export function Toggle({ checked, onChange, label, hint, disabled = false }: ToggleProps) {
  const m = useMotionPrefs();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={`toggle${label || hint ? " has-text" : ""}${disabled ? " is-disabled" : ""}`}
      disabled={disabled}
      onClick={() => onChange?.(!checked)}
    >
      {(label || hint) && (
        <span className="toggle-text">
          {label && <span>{label}</span>}
          {hint && <span className="small mute">{hint}</span>}
        </span>
      )}
      <span className={`toggle-track${checked ? " is-on" : ""}`}>
        <motion.span
          className="toggle-knob"
          initial={false}
          animate={{ x: checked ? 18 : 0 }}
          whileTap={{ scale: 0.85 }}
          transition={m.rich ? { type: "spring", stiffness: 500, damping: 22 } : m.ease(220)}
        />
      </span>
    </button>
  );
}

interface ChipProps {
  text: ReactNode;
  icon?: string;
  on: boolean;
  onChange?: (next: boolean) => void;
}

/** Переключаемая "таблетка": для инструментов, опций, фильтров. Галочка при
 *  выборе выпрыгивает на пружине. */
export function Chip({ text, icon = "", on, onChange }: ChipProps) {
  const m = useMotionPrefs();
  const glyph = on ? "check" : icon;
  return (
    <motion.button
      type="button"
      className={`chip${on ? " is-on" : ""}`}
      whileTap={{ scale: 0.94 }}
      transition={m.spring}
      onClick={() => onChange?.(!on)}
    >
      <AnimatePresence initial={false} mode="popLayout">
        {glyph && (
          <motion.span
            key={glyph}
            className="chip-glyph"
            initial={{ scale: 0.35, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.35, opacity: 0 }}
            transition={m.spring}
          >
            <Icon name={glyph} size={14} />
          </motion.span>
        )}
      </AnimatePresence>
      <span>{text}</span>
    </motion.button>
  );
}

export interface SegOption {
  value: any;
  title: string;
  icon?: string;
}

interface SegmentedProps {
  options: SegOption[];
  value: any;
  onChange?: (value: any) => void;
  stretch?: boolean;
}

/** Сегментированный переключатель: подсветка "переезжает" к выбранному пункту
 *  на мягкой пружине и меняет курс на лету при быстрых щелчках. */
export function Segmented({ options, value, onChange, stretch = false }: SegmentedProps) {
  const m = useMotionPrefs();
  const id = useId();
  return (
    <div className={`segmented${stretch ? " is-stretch" : ""}`}>
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button key={String(o.value)} type="button" className={`seg${active ? " is-active" : ""}`} onClick={() => onChange?.(o.value)}>
            {active && <motion.span layoutId={`seg-${id}`} className="seg-pill" transition={m.springSoft} />}
            <span className="seg-content">
              {o.icon && <Icon name={o.icon} size={14} />}
              <span className="ellipsis">{o.title}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

interface SliderProps {
  label: string;
  min: number;
  max: number;
  step: number;
  value: number;
  decimals?: number;
  suffix?: string;
  format?: (v: number) => string;
  disabled?: boolean;
  /** значение применяется по отпусканию ползунка */
  onCommit?: (v: number) => void;
}

/** Слайдер с подписью и значением справа. */
export function RangeSlider({ label, min, max, step, value, decimals = 2, suffix = "", format, disabled = false, onCommit }: SliderProps) {
  const [local, setLocal] = useState(value);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) setLocal(value);
  }, [value]);
  const commit = () => {
    dragging.current = false;
    if (local !== value) onCommit?.(local);
  };
  const pct = max > min ? ((local - min) / (max - min)) * 100 : 0;
  return (
    <div className={`slider${disabled ? " is-disabled" : ""}`}>
      <div className="row">
        <span className="field-label grow">{label}</span>
        <span className="slider-value mono">{format ? format(local) : local.toFixed(decimals) + suffix}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={local}
        disabled={disabled}
        style={{ ["--pct" as string]: `${pct}%` }}
        onPointerDown={() => (dragging.current = true)}
        onChange={(e) => setLocal(Number(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={() => dragging.current && commit()}
      />
    </div>
  );
}
