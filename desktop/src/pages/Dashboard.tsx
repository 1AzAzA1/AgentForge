// Дашборд: метрики, прогресс, кривые расхода, агенты, лента и инциденты.

import { useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { go } from "../api/nav";
import { Badge, EmptyState, SectionTitle, StatusDot } from "../ui/Bits";
import { IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Segmented } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { alpha, tone, useMotionPrefs } from "../ui/theme";
import { BarList, LineChart, SegmentBar, Ticker } from "../ui/Visuals";
import { motion } from "motion/react";

export function Dashboard() {
  const t = useT();
  const m = useMotionPrefs();
  const ctl = useCtl("dashboard");
  const backend = useCtl("backend");
  const agents = useList("dashboard", "agents");
  const feed = useList("dashboard", "feed");
  const incidents = useList("dashboard", "incidents");
  const [series, setSeries] = useState("cost");
  const limitPct: number = ctl.limitPct ?? -1;

  return (
    <Page
      title={t("dash.title")}
      subtitle={t("dash.subtitle")}
      icon="layout-dashboard"
      actions={<IconButton icon="refresh-cw" tip={t("common.refresh")} onClick={() => call("dashboard", "refresh")} />}
    >
      {backend.workspaceId < 0 ? (
        <EmptyState icon="layers" title={t("ws.empty_title")} text={t("ws.empty")} actionText={t("nav.workspaces")} actionIcon="arrow-right" onAction={() => go("workspaces")} />
      ) : (
        <>
          <div className="metrics">
            <Metric idx={0} icon="bot" caption={t("dash.m_agents")} value={ctl.agentsCount ?? 0} />
            <Metric idx={1} icon="list-checks" caption={t("dash.m_subtasks")} value={ctl.done ?? 0} total={ctl.total > 0 ? String(ctl.total) : ""} color="var(--success)" />
            <Metric
              idx={2}
              icon="coins"
              unit="tokens"
              value={ctl.tokens ?? 0}
              caption={limitPct >= 0 ? t("dash.m_tokens_pct", { pct: Math.round(limitPct * 100) }) : t("dash.m_tokens")}
              color={limitPct >= 1 ? "var(--danger)" : limitPct >= 0.8 ? "var(--warning)" : undefined}
            />
            <Metric idx={3} icon="dollar-sign" unit="usd" caption={t("dash.m_cost")} value={ctl.cost ?? 0} color="var(--teal)" />
            <Metric idx={4} icon="rotate-ccw" caption={t("dash.m_reworks")} value={ctl.reworks ?? 0} color={ctl.reworks ? "var(--warning)" : undefined} />
            <Metric
              idx={5}
              icon="triangle-alert"
              caption={t("dash.m_open_incidents")}
              value={ctl.openIncidents ?? 0}
              color={ctl.openIncidents ? "var(--danger)" : undefined}
              onClick={() => go("supervisor")}
            />
          </div>

          <Card stagger={2}>
            <div className="col" style={{ gap: 12 }}>
              <div className="row">
                <Icon name="list-checks" color="var(--primary-soft)" />
                <span className="h3 grow ellipsis">{ctl.taskTitle || t("task.no_task")}</span>
                {ctl.total > 0 && (
                  <span className="mono dim">
                    {ctl.done} / {ctl.total} · {Math.round((ctl.done / ctl.total) * 100)}%
                  </span>
                )}
              </div>
              <SegmentBar segments={ctl.segments ?? []} />
            </div>
          </Card>

          <div className="dash-charts">
            <Card stagger={3} className="chart-card" style={{ flex: 3 }}>
              <div className="row">
                <SectionTitle text={t("dash.spend")} icon="chart-line" />
                <span className="spacer" />
                <Segmented
                  options={[{ value: "cost", title: t("dash.money") }, { value: "tokens", title: t("dash.tokens") }]}
                  value={series}
                  onChange={setSeries}
                />
              </div>
              <div className="chart-area">
                <LineChart
                  points={series === "cost" ? ctl.costSeries ?? [] : ctl.tokenSeries ?? []}
                  unit={series === "cost" ? "usd" : "tokens"}
                  color={series === "cost" ? "var(--teal)" : "var(--primary-soft)"}
                  fill={series === "cost" ? "var(--teal)" : "var(--primary)"}
                  emptyText={t("dash.no_usage")}
                />
              </div>
            </Card>
            <Card stagger={4} className="chart-card" style={{ flex: 2 }}>
              <SectionTitle text={t("dash.by_agent")} icon="chart-bar">
                {ctl.supervisorShare > 0 && (
                  <Badge
                    text={t("dash.sup_share", { pct: Math.round(ctl.supervisorShare * 100) })}
                    tone={ctl.supervisorShare > 0.5 ? "warning" : "primary"}
                    icon="shield-check"
                  />
                )}
              </SectionTitle>
              <div className="chart-scroll">
                <BarList bars={ctl.bars ?? []} emptyText={t("dash.no_usage")} />
              </div>
            </Card>
          </div>

          <Card stagger={5}>
            <div className="col" style={{ gap: 8 }}>
              <SectionTitle text={t("dash.agents")} icon="users" />
              {agents.length === 0 && <div className="mute">{t("agents.empty")}</div>}
              {agents.map((a) => (
                <div key={a.id} className="dash-agent">
                  <StatusDot status={a.status} />
                  <Icon name={a.icon} size={15} color={a.isSupervisor ? "var(--magenta)" : "var(--primary-soft)"} />
                  <span className="medium ellipsis" style={{ width: 180 }}>{a.name}</span>
                  <span className={`small grow ellipsis ${a.doing ? "dim" : "mute"}`}>{a.doing ? `→ ${a.doing}` : a.statusTitle}</span>
                  <div className="track" style={{ width: 120, height: 5 }}>
                    <motion.div className="track-fill" style={{ background: "var(--primary)" }} initial={false} animate={{ width: `${Math.min(1, a.share) * 100}%` }} transition={m.ease(420)} />
                  </div>
                  <span className="mono small dim num">{a.tokens}</span>
                  <span className="mono small num" style={{ color: "var(--teal)" }}>{a.cost}</span>
                </div>
              ))}
            </div>
          </Card>

          <div className="dash-bottom">
            <Card stagger={6} style={{ flex: 3 }}>
              <div className="col" style={{ gap: 10 }}>
                <SectionTitle text={t("dash.feed")} icon="message-square-text" />
                {feed.length === 0 && <div className="mute">{t("dash.no_feed")}</div>}
                {feed.map((f) => (
                  <div key={f.id} className="col" style={{ gap: 3 }}>
                    <div className="row" style={{ gap: 8 }}>
                      <span className="dot" style={{ width: 6, height: 6, background: tone(f.tone) }} />
                      <span className="semi small" style={{ color: tone(f.tone) }}>{f.who}</span>
                      {f.badge && <Badge text={f.badge} tone={f.tone} />}
                      {f.confidence && <span className="micro mute">{t("dash.confidence")} {f.confidence}</span>}
                      <span className="spacer" />
                      <span className="micro mute mono">{f.when}</span>
                    </div>
                    <div className="small dim clamp2" style={{ paddingLeft: 14 }}>{f.text}</div>
                  </div>
                ))}
              </div>
            </Card>
            <Card stagger={7} style={{ flex: 2 }}>
              <div className="col" style={{ gap: 10 }}>
                <SectionTitle text={t("dash.incidents")} icon="triangle-alert" />
                {incidents.length === 0 && <div className="mute">{t("sup.no_incidents")}</div>}
                {incidents.map((i) => (
                  <div key={i.id} className="col" style={{ gap: 3 }}>
                    <div className="row">
                      <span className="semi small grow" style={{ color: tone(i.tone) }}>{i.kindTitle}</span>
                      <span className="micro mute">{i.statusTitle}</span>
                    </div>
                    <div className="small dim clamp2">{i.description}</div>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </>
      )}
    </Page>
  );
}

/** Крупная метрика с "досчитывающим" числом. */
function Metric({ idx, icon, caption, value, unit = "int", total = "", color, onClick }: {
  idx: number; icon: string; caption: string; value: number; unit?: string; total?: string; color?: string; onClick?: () => void;
}) {
  const accent = color ?? "var(--primary-soft)";
  return (
    <Card stagger={idx} hoverable padding={16} onClick={onClick}>
      <div className="col" style={{ gap: 6 }}>
        <div className="row">
          <div className="icon-tile" style={{ width: 30, height: 30, borderRadius: 9, background: alpha(color ?? "var(--primary)", 0.14), color: accent }}>
            <Icon name={icon} size={15} />
          </div>
          <span className="spacer" />
          {onClick && <Icon name="arrow-up-right" size={14} color="var(--text-mute)" />}
        </div>
        <Ticker value={value} unit={unit} total={total} className="metric-value bold" style={{ color: color ?? "var(--text)" }} />
        <div className="small mute ellipsis">{caption}</div>
      </div>
    </Card>
  );
}
