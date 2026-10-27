// Выполнение: управление прогоном, решения человека, живые рассуждения
// агентов, граф подзадач и лента событий.

import { AnimatePresence, motion, useAnimate } from "motion/react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { host } from "../api/host";
import { Avatar, Badge, SectionTitle, StatusDot } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Field, Segmented } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { askConfirm } from "../ui/Sheet";
import { toast } from "../ui/Toasts";
import { alpha, statusColor, tone, useMotionPrefs } from "../ui/theme";
import { useTip } from "../ui/Tip";
import { ProgressRing } from "../ui/Visuals";

export function Run() {
  const t = useT();
  const ctl = useCtl("run");
  const backend = useCtl("backend");
  const approvals = useList("run", "approvals");
  const streams = useList("run", "streams");

  return (
    <Page
      title={t("run.title")}
      subtitle={ctl.taskTitle || t("run.subtitle")}
      icon="play"
      fill
      actions={
        backend.running ? (
          <>
            <Button icon={backend.paused ? "circle-play" : "circle-pause"} onClick={() => call("run", "togglePause")}>
              {backend.paused ? t("run.resume") : t("run.pause")}
            </Button>
            <Button
              variant="danger"
              icon="square"
              onClick={() => askConfirm({ title: t("run.stop"), text: t("run.stop_confirm"), yesText: t("run.stop"), onYes: () => call("run", "stop") })}
            >
              {t("run.stop")}
            </Button>
          </>
        ) : (
          <Button variant="primary" icon="play" loading={Boolean(ctl.starting)} disabled={!ctl.canStart} onClick={() => call("run", "start")}>
            {t("run.start")}
          </Button>
        )
      }
    >
      <Card padding={16}>
        <div className="row" style={{ gap: 22 }}>
          <ProgressRing value={ctl.progress ?? 0} label={ctl.total > 0 ? `${Math.round((ctl.progress ?? 0) * 100)}%` : "-"} />
          <Kpi caption={t("run.k_done")} value={`${ctl.done ?? 0} / ${ctl.total ?? 0}`} color="var(--success)" />
          <Kpi caption={t("run.k_review")} value={ctl.review ?? 0} color={ctl.review ? "var(--violet-soft)" : undefined} />
          <Kpi caption={t("run.k_errors")} value={ctl.errors ?? 0} color={ctl.errors ? "var(--danger)" : undefined} />
          <Kpi caption={t("run.k_time")} value={backend.runElapsed || "-"} />
          <Kpi caption={t("run.k_tokens")} value={backend.running ? backend.runTokens : "-"} />
          <Kpi caption={t("run.k_cost")} value={backend.running ? backend.runCost : "-"} color="var(--teal)" />
          <span className="spacer" />
          {!backend.running && ctl.blocker && (
            <div className="row blocker">
              <Icon name="info" size={15} color="var(--warning)" />
              <span className="small" style={{ color: "var(--warning)" }}>{ctl.blocker}</span>
            </div>
          )}
        </div>
      </Card>

      <AnimatePresence initial={false}>
        {approvals.map((a) => (
          <motion.div key={a.id} initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, height: 0 }}>
            <Approval item={a} />
          </motion.div>
        ))}
      </AnimatePresence>

      <div className="run-main">
        <div className="col run-streams">
          <SectionTitle text={t("run.live")} hint={t("run.live_hint")} icon="brain" />
          <div className="streams-grid">
            {streams.map((s) => <StreamCard key={s.id} s={s} />)}
          </div>
        </div>
        <div className="col run-side">
          <SectionTitle text={t("run.graph")} icon="workflow" />
          <Card padding={12} className="graph-card">
            <Graph />
          </Card>
          <Feed />
        </div>
      </div>
    </Page>
  );
}

function Kpi({ caption, value, color }: { caption: string; value: any; color?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-value mono" style={{ color }}>{value}</div>
      <div className="micro mute">{caption}</div>
    </div>
  );
}

const DECISION_ICONS: Record<string, string> = {
  approve: "check", rework: "rotate-ccw", skip: "skip-forward", extend: "trending-up", abort: "octagon-x",
};

function Approval({ item }: { item: any }) {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const [comment, setComment] = useState("");
  return (
    <Card glow glowColor="var(--warning)" padding={18}>
      <div className="col" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
          <div className="icon-tile" style={{ width: 38, height: 38, background: alpha("var(--warning)", 0.16), color: "var(--warning)" }}>
            <Icon name={item.icon} size={18} />
          </div>
          <div className="grow">
            <div className="row" style={{ gap: 8 }}>
              <span className="bold" style={{ color: "var(--warning)" }}>{item.reasonTitle}</span>
              {item.agent && <span className="dim">· {item.agent}</span>}
            </div>
            <div className="wrap">{item.question}</div>
          </div>
          {item.details && (
            <Button compact variant="ghost" icon={expanded ? "chevron-up" : "chevron-down"} onClick={() => setExpanded(!expanded)}>
              {expanded ? t("run.hide_details") : t("run.show_details")}
            </Button>
          )}
        </div>
        <AnimatePresence initial={false}>
          {expanded && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} style={{ overflow: "hidden" }}>
              <div className="details small dim wrap selectable">{item.details}</div>
            </motion.div>
          )}
        </AnimatePresence>
        <div className="row" style={{ gap: 10 }}>
          <Field icon="message-square-text" placeholder={t("run.comment_placeholder")} value={comment} onChange={setComment} />
          {(item.options as any[]).map((o) => (
            <Button
              key={o.value}
              variant={o.value === "approve" || o.value === "extend" ? "primary" : o.value === "abort" ? "danger" : "secondary"}
              icon={DECISION_ICONS[o.value] ?? "check"}
              onClick={() => call("run", "decide", item.id, o.value, comment)}
            >
              {o.title}
            </Button>
          ))}
        </div>
      </div>
    </Card>
  );
}

const PHASE_TONES: Record<string, string> = { tool: "accent", done: "success", error: "error", idle: "muted", thinking: "violet", review: "violet" };

function StreamCard({ s }: { s: any }) {
  const t = useT();
  const m = useMotionPrefs();
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const glow = s.isSupervisor ? "var(--magenta)" : "var(--cyan)";
  const steps = Math.min(s.maxSteps ?? 0, 20);
  const step = Math.min(s.step ?? 0, steps);
  const phaseTitle = t(`run.ph_${s.phase || "idle"}`);
  const segments: [string, string][] = s.segments ?? [];

  useLayoutEffect(() => {
    const el = box.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [segments, follow]);

  const copy = async () => {
    const text = await call<string>("run", "fullText", s.id);
    await host.copy(text);
    toast("info", t("toast.copied"));
  };

  return (
    <Card padding={14} glow={s.live} glowColor={glow} className="stream-card">
      <div className="row" style={{ gap: 10 }}>
        <div className="avatar-wrap">
          <Avatar icon={s.icon} size={36} from={s.isSupervisor ? "var(--magenta)" : "var(--violet)"} to={s.isSupervisor ? "var(--violet-soft)" : "var(--cyan)"} />
          {s.live && <span className="avatar-ring rich-only" style={{ borderColor: alpha(glow, 0.5) }} />}
        </div>
        <div className="grow">
          <div className="row" style={{ gap: 6 }}>
            <span className="semi ellipsis" style={{ maxWidth: 170 }}>{s.name}</span>
            <span className="micro mute mono ellipsis" style={{ maxWidth: 150 }}>{s.modelName}</span>
          </div>
          <div className={`small ellipsis ${s.subtask ? "dim" : "mute"}`}>{s.subtask || t("run.waiting")}</div>
        </div>
        <Badge text={phaseTitle} tone={PHASE_TONES[s.phase] ?? "violet"} icon={s.phase === "tool" ? "zap" : s.phase === "done" ? "check" : ""} />
        <IconButton icon="copy" doneIcon="check" size={28} tip={t("run.copy_stream")} onClick={copy} />
      </div>

      {steps > 0 && (
        <div className="row" style={{ gap: 4 }}>
          {Array.from({ length: steps }, (_, i) => (
            <span
              key={i}
              className="step-dot"
              style={{ background: i < step ? (i === step - 1 && s.live ? "var(--cyan)" : "var(--violet)") : "var(--surface3)" }}
            />
          ))}
          {step > 0 && <span className="micro mute" style={{ marginLeft: 6 }}>{t("run.step_of", { n: step, m: steps })}</span>}
          <span className="spacer" />
          {s.live && s.phase === "thinking" && m.on && <TypingDots />}
        </div>
      )}

      <div className="stream-box">
        <div
          ref={box}
          className="stream-text mono selectable"
          onScroll={(e) => {
            const el = e.currentTarget;
            setFollow(el.scrollTop >= el.scrollHeight - el.clientHeight - 8);
          }}
        >
          {segments.map(([kind, text], i) => (
            <span key={i} className={`chunk-${kind}`}>{text}</span>
          ))}
        </div>
        {segments.length === 0 && (
          <div className="stream-empty small mute">{s.isSupervisor ? t("run.sup_empty") : t("run.stream_empty")}</div>
        )}
        <AnimatePresence>
          {!follow && (
            <motion.div className="stream-latest" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <Button compact icon="arrow-down" onClick={() => setFollow(true)}>{t("run.to_latest")}</Button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </Card>
  );
}

/** "Печатает...": три прыгающие точки. */
function TypingDots() {
  return (
    <span className="typing">
      <span />
      <span />
      <span />
    </span>
  );
}

const NODE_H = 54;
const GAP_X = 34;
const GAP_Y = 12;

/** Граф подзадач: слои по зависимостям, рёбра "текут", пока идёт работа. */
function Graph() {
  const t = useT();
  const ctl = useCtl("run");
  const nodes = useList("run", "nodes");
  const edges = useList("run", "edges");
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(400);
  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const levels = ctl.levels ?? 0;
  // Узлы сужаются, чтобы все слои помещались в колонку без прокрутки.
  const nodeW = levels > 0 ? Math.max(118, Math.min(170, Math.floor((width - GAP_X * (levels - 1)) / levels))) : 170;
  const contentW = Math.max(width, levels * (nodeW + GAP_X) - GAP_X);
  const contentH = Math.max(0, (ctl.maxRows ?? 0) * (NODE_H + GAP_Y) - GAP_Y);

  return (
    <div ref={wrap} className="graph">
      {nodes.length === 0 ? (
        <div className="graph-empty small mute">{t("run.graph_empty")}</div>
      ) : (
        <div className="graph-content" style={{ width: contentW, height: contentH }}>
          <svg width={contentW} height={contentH} className="graph-edges">
            {edges.map((e) => {
              const x1 = e.fromLevel * (nodeW + GAP_X) + nodeW;
              const y1 = e.fromRow * (NODE_H + GAP_Y) + NODE_H / 2;
              const x2 = e.toLevel * (nodeW + GAP_X);
              const y2 = e.toRow * (NODE_H + GAP_Y) + NODE_H / 2;
              const d = `M${x1},${y1} C${x1 + GAP_X * 0.6},${y1} ${x2 - GAP_X * 0.6},${y2} ${x2},${y2}`;
              return (
                <path
                  key={e.id}
                  d={d}
                  className={`edge edge-${e.state}`}
                  fill="none"
                  strokeWidth={e.state === "active" ? 2 : 1.5}
                />
              );
            })}
          </svg>
          {nodes.map((n) => <GraphNode key={n.id} n={n} nodeW={nodeW} />)}
        </div>
      )}
    </div>
  );
}

function GraphNode({ n, nodeW }: { n: any; nodeW: number }) {
  const m = useMotionPrefs();
  const [scope, animate] = useAnimate();
  const last = useRef(n.status);
  const tip = useTip(`${n.title} · ${n.statusTitle}`);
  const color = statusColor(n.status);
  // "Щелчок" при завершении подзадачи.
  useEffect(() => {
    if (n.status === "done" && last.current !== "done" && m.rich && scope.current) {
      animate(scope.current, { scale: [1, 1.08, 1] }, { duration: 0.34 });
    }
    last.current = n.status;
  }, [n.status, m.rich, animate, scope]);
  return (
    <motion.div
      ref={scope}
      className="graph-node"
      initial={false}
      animate={{ x: n.level * (nodeW + GAP_X), y: n.row * (NODE_H + GAP_Y) }}
      transition={m.ease(420)}
      style={{
        width: nodeW,
        height: NODE_H,
        background: alpha(color, 0.1),
        borderColor: alpha(color, n.status === "idle" ? 0.35 : 0.8),
        borderWidth: n.status === "running" ? 2 : 1,
      }}
      {...tip.bind}
    >
      <div className="row" style={{ gap: 6 }}>
        <StatusDot status={n.status} size={7} />
        <span className="small semi grow ellipsis">{n.title}</span>
        {n.status === "done" && <Icon name="check" size={13} color="var(--success)" />}
      </div>
      <div className="micro mute ellipsis">{n.agentName}</div>
      {tip.node}
    </motion.div>
  );
}

function Feed() {
  const t = useT();
  const feed = useList("run", "feed");
  const [mode, setMode] = useState("all");
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const shown = feed.filter((f) => mode === "all" || (mode === "error" && f.tone === "error") || (mode === "key" && f.tone !== "muted"));
  useLayoutEffect(() => {
    const el = box.current;
    if (el && follow) el.scrollTop = el.scrollHeight;
  }, [shown.length, follow]);
  return (
    <>
      <SectionTitle text={t("run.feed")} icon="activity">
        <Segmented
          options={[
            { value: "all", title: t("run.f_all") },
            { value: "key", title: t("run.f_key") },
            { value: "error", title: t("run.f_errors") },
          ]}
          value={mode}
          onChange={setMode}
        />
        <IconButton icon="eraser" tip={t("run.clear_feed")} onClick={() => call("run", "clearFeed")} />
      </SectionTitle>
      <Card padding={6} className="feed-card">
        <div
          ref={box}
          className="feed selectable"
          onScroll={(e) => {
            const el = e.currentTarget;
            setFollow(el.scrollTop >= el.scrollHeight - el.clientHeight - 8);
          }}
        >
          {shown.map((f) => (
            <div key={f.id} className="feed-row">
              <span className="micro mute mono feed-time">{f.time}</span>
              <span className="dot feed-dot" style={{ background: tone(f.tone), width: 6, height: 6 }} />
              <span className={`small grow wrap ${f.tone === "muted" ? "mute" : "dim"}`}>
                <b className="feed-agent">{f.agent}</b> {f.message}
              </span>
            </div>
          ))}
          {feed.length === 0 && <div className="feed-empty mute">{t("run.feed_empty")}</div>}
        </div>
      </Card>
    </>
  );
}
