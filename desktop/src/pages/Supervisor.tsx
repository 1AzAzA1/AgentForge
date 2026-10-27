// Супервайзер: кто проверяет, анонимные сводки, инциденты и решения человека.

import { useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { host } from "../api/host";
import { go } from "../api/nav";
import { Avatar, Badge, EmptyState } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Segmented, TextBox } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { Sheet } from "../ui/Sheet";
import { toast } from "../ui/Toasts";
import { alpha, tone } from "../ui/theme";

export function Supervisor() {
  const t = useT();
  const ctl = useCtl("supervisor");
  const backend = useCtl("backend");
  const summaries = useList("supervisor", "summaries");
  const incidents = useList("supervisor", "incidents");
  const decisions = useList("supervisor", "decisions");
  const [tab, setTab] = useState("summaries");
  const [resolving, setResolving] = useState<{ id: number; text: string } | null>(null);
  const [resolution, setResolution] = useState("");
  const local = ctl.mode === "local";

  return (
    <Page
      title={t("sup.title")}
      subtitle={t("sup.subtitle")}
      icon="shield-check"
      actions={
        <Button
          variant="primary"
          icon="scroll-text"
          loading={Boolean(ctl.summarizing)}
          disabled={backend.workspaceId < 0 || backend.running}
          onClick={() => call("supervisor", "makeSummary")}
        >
          {t("sup.make_summary")}
        </Button>
      }
    >
      <Card padding={16} glow={ctl.configured} glowColor="var(--magenta)">
        <div className="row" style={{ gap: 14 }}>
          <Avatar
            icon={local ? "hard-drive" : "shield-check"}
            size={42}
            from={ctl.configured ? "var(--magenta)" : "var(--surface3)"}
            to={ctl.configured ? "var(--primary-soft)" : "var(--surface2)"}
            ink={ctl.configured ? undefined : "var(--text-dim)"}
          />
          <div className="grow">
            <div className="semi">{ctl.modelTitle}</div>
            <div className="small dim">{ctl.configured ? t("sup.checklist") : t("sup.configure_hint")}</div>
          </div>
          <Badge text={local ? t("sup.mode_local") : t("sup.mode_api")} tone={local ? "accent" : "primary"} />
          <Button compact icon="settings" onClick={() => go("settings")}>{t("nav.settings")}</Button>
        </div>
      </Card>

      <div>
        <Segmented
          options={[
            { value: "summaries", title: `${t("sup.summaries")} · ${summaries.length}`, icon: "scroll-text" },
            { value: "incidents", title: `${t("sup.incidents")} · ${incidents.length}`, icon: "triangle-alert" },
            { value: "decisions", title: `${t("sup.approvals")} · ${decisions.length}`, icon: "hand" },
          ]}
          value={tab}
          onChange={setTab}
        />
      </div>

      {tab === "summaries" &&
        (summaries.length === 0 ? (
          <EmptyState icon="scroll-text" title={t("sup.no_summaries_title")} text={t("sup.no_summaries")} />
        ) : (
          summaries.map((s, i) => (
            <Card key={s.id} stagger={i}>
              <div className="col" style={{ gap: 10 }}>
                <div className="row">
                  <Icon name="scroll-text" size={15} color="var(--primary-soft)" />
                  <span className="semi">{s.when}</span>
                  <Badge text={s.triggerTitle} tone={s.trigger === "final" ? "success" : "primary"} />
                  <span className="spacer" />
                  <span className="small mute">{t("sup.delivered", { n: s.recipients })}</span>
                  <IconButton
                    icon="copy"
                    doneIcon="check"
                    size={28}
                    tip={t("common.copy")}
                    onClick={async () => {
                      await host.copy(s.content);
                      toast("info", t("toast.copied"));
                    }}
                  />
                </div>
                <div className="dim wrap selectable" style={{ lineHeight: 1.45 }}>{s.content}</div>
              </div>
            </Card>
          ))
        ))}

      {tab === "incidents" &&
        (incidents.length === 0 ? (
          <EmptyState icon="badge-check" title={t("sup.no_incidents_title")} text={t("sup.no_incidents")} />
        ) : (
          incidents.map((inc, i) => (
            <Card key={inc.id} stagger={i} padding={16} glow={inc.status === "escalated"} glowColor="var(--warning)">
              <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
                <div className="icon-tile" style={{ width: 34, height: 34, borderRadius: 10, background: alpha(tone(inc.tone), 0.15), color: inc.open ? tone(inc.tone) : "var(--success)" }}>
                  <Icon name={inc.open ? "triangle-alert" : "circle-check"} size={16} />
                </div>
                <div className="col grow" style={{ gap: 5 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="semi">{inc.kindTitle}</span>
                    <Badge text={inc.severityTitle} tone={inc.tone} />
                    <Badge text={inc.statusTitle} tone={inc.open ? "warning" : "success"} />
                    <span className="spacer" />
                    <span className="small mute">{inc.when}</span>
                  </div>
                  <div className="dim wrap selectable">{inc.description}</div>
                  {inc.resolution && <div className="small mute wrap">→ {inc.resolution}</div>}
                </div>
                {inc.open && (
                  <Button
                    compact
                    icon="check"
                    onClick={() => {
                      setResolution("");
                      setResolving({ id: inc.id, text: inc.description });
                    }}
                  >
                    {t("sup.resolve")}
                  </Button>
                )}
              </div>
            </Card>
          ))
        ))}

      {tab === "decisions" &&
        (decisions.length === 0 ? (
          <EmptyState icon="hand" title={t("sup.no_approvals_title")} text={t("sup.no_approvals")} />
        ) : (
          decisions.map((d, i) => (
            <Card key={d.id} stagger={i} padding={16}>
              <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
                <span className="dot" style={{ width: 10, height: 10, marginTop: 5, background: tone(d.tone) }} />
                <div className="col grow" style={{ gap: 4 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="semi">{d.reasonTitle}</span>
                    <Badge text={d.decisionTitle} tone={d.tone} />
                    <span className="spacer" />
                    <span className="small mute">{d.when}</span>
                  </div>
                  <div className="dim wrap">{d.question}</div>
                  {(d.comment || d.agent) && (
                    <div className="small mute wrap">{[d.agent, d.comment].filter(Boolean).join(" · ")}</div>
                  )}
                </div>
              </div>
            </Card>
          ))
        ))}

      <Sheet
        open={resolving !== null}
        onClose={() => setResolving(null)}
        title={t("sup.resolve")}
        subtitle={resolving?.text}
        icon="badge-check"
        width={520}
        footer={
          <>
            <span className="spacer" />
            <Button variant="ghost" onClick={() => setResolving(null)}>{t("common.cancel")}</Button>
            <Button
              variant="primary"
              icon="check"
              onClick={() => {
                if (resolving) void call("supervisor", "resolveIncident", resolving.id, resolution);
                setResolving(null);
              }}
            >
              {t("sup.resolve")}
            </Button>
          </>
        }
      >
        <TextBox label={t("sup.resolution")} minHeight={110} autoFocus value={resolution} onChange={setResolution} />
      </Sheet>
    </Page>
  );
}
