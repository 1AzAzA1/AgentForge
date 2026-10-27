// Настройки: интерфейс, human-in-the-loop, супервайзер, выполнение,
// инструменты, песочница, доступные каталоги и безопасность профиля.

import { useEffect, useState, type ReactNode } from "react";
import { call } from "../api/bridge";
import { useCtl, useT } from "../api/hooks";
import { host } from "../api/host";
import { Badge, SectionTitle } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Chip, Field, RangeSlider, Segmented, Select, Toggle } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { Sheet } from "../ui/Sheet";
import { Spinner } from "../ui/Spinner";
import { toast } from "../ui/Toasts";
import { OrbitLogo } from "../ui/Visuals";

function Section({ icon, title, index, disabled = false, children }: {
  icon: string; title: string; index: number; disabled?: boolean; children: ReactNode;
}) {
  return (
    <Card stagger={index} dimmed={disabled} className={disabled ? "is-inert" : ""}>
      <div className="col" style={{ gap: 14 }}>
        <SectionTitle text={title} icon={icon} />
        {children}
      </div>
    </Card>
  );
}

/** Поле, которое сохраняет значение при уходе из него или по Enter. */
function CommitField(props: { label: string; value: string; icon: string; onCommit: (v: string) => void }) {
  const [value, setValue] = useState(props.value);
  useEffect(() => setValue(props.value), [props.value]);
  return (
    <Field
      label={props.label}
      icon={props.icon}
      mono
      value={value}
      onChange={setValue}
      onCommit={(v) => v !== props.value && props.onCommit(v)}
    />
  );
}

export function Settings() {
  const t = useT();
  const backend = useCtl("backend");
  const ctl = useCtl("prefs");
  const ws = ctl.ws ?? {};
  const hasWs = backend.workspaceId >= 0;
  const set = (key: string, value: unknown) => call("prefs", "setValue", key, value);
  const [searchKey, setSearchKey] = useState("");
  const [pwdOpen, setPwdOpen] = useState(false);
  const [pwd, setPwd] = useState({ old: "", next: "", next2: "" });
  const [pwdError, setPwdError] = useState("");

  const saveSearchKey = () => {
    void call("prefs", "setSearchKey", searchKey);
    setSearchKey("");
  };
  const addPath = async () => {
    const folder = await host.chooseFolder();
    if (!folder) return;
    const err = await call<string>("prefs", "addPath", folder);
    if (err) toast("warning", err, folder);
  };
  const changePassword = async () => {
    const err = await call<string>("prefs", "changePassword", pwd.old, pwd.next, pwd.next2);
    setPwdError(err);
    if (!err) setPwdOpen(false);
  };
  const supMode = ws.supervisor_mode || "api";
  const searchBackend = ws.search_backend || "duckduckgo";

  return (
    <Page title={t("settings.title")} subtitle={t("settings.subtitle")} icon="settings">
      <div className="settings-grid">
        <Section icon="monitor" title={t("settings.interface")} index={0}>
          <div className="row">
            <span className="grow">{t("settings.language")}</span>
            <Segmented
              options={(backend.languages ?? []).map((l: any) => ({ value: l.code, title: l.title }))}
              value={backend.lang}
              onChange={(v) => call("backend", "setLanguage", v)}
            />
          </div>
          <div className="row" style={{ gap: 14 }}>
            <div className="grow">
              <div>{t("settings.motion")}</div>
              <div className="small mute">{t("settings.motion_hint")}</div>
            </div>
            <OrbitLogo size={34} />
          </div>
          <Segmented
            stretch
            options={[
              { value: "full", title: t("settings.motion_full"), icon: "sparkles" },
              { value: "reduced", title: t("settings.motion_reduced"), icon: "gauge" },
              { value: "off", title: t("settings.motion_off"), icon: "pause" },
            ]}
            value={backend.motion}
            onChange={(v) => call("backend", "setMotion", v)}
          />
        </Section>

        <Section icon="hand" title={t("settings.hitl_title")} index={1} disabled={!hasWs}>
          <Toggle label={t("settings.hitl")} hint={t("settings.hitl_hint")} checked={Boolean(ws.human_in_the_loop)} onChange={(v) => set("human_in_the_loop", v)} />
          <RangeSlider
            label={t("settings.hitl_threshold")}
            min={0}
            max={1}
            step={0.05}
            disabled={!ws.human_in_the_loop}
            value={ws.hitl_confidence_threshold ?? 0}
            format={(v) => (v <= 0 ? t("settings.never") : v.toFixed(2))}
            onCommit={(v) => set("hitl_confidence_threshold", v)}
          />
          <div className="small mute">{t("settings.hitl_threshold_hint")}</div>
          <Toggle
            label={t("settings.hitl_milestone")}
            hint={t("settings.hitl_milestone_hint")}
            disabled={!ws.human_in_the_loop}
            checked={Boolean(ws.hitl_pause_on_milestone)}
            onChange={(v) => set("hitl_pause_on_milestone", v)}
          />
        </Section>

        <Section icon="shield-check" title={t("settings.supervisor")} index={2} disabled={!hasWs}>
          <Segmented
            stretch
            options={[
              { value: "api", title: t("settings.sup_api_short"), icon: "key-round" },
              { value: "local", title: t("settings.sup_local_short"), icon: "hard-drive" },
            ]}
            value={supMode}
            onChange={(v) => set("supervisor_mode", v)}
          />
          {supMode === "api" ? (
            <Select
              label={t("settings.supervisor_agent")}
              icon="bot"
              options={(ctl.supervisorOptions ?? []).map((o: any) => ({ value: o.id, title: o.title }))}
              value={ws.supervisor_agent_id ?? -1}
              onChange={(v) => set("supervisor_agent_id", v)}
            />
          ) : (
            <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
              <CommitField label={t("settings.local_model")} icon="cpu" value={ws.supervisor_local_model ?? ""} onCommit={(v) => set("supervisor_local_model", v)} />
              <CommitField label={t("keys.base_url")} icon="globe" value={ws.supervisor_local_base_url ?? ""} onCommit={(v) => set("supervisor_local_base_url", v)} />
            </div>
          )}
          <RangeSlider
            label={t("settings.summary_interval")}
            min={0}
            max={120}
            step={5}
            value={ws.summary_interval_minutes ?? 0}
            format={(v) => (v <= 0 ? t("settings.off") : `${Math.round(v)} ${t("settings.min")}`)}
            onCommit={(v) => set("summary_interval_minutes", v)}
          />
          <Toggle label={t("settings.summary_on_event")} hint={t("settings.summary_cost_hint")} checked={Boolean(ws.summary_on_event)} onChange={(v) => set("summary_on_event", v)} />
          <Toggle label={t("settings.anonymize")} hint={t("settings.anonymize_hint")} checked={ws.anonymize_summaries !== false} onChange={(v) => set("anonymize_summaries", v)} />
        </Section>

        <Section icon="workflow" title={t("settings.execution")} index={3} disabled={!hasWs}>
          <RangeSlider label={t("settings.max_steps")} min={1} max={50} step={1} decimals={0} value={ws.agent_max_steps ?? 10} onCommit={(v) => set("agent_max_steps", v)} />
          <RangeSlider label={t("settings.rework_rounds")} min={0} max={10} step={1} decimals={0} value={ws.max_rework_rounds ?? 2} onCommit={(v) => set("max_rework_rounds", v)} />
          <RangeSlider label={t("settings.parallel")} min={1} max={16} step={1} decimals={0} value={ws.max_parallel_agents ?? 6} onCommit={(v) => set("max_parallel_agents", v)} />
        </Section>

        <Section icon="zap" title={t("settings.tools")} index={4} disabled={!hasWs}>
          <div className="small mute">{t("settings.tools_hint")}</div>
          <div className="wrap-row">
            {(ctl.toolSwitches ?? []).map((s: any) => (
              <Chip key={s.key} text={s.title} on={(ws.tools_enabled ?? []).includes(s.key)} onChange={(v) => call("prefs", "setTool", s.key, v)} />
            ))}
          </div>
          <div className="field-label">{t("settings.search_backend")}</div>
          <div>
            <Segmented
              options={[{ value: "duckduckgo", title: "DuckDuckGo" }, { value: "tavily", title: "Tavily" }, { value: "brave", title: "Brave" }]}
              value={searchBackend}
              onChange={(v) => set("search_backend", v)}
            />
          </div>
          {searchBackend !== "duckduckgo" && (
            <div className="row" style={{ gap: 10, alignItems: "flex-end" }}>
              <Field
                label={t("settings.search_key")}
                placeholder={ctl.hasSearchKey ? t("settings.search_key_set") : "tvly-..."}
                password
                mono
                icon="lock"
                value={searchKey}
                onChange={setSearchKey}
                onEnter={saveSearchKey}
              />
              <Button icon="check" onClick={saveSearchKey}>{t("common.save")}</Button>
            </div>
          )}
          <Toggle label={t("settings.fetch_pages")} checked={ws.fetch_pages !== false} onChange={(v) => set("fetch_pages", v)} />
        </Section>

        <Section icon="box" title={t("settings.sandbox")} index={5} disabled={!hasWs}>
          <div className="row">
            <Segmented
              options={[
                { value: "auto", title: t("settings.sb_auto") },
                { value: "subprocess", title: t("settings.sb_process") },
                { value: "docker", title: "Docker" },
              ]}
              value={ws.sandbox_backend || "auto"}
              onChange={(v) => set("sandbox_backend", v)}
            />
            <span className="spacer" />
            {ctl.docker === "checking" ? (
              <Spinner size={14} />
            ) : (
              <Badge
                text={ctl.docker === "yes" ? t("settings.docker_yes") : t("settings.docker_no")}
                tone={ctl.docker === "yes" ? "success" : "warning"}
                icon={ctl.docker === "yes" ? "circle-check" : "triangle-alert"}
              />
            )}
            <IconButton icon="refresh-cw" tip={t("settings.docker_recheck")} onClick={() => call("prefs", "checkDocker")} />
          </div>
          <div className="small mute">{ctl.docker === "yes" ? t("settings.docker_note_yes") : t("settings.docker_note_no")}</div>
          <div className="row" style={{ gap: 20 }}>
            <RangeSlider label={t("settings.sb_timeout")} min={5} max={300} step={5} decimals={0} suffix=" s" value={ws.sandbox_timeout_sec ?? 30} onCommit={(v) => set("sandbox_timeout_sec", v)} />
            <RangeSlider label={t("settings.sb_memory")} min={64} max={4096} step={64} decimals={0} suffix=" MB" value={ws.sandbox_memory_mb ?? 512} onCommit={(v) => set("sandbox_memory_mb", v)} />
          </div>
        </Section>

        <Section icon="folder-open" title={t("settings.allowed_paths")} index={6} disabled={!hasWs}>
          <div className="small" style={{ color: "var(--warning)" }}>{t("settings.paths_warning")}</div>
          {(ws.extra_allowed_paths ?? []).map((p: string) => (
            <div key={p} className="path-row">
              <Icon name="folder" size={14} color="var(--text-mute)" />
              <span className="mono small grow ellipsis selectable" title={p}>{p}</span>
              <IconButton icon="x" size={28} danger onClick={() => call("prefs", "removePath", p)} />
            </div>
          ))}
          {(ws.extra_allowed_paths ?? []).length === 0 && <div className="small mute">{t("settings.paths_empty")}</div>}
          <div>
            <Button icon="plus" onClick={addPath}>{t("settings.add_path")}</Button>
          </div>
        </Section>

        <Section icon="lock" title={t("settings.security")} index={7}>
          <div className="small dim">{t("settings.security_text")}</div>
          <div className="row">
            <Button
              icon="key-round"
              onClick={() => {
                setPwd({ old: "", next: "", next2: "" });
                setPwdError("");
                setPwdOpen(true);
              }}
            >
              {t("settings.change_password")}
            </Button>
            <Button icon="folder-open" onClick={() => call("backend", "openPath", backend.dataRoot)}>{t("settings.open_data")}</Button>
          </div>
          <div className="micro mute mono ellipsis selectable">{backend.dataRoot}</div>
        </Section>
      </div>

      <Sheet
        open={pwdOpen}
        onClose={() => setPwdOpen(false)}
        title={t("settings.change_password")}
        subtitle={t("settings.password_note")}
        icon="key-round"
        width={460}
        footer={
          <>
            <span className="spacer" />
            <Button variant="ghost" onClick={() => setPwdOpen(false)}>{t("common.cancel")}</Button>
            <Button variant="primary" icon="check" onClick={changePassword}>{t("common.save")}</Button>
          </>
        }
      >
        <Field label={t("settings.old_password")} password icon="lock" autoFocus value={pwd.old} onChange={(old) => setPwd({ ...pwd, old })} />
        <Field label={t("login.password")} password icon="lock" value={pwd.next} onChange={(next) => setPwd({ ...pwd, next })} />
        <Field label={t("login.password2")} password icon="lock" value={pwd.next2} error={pwdError} onChange={(next2) => setPwd({ ...pwd, next2 })} onEnter={changePassword} />
      </Sheet>
    </Page>
  );
}
