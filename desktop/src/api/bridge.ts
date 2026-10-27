// Клиент ядра: WebSocket, зеркало состояния контроллеров и вызовы методов.
//
// Python держит состояние каждого экрана (agents, run, ...) и присылает только
// изменения. Здесь они накладываются на локальную копию; каждый изменённый
// экран получает новый объект, поэтому React перерисовывает лишь то, что
// действительно поменялось.

import { host } from "./host";

export type Values = Record<string, any>;
export type Item = Record<string, any>;

interface Target {
  values: Values;
  lists: Record<string, Item[]>;
}

type Status = "connecting" | "open" | "lost";
type Handler = (...args: any[]) => void;

interface Pending {
  resolve: (value: any) => void;
  reject: (error: Error) => void;
}

const EMPTY_TARGET: Target = { values: {}, lists: {} };

class Bridge {
  status: Status = "connecting";
  error = "";
  targets: Record<string, Target> = {};
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, Pending>();
  private listeners = new Set<() => void>();
  private handlers = new Map<string, Set<Handler>>();
  private retry = 0;

  async connect(): Promise<void> {
    let info;
    try {
      info = await host.backend();
    } catch (err) {
      this.setStatus("lost", String((err as Error)?.message ?? err));
      return;
    }
    const ws = new WebSocket(`ws://127.0.0.1:${info.port}`);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ t: "hello", token: info.token }));
    ws.onmessage = (event) => this.receive(JSON.parse(String(event.data)));
    ws.onclose = () => {
      this.ws = null;
      for (const p of this.pending.values()) p.reject(new Error("соединение с ядром потеряно"));
      this.pending.clear();
      this.setStatus(this.status === "open" ? "lost" : this.status, this.error);
      // Ядро могли перезапустить (режим разработки): пробуем подключиться снова.
      this.retry = Math.min(this.retry + 1, 10);
      window.setTimeout(() => void this.connect(), 400 * this.retry);
    };
  }

  private setStatus(status: Status, error = "") {
    this.status = status;
    this.error = error;
    this.notify();
  }

  private receive(message: any) {
    if (message.t === "snapshot") {
      this.targets = message.state;
      this.retry = 0;
      this.setStatus("open");
    } else if (message.t === "batch") {
      for (const item of message.items) this.apply(item);
      this.notify();
    } else if (message.t === "result") {
      const p = this.pending.get(message.id);
      if (!p) return;
      this.pending.delete(message.id);
      if (message.ok) p.resolve(message.value);
      else p.reject(new Error(message.error));
    }
  }

  private apply(item: any) {
    const target: Target = this.targets[item.target] ?? EMPTY_TARGET;
    if (item.t === "state") {
      this.targets[item.target] = { ...target, values: { ...target.values, ...item.values } };
    } else if (item.t === "list") {
      const list = target.lists[item.name] ?? [];
      let next: Item[] = list;
      if (item.op === "set") next = item.items;
      else if (item.op === "append") {
        next = [...list, item.item];
        if (item.limit != null && next.length > item.limit) next = next.slice(next.length - item.limit);
      } else if (item.op === "update") {
        next = list.map((row) => (row.id === item.key ? { ...row, ...item.changes } : row));
      }
      this.targets[item.target] = { ...target, lists: { ...target.lists, [item.name]: next } };
    } else if (item.t === "event") {
      const set = this.handlers.get(`${item.target}.${item.name}`);
      set?.forEach((handler) => handler(...item.args));
    }
  }

  private notify() {
    this.listeners.forEach((listener) => listener());
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  target(name: string): Target {
    return this.targets[name] ?? EMPTY_TARGET;
  }

  call<T = any>(target: string, method: string, ...args: any[]): Promise<T> {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("нет соединения с ядром"));
    }
    const id = ++this.seq;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ t: "call", id, target, method, args }));
    });
  }

  on(target: string, name: string, handler: Handler): () => void {
    const key = `${target}.${name}`;
    if (!this.handlers.has(key)) this.handlers.set(key, new Set());
    this.handlers.get(key)!.add(handler);
    return () => this.handlers.get(key)?.delete(handler);
  }
}

export const bridge = new Bridge();
export const call = bridge.call.bind(bridge);
