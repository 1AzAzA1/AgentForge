// Текущая страница оболочки. Хранится вне React, чтобы любая страница могла
// перевести пользователя на другую: go("run").

import { useSyncExternalStore } from "react";

let page = "";
const listeners = new Set<() => void>();

export function go(next: string) {
  if (next === page) return;
  page = next;
  listeners.forEach((l) => l());
}

export function usePage(): string {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => page,
  );
}
