// API-ключи: карточки подключений с живой проверкой и мастер добавления,
// где провайдер выбирается плиткой, а адрес подставляется сам.

import { useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { host } from "../api/host";
import { Badge, EmptyState, IconTile } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Field } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { askConfirm, Sheet } from "../ui/Sheet";
import { Spinner } from "../ui/Spinner";

interface Draft {
  id: number;
  provider: string;
  label: string;
  url: string;
  secret: string;
}

export function Keys() {
  const t = useT();
  const ctl = useCtl("keys");
  const items = useList("keys", "model");
  const presets: any[] = ctl.presets ?? [];
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");
  const preset = presets.find((p) => p.key === draft?.provider) ?? presets[0] ?? {};

  const openNew = () => {
    const p = presets.find((x) => x.key === "openai") ?? presets[0];
    setError("");
    setDraft({ id: -1, provider: p?.key ?? "openai", label: "", url: p?.baseUrl ?? "", secret: "" });
  };
  const save = async () => {
    if (!draft) return;
    const err = draft.id >= 0
      ? await call<string>("keys", "update", draft.id, draft.label, draft.url, draft.secret)
      : await call<string>("keys", "create", draft.provider, draft.label, draft.url, draft.secret);
    if (err) setError(err);
    else setDraft(null);
  };

  return (
    <Page
      title={t("keys.title")}
      subtitle={t("keys.subtitle")}
      icon="key-round"
      actions={<Button variant="primary" icon="plus" onClick={openNew}>{t("keys.new")}</Button>}
    >
      {items.length === 0 && (
        <EmptyState icon="key-round" title={t("keys.empty_title")} text={t("keys.empty")} actionText={t("keys.new")} onAction={openNew} />
      )}
      {items.map((k, i) => (
        <Card key={k.id} hoverable stagger={i} padding={18}>
          <div className="row" style={{ gap: 16, alignItems: "flex-start" }}>
            <IconTile icon={k.local ? "hard-drive" : "key-round"} size={46} color={k.local ? "var(--teal)" : "var(--primary-soft)"} />
            <div className="col grow" style={{ gap: 4 }}>
              <div className="row" style={{ gap: 8 }}>
                <span className="h3 ellipsis">{k.label}</span>
                <Badge text={k.providerTitle} tone="primary" />
                {k.free && <Badge text={t("keys.free")} tone="success" />}
                {k.local && <Badge text={t("keys.local")} tone="accent" icon="hard-drive" />}
              </div>
              <div className="small mute mono ellipsis selectable">{k.baseUrl}</div>
              <div className="row small" style={{ gap: 8 }}>
                <Icon name={k.hasSecret ? "lock" : "circle-dot"} size={12} color={k.hasSecret ? "var(--success)" : "var(--text-mute)"} />
                <span className="dim">{k.hasSecret ? t("keys.stored") : t("keys.no_secret")}</span>
                {k.models > 0 && <span className="mute">· {t("keys.models_cached", { n: k.models })}</span>}
              </div>
              {k.testState && (
                <div className="row small" style={{ gap: 8, alignItems: "flex-start" }}>
                  {k.testState === "testing" ? (
                    <Spinner size={14} />
                  ) : (
                    <Icon name={k.testState === "ok" ? "circle-check" : "circle-x"} size={14} color={k.testState === "ok" ? "var(--success)" : "var(--danger)"} />
                  )}
                  <span
                    className="wrap selectable"
                    style={{ maxWidth: 560, color: k.testState === "fail" ? "var(--danger)" : k.testState === "ok" ? "var(--success)" : "var(--text-dim)" }}
                  >
                    {k.testState === "testing" ? t("keys.testing") : k.testMessage}
                  </span>
                </div>
              )}
            </div>
            <Button compact icon="activity" loading={k.testState === "testing"} onClick={() => call("keys", "test", k.id)}>
              {t("common.test")}
            </Button>
            <IconButton icon="pencil" tip={t("common.edit")} onClick={() => { setError(""); setDraft({ id: k.id, provider: k.provider, label: k.label, url: k.baseUrl, secret: "" }); }} />
            <IconButton
              icon="trash-2"
              danger
              tip={t("common.delete")}
              onClick={() => askConfirm({ title: t("keys.delete_title"), text: t("keys.delete_confirm"), onYes: () => call("keys", "remove", k.id) })}
            />
          </div>
        </Card>
      ))}

      <Sheet
        open={draft !== null}
        onClose={() => setDraft(null)}
        title={draft && draft.id >= 0 ? t("keys.edit") : t("keys.new")}
        subtitle={t("keys.subtitle")}
        icon="key-round"
        width={680}
        footer={
          <>
            <Icon name="shield-check" size={14} color="var(--success)" />
            <span className="small mute grow">{t("keys.encrypted_note")}</span>
            <Button variant="ghost" onClick={() => setDraft(null)}>{t("common.cancel")}</Button>
            <Button variant="primary" icon="check" onClick={save}>{t("common.save")}</Button>
          </>
        }
      >
        {draft && (
          <>
            {draft.id < 0 && (
              <>
                <div className="field-label">{t("keys.provider")}</div>
                <div className="provider-grid">
                  {presets.map((p) => (
                    <button
                      key={p.key}
                      type="button"
                      className={`provider-tile${draft.provider === p.key ? " is-chosen" : ""}`}
                      onClick={() => setDraft({ ...draft, provider: p.key, url: p.baseUrl })}
                    >
                      <span className="small semi ellipsis">{p.title}</span>
                      <span className="row" style={{ gap: 4 }}>
                        {p.free && <Badge text={t("keys.free")} tone="success" />}
                        {p.local && <Badge text={t("keys.local")} tone="accent" />}
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
            {(preset.notes || preset.docsUrl) && (
              <div className="provider-notes">
                {preset.notes && <div className="small dim wrap">{preset.notes}</div>}
                {preset.docsUrl && (
                  <a
                    href={preset.docsUrl}
                    className="small"
                    onClick={(e) => {
                      e.preventDefault();
                      host.openExternal(preset.docsUrl);
                    }}
                  >
                    {t("keys.get_key")} ↗
                  </a>
                )}
              </div>
            )}
            <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
              <Field label={t("keys.label")} placeholder={preset.title ?? ""} icon="star" value={draft.label} onChange={(label) => setDraft({ ...draft, label })} />
              <div style={{ flex: 2, minWidth: 0, display: "flex" }}>
                <Field label={t("keys.base_url")} mono icon="globe" value={draft.url} onChange={(url) => setDraft({ ...draft, url })} />
              </div>
            </div>
            <Field
              label={t("keys.key")}
              password
              mono
              icon="lock"
              placeholder={draft.id >= 0 ? t("keys.keep_secret") : "sk-..."}
              hint={preset.requiresKey === false ? t("keys.no_key_needed") : ""}
              error={error}
              value={draft.secret}
              onChange={(secret) => setDraft({ ...draft, secret })}
              onEnter={save}
            />
          </>
        )}
      </Sheet>
    </Page>
  );
}
