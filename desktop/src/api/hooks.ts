import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";
import { bridge, type Item, type Values } from "./bridge";

const NO_ITEMS: Item[] = [];

/** Значения экрана: useCtl("agents").hasKeys */
export function useCtl(target: string): Values {
  return useSyncExternalStore(bridge.subscribe, () => bridge.target(target).values);
}

/** Список экрана: useList("agents", "model") */
export function useList(target: string, name: string): Item[] {
  return useSyncExternalStore(bridge.subscribe, () => bridge.target(target).lists[name] ?? NO_ITEMS);
}

export function useBridgeStatus() {
  const status = useSyncExternalStore(bridge.subscribe, () => bridge.status);
  const error = useSyncExternalStore(bridge.subscribe, () => bridge.error);
  return { status, error };
}

/** Подписка на разовое событие ядра на время жизни компонента. */
export function useBridgeEvent(target: string, name: string, handler: (...args: any[]) => void) {
  useEffect(() => bridge.on(target, name, handler), [target, name, handler]);
}

/** Подставляет {name} из словаря; сломанный шаблон возвращается как есть. */
export function format(template: string, args: Record<string, unknown> = {}): string {
  return (template ?? "").replace(/\{(\w+)\}/g, (whole, key) => (key in args ? String(args[key]) : whole));
}

/** Строка вне компонента (обработчики событий, таймеры). */
export function tr(key: string, args?: Record<string, unknown>): string {
  const strings: Record<string, string> = bridge.target("backend").values.strings ?? {};
  return format(strings[key] ?? key, args);
}

/** Строки интерфейса текущего языка: t("nav.agents"), t("ws.updated", { when }). */
export function useT() {
  const backend = useCtl("backend");
  const strings: Record<string, string> = backend.strings ?? {};
  const t = useCallback(
    (key: string, args?: Record<string, unknown>) => {
      const text = strings[key] ?? key;
      return args ? format(text, args) : text;
    },
    [strings],
  );
  return useMemo(() => Object.assign(t, { lang: String(backend.lang ?? "ru") }), [t, backend.lang]);
}
