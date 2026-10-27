// Оболочка после входа: боковая навигация, верхняя панель со статусом прогона
// и страницы, которые создаются при первом открытии и дальше переключаются
// с анимацией, сохраняя своё состояние (введённый текст, прокрутку).

import { motion } from "motion/react";
import { useEffect, useMemo, useRef, useState, type ComponentType } from "react";
import { call } from "../api/bridge";
import { useCtl, useT } from "../api/hooks";
import { go, usePage } from "../api/nav";
import { Badge, StatusDot } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Field } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Sheet } from "../ui/Sheet";
import { useTip } from "../ui/Tip";
import { useMotionPrefs } from "../ui/theme";
import { OrbitLogo } from "../ui/Visuals";
import { Agents } from "./Agents";
import { Budget } from "./Budget";
import { Dashboard } from "./Dashboard";
import { Export } from "./Export";
import { Keys } from "./Keys";
import { Run } from "./Run";
import { Settings } from "./Settings";
import { Supervisor } from "./Supervisor";
import { Task } from "./Task";
import { Workspaces } from "./Workspaces";

const PAGES: Record<string, ComponentType> = {
  workspaces: Workspaces, agents: Agents, task: Task, run: Run, supervisor: Supervisor,
  dashboard: Dashboard, keys: Keys, budget: Budget, export: Export, settings: Settings,
};
const ORDER = ["workspaces", "agents", "task", "run", "supervisor", "dashboard", "keys", "budget", "export", "settings"];

interface NavEntry {
  key: string;
  icon: string;
  title: string;
}

function useSections() {
  const t = useT();
  return useMemo(
    () => [
      { title: t("nav.g_project"), items: [
        { key: "workspaces", icon: "layers", title: t("nav.workspaces") },
        { key: "agents", icon: "bot", title: t("nav.agents") },
        { key: "task", icon: "list-checks", title: t("nav.task") },
      ] },
      { title: t("nav.g_work"), items: [
        { key: "run", icon: "play", title: t("nav.run") },
        { key: "supervisor", icon: "shield-check", title: t("nav.supervisor") },
        { key: "dashboard", icon: "layout-dashboard", title: t("nav.dashboard") },
      ] },
      { title: t("nav.g_resources"), items: [
        { key: "keys", icon: "key-round", title: t("nav.keys") },
        { key: "budget", icon: "wallet", title: t("nav.budget") },
        { key: "export", icon: "package", title: t("nav.export") },
      ] },
    ],
    [t],
  );
}

export function Shell() {
  const backend = useCtl("backend");
  const page = usePage();
  const [palette, setPalette] = useState(false);

  // Стартовая страница выбирается один раз; дальше страницу меняет только
  // пользователь.
  const startPage = useRef(backend.workspaceId >= 0 ? "dashboard" : "workspaces");
  useEffect(() => {
    go(startPage.current);
  }, []);

  // Ctrl+1...0 - страницы, Ctrl+K - палитра команд.
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (!e.ctrlKey || e.altKey || e.shiftKey) return;
      if (e.key.toLowerCase() === "k" || e.code === "KeyK") {
        e.preventDefault();
        setPalette(true);
        return;
      }
      const digit = /^Digit(\d)$/.exec(e.code);
      if (digit) {
        e.preventDefault();
        const n = Number(digit[1]);
        go(ORDER[(n + 9) % 10]);
      }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, []);

  return (
    <div className="shell">
      <Sidebar page={page} onPalette={() => setPalette(true)} />
      <div className="shell-main">
        <TopBar page={page} />
        <Stage page={page} />
      </div>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
    </div>
  );
}

function Sidebar({ page, onPalette }: { page: string; onPalette: () => void }) {
  const t = useT();
  const backend = useCtl("backend");
  const sections = useSections();
  const logout = useTip(t("nav.logout"));
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <OrbitLogo size={36} />
        <div>
          <div className="h3 bold">{backend.appName}</div>
          <div className="micro mute mono">v{backend.appVersion}</div>
        </div>
      </div>

      <button type="button" className="sidebar-search" onClick={onPalette}>
        <Icon name="search" size={15} />
        <span className="grow small">{t("nav.search")}</span>
        <span className="kbd mono">Ctrl K</span>
      </button>

      <nav className="sidebar-nav">
        {sections.map((section) => (
          <div key={section.title} className="nav-section">
            <div className="nav-section-title">{section.title}</div>
            {section.items.map((item) => (
              <NavItem key={item.key} item={item} active={page === item.key} />
            ))}
          </div>
        ))}
      </nav>

      <NavItem item={{ key: "settings", icon: "settings", title: t("nav.settings") }} active={page === "settings"} />

      <div className="profile-card">
        <div className="avatar profile-avatar">{String(backend.username || "?")[0].toUpperCase()}</div>
        <div className="grow">
          <div className="semi ellipsis">{backend.username}</div>
          <div className="micro mute">{t("nav.local_profile")}</div>
        </div>
        <span {...logout.bind}>
          <IconButton icon="log-out" onClick={() => call("backend", "logout")} />
          {logout.node}
        </span>
      </div>
    </aside>
  );
}

function NavItem({ item, active }: { item: NavEntry; active: boolean }) {
  const backend = useCtl("backend");
  const supervisor = useCtl("supervisor");
  const m = useMotionPrefs();
  return (
    <button type="button" className={`nav-item${active ? " is-active" : ""}`} onClick={() => go(item.key)}>
      {active && (
        <motion.span layoutId="nav-highlight" className="nav-highlight" transition={m.on ? { type: "spring", stiffness: 420, damping: 34 } : { duration: 0 }}>
          <span className="nav-highlight-bar" />
        </motion.span>
      )}
      <Icon name={item.icon} size={17} className="nav-icon" />
      <span className="grow ellipsis nav-title">{item.title}</span>
      {item.key === "run" && backend.running && <StatusDot status="running" />}
      {item.key === "run" && backend.pendingApprovals > 0 && <Badge text={backend.pendingApprovals} tone="warning" solid />}
      {item.key === "supervisor" && supervisor.openIncidents > 0 && <Badge text={supervisor.openIncidents} tone="error" />}
    </button>
  );
}

function TopBar({ page }: { page: string }) {
  const t = useT();
  const backend = useCtl("backend");
  const run = useCtl("run");
  const bell = useTip(t("top.pending", { n: backend.pendingApprovals }));
  return (
    <div className="topbar">
      <Icon name="layers" size={15} color="var(--text-mute)" />
      <button type="button" className="ws-button" onClick={() => go("workspaces")}>
        <span className={backend.workspaceName ? "semi" : "semi mute"}>{backend.workspaceName || t("top.no_ws")}</span>
        <Icon name="chevron-down" size={14} color="var(--text-mute)" />
      </button>
      <span className="spacer" />

      {backend.running && (
        <button type="button" className="run-pill" onClick={() => go("run")}>
          <StatusDot status={backend.paused ? "paused" : "running"} />
          <span className="semi small">{backend.paused ? t("top.paused") : t("top.running")}</span>
          <span className="mono small dim">{backend.runElapsed}</span>
          <span className="run-pill-sep" />
          <Icon name="coins" size={13} color="var(--text-mute)" />
          <span className="mono small dim">{backend.runTokens}</span>
          <span className="mono small" style={{ color: "var(--teal)" }}>{backend.runCost}</span>
        </button>
      )}

      {backend.pendingApprovals > 0 && (
        <button type="button" className="bell" onClick={() => go("run")} {...bell.bind}>
          <Icon name="bell" size={16} className="bell-icon rich-only" />
          {bell.node}
        </button>
      )}

      {!backend.running && page !== "run" && (
        <Button
          compact
          icon="play"
          disabled={!run.canStart}
          onClick={() => {
            go("run");
            void call("run", "start");
          }}
        >
          {t("run.start")}
        </Button>
      )}
    </div>
  );
}

function Stage({ page }: { page: string }) {
  const m = useMotionPrefs();
  const [visited, setVisited] = useState<string[]>([]);
  useEffect(() => {
    if (page && !visited.includes(page)) setVisited((v) => [...v, page]);
  }, [page, visited]);
  return (
    <div className="stage">
      {ORDER.filter((key) => visited.includes(key)).map((key) => {
        const Page = PAGES[key];
        const current = key === page;
        return (
          <motion.div
            key={key}
            className="stage-page"
            style={{ pointerEvents: current ? "auto" : "none" }}
            initial={{ opacity: 0, y: m.rich ? 16 : 0 }}
            animate={
              current
                ? { opacity: 1, y: 0, display: "flex" }
                : { opacity: 0, y: m.rich ? 16 : 0, transitionEnd: { display: "none" } }
            }
            transition={{ opacity: m.ease(220), y: m.ease(420) }}
          >
            <Page />
          </motion.div>
        );
      })}
    </div>
  );
}

interface PaletteEntry {
  kind: "page" | "action";
  key: string;
  icon: string;
  title: string;
}

function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const backend = useCtl("backend");
  const sections = useSections();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
    }
  }, [open]);

  const entries = useMemo(() => {
    const list: PaletteEntry[] = [];
    for (const s of sections) for (const it of s.items) list.push({ kind: "page", ...it });
    list.push({ kind: "page", key: "settings", icon: "settings", title: t("nav.settings") });
    list.push({ kind: "action", key: "start", icon: "play", title: t("palette.start") });
    list.push({ kind: "action", key: "summary", icon: "scroll-text", title: t("sup.make_summary") });
    list.push({ kind: "action", key: "motion", icon: "sparkles", title: t("palette.motion") });
    const q = query.trim().toLowerCase();
    return q ? list.filter((e) => e.title.toLowerCase().includes(q)) : list;
  }, [sections, t, query]);

  const run = (entry?: PaletteEntry) => {
    onClose();
    if (!entry) return;
    if (entry.kind === "page") go(entry.key);
    else if (entry.key === "start") {
      go("run");
      void call("run", "start");
    } else if (entry.key === "summary") void call("supervisor", "makeSummary");
    else if (entry.key === "motion") {
      const next = backend.motion === "full" ? "reduced" : backend.motion === "reduced" ? "off" : "full";
      void call("backend", "setMotion", next);
    }
  };

  useEffect(() => {
    listRef.current?.querySelector(".is-selected")?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  return (
    <Sheet open={open} onClose={onClose} title={t("palette.title")} icon="command" width={560}>
      <Field
        icon="search"
        autoFocus
        value={query}
        placeholder={t("palette.placeholder")}
        onChange={(v) => {
          setQuery(v);
          setSelected(0);
        }}
        onEnter={() => run(entries[selected])}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setSelected((s) => Math.min(s + 1, entries.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setSelected((s) => Math.max(s - 1, 0));
          }
        }}
      />
      <div ref={listRef} className="palette-list">
        {entries.map((entry, i) => (
          <div
            key={`${entry.kind}-${entry.key}`}
            className={`palette-item${i === selected ? " is-selected" : ""}`}
            onPointerEnter={() => setSelected(i)}
            onClick={() => run(entry)}
          >
            <Icon name={entry.icon} size={16} />
            <span className="grow">{entry.title}</span>
            <span className="micro mute">{entry.kind === "page" ? t("palette.page") : t("palette.action")}</span>
          </div>
        ))}
      </div>
    </Sheet>
  );
}
