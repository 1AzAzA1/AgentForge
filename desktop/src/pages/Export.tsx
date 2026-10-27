// Экспорт: формат выбирается плиткой (рекомендованный отмечен и объяснён),
// состав документа - переключателями, путь - полем с кнопкой "Обзор".

import { useEffect, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useT } from "../api/hooks";
import { host } from "../api/host";
import { go } from "../api/nav";
import { Badge, EmptyState, Notice, SectionTitle } from "../ui/Bits";
import { Button, IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Field, Toggle } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { Page } from "../ui/Page";
import { alpha } from "../ui/theme";

export function Export() {
  const t = useT();
  const ctl = useCtl("exporter");
  const backend = useCtl("backend");
  const [path, setPath] = useState(ctl.path ?? "");
  useEffect(() => setPath(ctl.path ?? ""), [ctl.path]);
  useEffect(() => {
    if (backend.workspaceId >= 0) void call("exporter", "refresh");
  }, [backend.workspaceId]);
  const options = ctl.options ?? {};

  const browse = async () => {
    const chosen = await host.saveFile(path);
    if (chosen) void call("exporter", "setPath", chosen);
  };

  return (
    <Page
      title={t("exp.title")}
      subtitle={t("exp.subtitle")}
      icon="package"
      actions={<IconButton icon="refresh-cw" tip={t("common.refresh")} onClick={() => call("exporter", "refresh")} />}
    >
      {backend.workspaceId < 0 ? (
        <EmptyState icon="layers" title={t("ws.empty_title")} text={t("ws.empty")} actionText={t("nav.workspaces")} actionIcon="arrow-right" onAction={() => go("workspaces")} />
      ) : (
        <>
          <Card padding={16}>
            <div className="row" style={{ gap: 12 }}>
              <Icon name={ctl.nothing ? "inbox" : "wand-sparkles"} size={18} color={ctl.nothing ? "var(--text-mute)" : "var(--primary-soft)"} />
              <div className="grow">
                <div className="wrap">{ctl.nothing ? t("exp.nothing") : t("exp.auto_hint", { reason: ctl.reason })}</div>
                <div className="small mute">{ctl.stats}</div>
              </div>
            </div>
          </Card>

          <SectionTitle text={t("exp.format")} icon="file-type" />
          <div className="format-grid">
            {(ctl.formats ?? []).map((f: any, i: number) => {
              const chosen = ctl.format === f.key;
              return (
                <Card key={f.key} hoverable glow={chosen} stagger={i} onClick={() => call("exporter", "setFormat", f.key)} className="format-card">
                  <div className="row">
                    <div className="icon-tile" style={{ width: 40, height: 40, background: chosen ? alpha("var(--primary)", 0.3) : "var(--surface3)", color: chosen ? "white" : "var(--text-dim)" }}>
                      <Icon name={f.icon} size={18} />
                    </div>
                    <span className="spacer" />
                    {ctl.recommended === f.key && <Badge text={t("exp.recommended")} tone="success" icon="sparkles" />}
                  </div>
                  <div className="h3">{f.title}</div>
                  <div className="small dim">{f.hint}</div>
                </Card>
              );
            })}
          </div>

          <div className="export-row">
            <Card>
              <div className="col" style={{ gap: 12 }}>
                <SectionTitle text={t("exp.content")} icon="list-checks" />
                {(ctl.optionList ?? []).map((o: any) => (
                  <Toggle
                    key={o.key}
                    label={o.title}
                    hint={o.key === "files" && ctl.format !== "zip" ? t("exp.files_zip_only") : o.key === "anon" ? t("exp.opt_anon_hint") : ""}
                    disabled={o.key === "files" && ctl.format !== "zip"}
                    checked={Boolean(options[o.key])}
                    onChange={(v) => call("exporter", "setOption", o.key, v)}
                  />
                ))}
              </div>
            </Card>
            <Card>
              <div className="col" style={{ gap: 14 }}>
                <SectionTitle text={t("exp.output")} icon="folder-open" />
                <div className="row" style={{ gap: 10 }}>
                  <Field mono icon="folder" value={path} onChange={setPath} onCommit={(v) => call("exporter", "setPath", v)} />
                  <Button icon="folder-open" onClick={browse}>{t("exp.browse")}</Button>
                </div>
                <Button
                  variant="primary"
                  icon="download"
                  block
                  disabled={!ctl.canExport}
                  loading={Boolean(ctl.exporting)}
                  onClick={async () => {
                    await call("exporter", "setPath", path);
                    void call("exporter", "exportNow");
                  }}
                >
                  {t("exp.export")}
                </Button>
                {ctl.lastPath && (
                  <Notice
                    tone="success"
                    icon="circle-check"
                    action={<Button compact icon="external-link" onClick={() => call("exporter", "openFolder")}>{t("exp.open_folder")}</Button>}
                  >
                    {ctl.lastInfo}
                  </Notice>
                )}
              </div>
            </Card>
          </div>
        </>
      )}
    </Page>
  );
}
