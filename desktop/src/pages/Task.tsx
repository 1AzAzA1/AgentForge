// Постановка задачи слева, подзадачи справа: ручные или от ИИ-планировщика,
// с исполнителями и зависимостями.

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { go } from "../api/nav";
import { Badge, EmptyState, SectionTitle } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Chip, Field, Segmented, Select, TextBox } from "../ui/Forms";
import { Page } from "../ui/Page";
import { askConfirm, Sheet } from "../ui/Sheet";
import { Skeleton } from "../ui/Spinner";
import { toast } from "../ui/Toasts";
import { alpha, statusColor, useMotionPrefs } from "../ui/theme";

interface SubDraft {
  id: number;
  title: string;
  description: string;
  agentId: number;
  deps: number[];
}

export function Task() {
  const t = useT();
  const m = useMotionPrefs();
  const ctl = useCtl("task");
  const backend = useCtl("backend");
  const subtasks = useList("task", "subtasks");
  const hasWs = backend.workspaceId >= 0;

  const [title, setTitle] = useState(ctl.title ?? "");
  const [body, setBody] = useState(ctl.description ?? "");
  const [limit, setLimit] = useState(ctl.tokenLimit ?? "");
  const [fmt, setFmt] = useState(ctl.format ?? "auto");
  const [error, setError] = useState("");
  const titleRef = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLDivElement>(null);

  // Данные с сервера заменяют поле, только если человек его сейчас не правит.
  const focused = () => formRef.current?.contains(document.activeElement) ?? false;
  useEffect(() => {
    if (focused()) return;
    setTitle(ctl.title ?? "");
    setBody(ctl.description ?? "");
    setLimit(ctl.tokenLimit ?? "");
    setFmt(ctl.format ?? "auto");
  }, [ctl.taskId, ctl.title, ctl.description, ctl.tokenLimit, ctl.format]);

  const save = async () => {
    const err = await call<string>("task", "saveTask", title, body, fmt, limit);
    setError(err);
    if (!err) toast("success", t("toast.task_saved"));
    return !err;
  };

  const [sub, setSub] = useState<SubDraft | null>(null);
  const [subError, setSubError] = useState("");
  const agentOptions: any[] = ctl.agentOptions ?? [];
  const openNewSub = () => {
    setSubError("");
    setSub({ id: -1, title: "", description: "", agentId: agentOptions.length ? agentOptions[0].id : -1, deps: [] });
  };
  const saveSub = async () => {
    if (!sub) return;
    const err = await call<string>("task", "saveSubtask", sub);
    if (err) setSubError(err);
    else setSub(null);
  };

  const statusTone = ctl.status === "done" ? "success" : ctl.status === "running" ? "accent" : ctl.status === "failed" ? "error" : "muted";

  return (
    <Page
      title={t("task.title")}
      subtitle={t("task.subtitle")}
      icon="list-checks"
      actions={
        <>
          {ctl.hasTask && (
            <Button
              icon="square-pen"
              disabled={backend.running}
              onClick={() =>
                askConfirm({
                  title: t("task.new_task"),
                  text: t("task.new_task_confirm"),
                  danger: false,
                  yesText: t("task.new_task"),
                  onYes: async () => {
                    const e = await call<string>("task", "newTask");
                    if (e) toast("warning", e);
                  },
                })
              }
            >
              {t("task.new_task")}
            </Button>
          )}
          <Button
            variant="primary"
            icon="play"
            disabled={backend.running || !hasWs}
            onClick={async () => {
              if (await save()) {
                go("run");
                void call("run", "start");
              }
            }}
          >
            {t("run.start")}
          </Button>
        </>
      }
    >
      {!hasWs ? (
        <EmptyState icon="layers" title={t("ws.empty_title")} text={t("ws.empty")} actionText={t("nav.workspaces")} actionIcon="arrow-right" onAction={() => go("workspaces")} />
      ) : (
        <div className="task-layout">
          <Card className="task-form" stagger={0}>
            <div ref={formRef} className="col" style={{ gap: 16 }}>
              <SectionTitle text={t("task.statement")} icon="square-pen">
                {ctl.status && <Badge text={ctl.statusTitle} tone={statusTone} />}
              </SectionTitle>
              <Field label={t("task.name")} placeholder={t("task.untitled")} icon="sparkles" inputRef={titleRef} value={title} onChange={setTitle} />
              <TextBox label={t("task.body")} placeholder={t("task.placeholder")} minHeight={240} value={body} onChange={setBody} />
              <div className="field-label">{t("task.result_format")}</div>
              <Segmented
                stretch
                options={(ctl.formats ?? []).map((f: any) => ({ value: f.key, title: f.title, icon: f.icon }))}
                value={fmt}
                onChange={setFmt}
              />
              <Field
                label={t("task.token_limit")}
                placeholder={t("task.token_limit_hint")}
                hint={t("task.token_limit_note")}
                icon="gauge"
                mono
                value={limit}
                onChange={setLimit}
              />
              {error && <div className="small" style={{ color: "var(--danger)" }}>{error}</div>}
              <Button variant="primary" icon="check" block onClick={save}>{t("task.save")}</Button>
            </div>
          </Card>

          <div className="col task-subs" style={{ gap: 12 }}>
            <SectionTitle
              text={t("task.subtasks") + (subtasks.length ? `  ·  ${subtasks.length}` : "")}
              hint={t("task.subtasks_hint")}
              icon="workflow"
            >
              <Button compact icon="plus" onClick={async () => { if (!ctl.hasTask && !(await save())) return; openNewSub(); }}>
                {t("task.add_subtask")}
              </Button>
              <Button
                compact
                variant="primary"
                icon="wand-sparkles"
                loading={Boolean(ctl.planning)}
                onClick={async () => { if (await save()) void call("task", "autosplit"); }}
              >
                {t("task.autosplit")}
              </Button>
            </SectionTitle>

            {ctl.planning &&
              [0, 1, 2].map((i) => (
                <Card key={`sk-${i}`} padding={16}>
                  <div className="col" style={{ gap: 10 }}>
                    <Skeleton width="55%" height={14} />
                    <Skeleton height={10} />
                    <Skeleton width="35%" height={10} />
                  </div>
                </Card>
              ))}

            {subtasks.length === 0 && !ctl.planning && (
              <Card>
                <EmptyState icon="workflow" title={t("task.no_subtasks_title")} text={t("task.no_subtasks")} />
              </Card>
            )}

            <AnimatePresence initial={false}>
              {subtasks.map((s, index) => {
                const color = statusColor(s.status);
                return (
                  <motion.div
                    key={s.id}
                    layout={m.on ? "position" : false}
                    initial={{ opacity: 0, scale: 0.96 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0 }}
                    transition={m.rich ? { type: "spring", stiffness: 380, damping: 26 } : m.ease(220)}
                  >
                    <Card padding={16} hoverable glow={s.status === "running"} glowColor="var(--cyan)">
                      <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
                        <div className="sub-index" style={{ color, background: alpha(color, 0.16), borderColor: alpha(color, 0.5) }}>
                          {s.index}
                        </div>
                        <div className="col grow" style={{ gap: 6 }}>
                          <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                            <span className="semi grow wrap">{s.title}</span>
                            <Badge text={s.statusTitle} tint={color} />
                          </div>
                          <div className="wrap-row" style={{ gap: 6 }}>
                            <Badge text={s.agentName} icon={s.agentId >= 0 ? "bot" : "circle-alert"} tone={s.agentId >= 0 ? "primary" : "warning"} />
                            {(s.depTitles as string[]).map((d) => <Badge key={d} text={d} icon="git-branch" tone="muted" />)}
                            {s.reworks > 0 && <Badge text={t("task.reworks", { n: s.reworks })} tone="warning" icon="rotate-ccw" />}
                            {s.tokens !== "0" && <Badge text={`${s.tokens} · ${s.cost}`} tone="muted" icon="coins" />}
                          </div>
                          {s.description && <div className="small dim clamp3">{s.description}</div>}
                          {s.result && <div className="sub-result small dim clamp3">{s.resultPreview}</div>}
                        </div>
                        <div className="sub-actions">
                          <IconButton icon="chevron-up" size={28} disabled={index === 0} tip={t("task.move_up")} onClick={() => call("task", "move", s.id, -1)} />
                          <IconButton icon="chevron-down" size={28} disabled={index === subtasks.length - 1} tip={t("task.move_down")} onClick={() => call("task", "move", s.id, 1)} />
                          <IconButton
                            icon="pencil"
                            size={28}
                            tip={t("common.edit")}
                            onClick={() => {
                              setSubError("");
                              setSub({ id: s.id, title: s.title, description: s.description, agentId: s.agentId, deps: s.deps });
                            }}
                          />
                          <IconButton
                            icon="trash-2"
                            size={28}
                            danger
                            tip={t("common.delete")}
                            onClick={() => askConfirm({ title: t("task.delete_subtask"), text: s.title, onYes: () => call("task", "removeSubtask", s.id) })}
                          />
                          {["done", "error", "review"].includes(s.status) && (
                            <IconButton icon="rotate-ccw" size={28} tip={t("task.rerun")} onClick={() => call("task", "resetSubtask", s.id)} />
                          )}
                        </div>
                      </div>
                    </Card>
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>
        </div>
      )}

      <Sheet
        open={sub !== null}
        onClose={() => setSub(null)}
        title={sub && sub.id >= 0 ? t("task.edit_subtask") : t("task.add_subtask")}
        icon="workflow"
        width={620}
        footer={
          <>
            <span className="spacer" />
            <Button variant="ghost" onClick={() => setSub(null)}>{t("common.cancel")}</Button>
            <Button variant="primary" icon="check" onClick={saveSub}>{t("common.save")}</Button>
          </>
        }
      >
        {sub && (
          <>
            <Field label={t("task.subtask_title")} icon="square-pen" autoFocus value={sub.title} error={subError} onChange={(v) => setSub({ ...sub, title: v })} onEnter={saveSub} />
            <TextBox label={t("common.description")} minHeight={120} value={sub.description} onChange={(v) => setSub({ ...sub, description: v })} />
            <Select
              label={t("task.assignee")}
              icon="bot"
              options={[{ value: -1, title: t("task.unassigned") }, ...agentOptions.map((a) => ({ value: a.id, title: `${a.name} · ${a.model}` }))]}
              value={sub.agentId}
              onChange={(v) => setSub({ ...sub, agentId: v })}
            />
            <SectionTitle text={t("task.depends_on")} hint={t("task.depends_hint")} icon="git-branch" />
            <div className="wrap-row">
              {subtasks.filter((s) => s.id !== sub.id).map((s) => (
                <Chip
                  key={s.id}
                  text={`${s.index}. ${s.title}`}
                  on={sub.deps.includes(s.id)}
                  onChange={(on) => setSub({ ...sub, deps: on ? [...sub.deps, s.id] : sub.deps.filter((d) => d !== s.id) })}
                />
              ))}
            </div>
            {subtasks.length <= (sub.id >= 0 ? 1 : 0) && <div className="small mute">{t("task.no_deps_possible")}</div>}
          </>
        )}
      </Sheet>
    </Page>
  );
}
