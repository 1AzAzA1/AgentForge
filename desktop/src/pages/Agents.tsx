// Агенты воркспейса: сетка карточек и редактор с шаблонами ролей, загрузкой
// моделей, инструментами и системным промптом.

import { useRef, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { go } from "../api/nav";
import { Avatar, Badge, EmptyState, StatusDot } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Chip, Field, RangeSlider, Select, TextBox, Toggle } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { askConfirm, Sheet } from "../ui/Sheet";

interface Draft {
  id: number;
  role: string;
  name: string;
  prompt: string;
  keyId: number;
  model: string;
  temperature: number;
  maxTokens: number;
  tools: string[];
  isSupervisor: boolean;
}

export function Agents() {
  const t = useT();
  const ctl = useCtl("agents");
  const backend = useCtl("backend");
  const items = useList("agents", "model");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [error, setError] = useState("");
  // Шаблон роли подставляет имя и промпт, но не затирает то, что пользователь
  // уже успел поправить руками.
  const auto = useRef({ name: "", prompt: "" });
  const hasWs = backend.workspaceId >= 0;
  const keyOptions: any[] = ctl.keyOptions ?? [];

  const currentKey = useRef(-1);

  const refreshModels = async (keyId: number) => {
    currentKey.current = keyId;
    const list = await call<string[]>("agents", "modelsForKey", keyId);
    setModels(list);
    return list;
  };

  const applyTemplate = async (key: string) => {
    const info = await call<{ name: string; prompt: string; tools: string[] }>("agents", "templateInfo", key);
    setDraft((d) => {
      if (!d) return d;
      const next = { ...d, role: key, tools: info.tools };
      if (d.name === "" || d.name === auto.current.name) {
        next.name = info.name;
        auto.current.name = info.name;
      }
      if (info.prompt && (d.prompt === "" || d.prompt === auto.current.prompt)) {
        next.prompt = info.prompt;
        auto.current.prompt = info.prompt;
      }
      return next;
    });
  };

  const openNew = async () => {
    const keyId = keyOptions.length ? keyOptions[0].id : -1;
    auto.current = { name: "", prompt: "" };
    setError("");
    setLoadingModels(false);
    const list = await refreshModels(keyId);
    setDraft({ id: -1, role: "analyst", name: "", prompt: "", keyId, model: list[0] ?? "", temperature: 0.7,
      maxTokens: 2048, tools: [], isSupervisor: false });
    await applyTemplate("analyst");
  };

  const openEdit = async (a: any) => {
    auto.current = { name: "", prompt: "" };
    setError("");
    setLoadingModels(false);
    await refreshModels(a.keyId);
    setDraft({ id: a.id, role: a.role, name: a.name, prompt: a.prompt, keyId: a.keyId, model: a.modelName,
      temperature: a.temperature, maxTokens: a.maxTokens, tools: a.tools, isSupervisor: a.isSupervisor });
  };

  const loadModels = async () => {
    if (!draft) return;
    const keyId = draft.keyId;
    setLoadingModels(true);
    setError("");
    const result = await call<{ models: string[]; error: string }>("agents", "loadModels", keyId);
    // Пока шёл запрос, могли выбрать другой ключ: чужой список не нужен.
    if (currentKey.current !== keyId) return;
    setLoadingModels(false);
    if (result.error) setError(result.error);
    else setModels(result.models);
  };

  const save = async () => {
    if (!draft) return;
    const err = await call<string>("agents", "save", draft);
    if (err) setError(err);
    else setDraft(null);
  };

  const toggleTool = (name: string, on: boolean) =>
    setDraft((d) => d && { ...d, tools: on ? [...d.tools.filter((x) => x !== name), name] : d.tools.filter((x) => x !== name) });

  return (
    <Page
      title={t("agents.title")}
      subtitle={t("agents.isolated_note")}
      icon="bot"
      actions={
        <Button variant="primary" icon="plus" disabled={!hasWs || !ctl.hasKeys} onClick={openNew}>
          {t("agents.new")}
        </Button>
      }
    >
      {!hasWs ? (
        <EmptyState icon="layers" title={t("ws.empty_title")} text={t("ws.empty")} actionText={t("nav.workspaces")} actionIcon="arrow-right" onAction={() => go("workspaces")} />
      ) : !ctl.hasKeys ? (
        <EmptyState icon="key-round" title={t("agents.no_keys_title")} text={t("agents.no_keys")} actionText={t("keys.new")} onAction={() => go("keys")} />
      ) : items.length === 0 ? (
        <EmptyState icon="bot" title={t("agents.empty_title")} text={t("agents.empty")} actionText={t("agents.new")} onAction={openNew} />
      ) : (
        <div className="grid" style={{ ["--min" as string]: "360px" }}>
          {items.map((a, i) => (
            <Card key={a.id} className="agent-card" hoverable stagger={i} glow={a.status === "running"} glowColor="var(--cyan)" dimmed={!a.enabled}>
              <div className="row" style={{ gap: 12 }}>
                <div className="avatar-wrap">
                  <Avatar icon={a.icon} size={46} from={a.isSupervisor ? "var(--magenta)" : "var(--primary)"} to={a.isSupervisor ? "var(--primary-soft)" : "var(--cyan)"} />
                  <span className="avatar-status"><StatusDot status={a.status} size={11} /></span>
                </div>
                <div className="grow">
                  <div className="row" style={{ gap: 6 }}>
                    <span className="h3 ellipsis">{a.name}</span>
                    {a.isSupervisor && <Icon name="star" size={14} color="var(--warning)" />}
                  </div>
                  <div className="small mute ellipsis">{a.roleTitle} · {a.statusTitle}</div>
                </div>
                <Toggle checked={a.enabled} onChange={(v) => call("agents", "setEnabled", a.id, v)} />
              </div>
              <div className="row" style={{ gap: 6 }}>
                <Badge text={a.modelName || "-"} tone="accent" icon="cpu" />
                {a.price && <Badge text={a.price} tone="muted" icon="coins" />}
              </div>
              <div className="small dim clamp3 agent-prompt">{a.promptPreview || t("agents.no_prompt")}</div>
              <div className="row" style={{ gap: 6 }}>
                <div className="tool-row grow">
                  {(a.toolTitles as string[]).map((title) => (
                    <span key={title} className="tool-tag micro dim">{title}</span>
                  ))}
                </div>
                <IconButton icon="copy" tip={t("agents.duplicate")} onClick={() => call("agents", "duplicate", a.id)} />
                <IconButton icon="pencil" tip={t("common.edit")} onClick={() => openEdit(a)} />
                <IconButton
                  icon="trash-2"
                  danger
                  tip={t("common.delete")}
                  onClick={() => askConfirm({ title: t("agents.delete_title"), text: t("agents.delete_confirm"), onYes: () => call("agents", "remove", a.id) })}
                />
              </div>
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={draft !== null}
        onClose={() => setDraft(null)}
        title={draft && draft.id >= 0 ? t("agents.edit") : t("agents.new")}
        subtitle={t("agents.isolated_note")}
        icon="bot"
        width={760}
        footer={
          <>
            <span className="spacer" />
            <Button variant="ghost" onClick={() => setDraft(null)}>{t("common.cancel")}</Button>
            <Button variant="primary" icon="check" onClick={save}>{t("common.save")}</Button>
          </>
        }
      >
        {draft && (
          <>
            <div className="field-label">{t("agents.template")}</div>
            <div className="wrap-row">
              {(ctl.templates ?? []).map((tpl: any) => (
                <Chip key={tpl.key} text={tpl.title} icon={tpl.icon} on={draft.role === tpl.key} onChange={() => applyTemplate(tpl.key)} />
              ))}
            </div>
            <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
              <Field label={t("agents.name")} icon="user" value={draft.name} onChange={(name) => setDraft({ ...draft, name })} />
              <Select
                label={t("agents.provider_key")}
                icon="key-round"
                options={keyOptions.map((k) => ({ value: k.id, title: k.title }))}
                value={draft.keyId}
                onChange={async (keyId) => {
                  setLoadingModels(false);
                  setDraft((d) => d && { ...d, keyId });
                  await refreshModels(keyId);
                }}
              />
            </div>
            <div className="row" style={{ gap: 12, alignItems: "flex-end" }}>
              <Select
                label={t("agents.model")}
                icon="cpu"
                editable
                options={models}
                value={draft.model}
                placeholder="gpt-4o-mini"
                onChange={(model) => setDraft((d) => d && { ...d, model })}
              />
              <Button icon="refresh-cw" loading={loadingModels} onClick={loadModels}>{t("agents.load_models")}</Button>
            </div>
            <div className="row" style={{ gap: 24 }}>
              <RangeSlider
                label={t("agents.temperature")}
                min={0}
                max={2}
                step={0.1}
                decimals={1}
                value={draft.temperature}
                onCommit={(temperature) => setDraft((d) => d && { ...d, temperature })}
              />
              <RangeSlider
                label={t("agents.max_tokens")}
                min={256}
                max={32768}
                step={256}
                decimals={0}
                value={draft.maxTokens}
                onCommit={(maxTokens) => setDraft((d) => d && { ...d, maxTokens })}
              />
            </div>
            <div className="field-label">{t("agents.tools")}</div>
            <div className="wrap-row">
              {(ctl.tools ?? []).map((tool: any) => (
                <Chip key={tool.name} text={tool.title} on={draft.tools.includes(tool.name)} onChange={(on) => toggleTool(tool.name, on)} />
              ))}
            </div>
            <TextBox label={t("agents.prompt")} mono minHeight={190} value={draft.prompt} onChange={(prompt) => setDraft({ ...draft, prompt })} />
            <Toggle
              label={t("agents.is_supervisor")}
              hint={t("agents.is_supervisor_hint")}
              checked={draft.isSupervisor}
              onChange={(isSupervisor) => setDraft({ ...draft, isSupervisor })}
            />
            {error && <div className="small" style={{ color: "var(--danger)" }}>{error}</div>}
          </>
        )}
      </Sheet>
    </Page>
  );
}
