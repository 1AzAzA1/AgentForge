// Воркспейсы: параллельные проекты. Активный подсвечен, щелчок - сделать активным.

import { useState } from "react";
import { call } from "../api/bridge";
import { useList, useT } from "../api/hooks";
import { Avatar, Badge, EmptyState } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Field, TextBox } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { askConfirm, Sheet } from "../ui/Sheet";

interface Draft {
  id: number;
  name: string;
  description: string;
}

export function Workspaces() {
  const t = useT();
  const items = useList("workspaces", "model");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState("");

  const edit = (d: Draft) => {
    setError("");
    setDraft(d);
  };
  const save = async () => {
    if (!draft) return;
    const err = draft.id >= 0
      ? await call<string>("workspaces", "update", draft.id, draft.name, draft.description)
      : await call<string>("workspaces", "create", draft.name, draft.description);
    if (err) setError(err);
    else setDraft(null);
  };

  return (
    <Page
      title={t("ws.title")}
      subtitle={t("ws.subtitle")}
      icon="layers"
      actions={<Button variant="primary" icon="plus" onClick={() => edit({ id: -1, name: "", description: "" })}>{t("ws.new")}</Button>}
    >
      {items.length === 0 ? (
        <EmptyState
          icon="layers"
          title={t("ws.empty_title")}
          text={t("ws.empty")}
          actionText={t("ws.new")}
          onAction={() => edit({ id: -1, name: "", description: "" })}
        />
      ) : (
        <div className="grid" style={{ ["--min" as string]: "340px" }}>
          {items.map((ws, i) => (
            <Card
              key={ws.id}
              className="ws-card"
              hoverable
              glow={ws.active}
              stagger={i}
              onClick={ws.active ? undefined : () => call("workspaces", "select", ws.id)}
            >
              <div className="row" style={{ gap: 12 }}>
                <Avatar
                  letter={String(ws.name || "?")[0].toUpperCase()}
                  round={false}
                  from={ws.active ? "var(--primary)" : "var(--surface3)"}
                  to={ws.active ? "var(--teal)" : "var(--surface2)"}
                  ink={ws.active ? undefined : "var(--text-dim)"}
                />
                <div className="grow">
                  <div className="h3 ellipsis">{ws.name}</div>
                  <div className="small mute">{t("ws.updated", { when: ws.updated })}</div>
                </div>
                {ws.active && <Badge text={t("ws.active")} tone="primary" icon="check" />}
              </div>
              <div className={`clamp2 ws-desc ${ws.description ? "dim" : "mute"}`}>
                {ws.description || t("ws.no_description")}
              </div>
              <div className="row" style={{ gap: 14 }}>
                <Stat icon="bot" value={ws.agents} />
                <Stat icon="coins" value={ws.tokens} />
                <Stat icon="dollar-sign" value={ws.cost} color="var(--teal)" />
                <span className="spacer" />
                <span className="row" style={{ gap: 2 }} onClick={(e) => e.stopPropagation()}>
                  <IconButton icon="pencil" tip={t("common.edit")} onClick={() => edit({ id: ws.id, name: ws.name, description: ws.description })} />
                  <IconButton
                    icon="trash-2"
                    danger
                    tip={t("common.delete")}
                    onClick={() =>
                      askConfirm({ title: t("ws.delete_title"), text: t("ws.delete_confirm"), onYes: () => call("workspaces", "remove", ws.id) })
                    }
                  />
                </span>
              </div>
              {ws.taskTitle && (
                <div className="ws-task">
                  <Icon name="list-checks" size={13} color="var(--text-mute)" />
                  <span className="small dim ellipsis">{ws.taskTitle}</span>
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={draft !== null}
        onClose={() => setDraft(null)}
        title={draft && draft.id >= 0 ? t("ws.edit") : t("ws.new")}
        icon="layers"
        width={480}
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
            <Field
              label={t("ws.name")}
              icon="layers"
              autoFocus
              value={draft.name}
              error={error}
              onChange={(name) => setDraft({ ...draft, name })}
              onEnter={save}
            />
            <TextBox
              label={t("common.description")}
              placeholder={t("ws.desc_placeholder")}
              minHeight={100}
              value={draft.description}
              onChange={(description) => setDraft({ ...draft, description })}
            />
          </>
        )}
      </Sheet>
    </Page>
  );
}

function Stat({ icon, value, color = "var(--text-dim)" }: { icon: string; value: any; color?: string }) {
  return (
    <span className="row small mono" style={{ gap: 5, color }}>
      <Icon name={icon} size={13} color="var(--text-mute)" />
      {value}
    </span>
  );
}
