// Бюджеты: по одной карточке на уровень (проект, задача, агент) с живой шкалой
// расхода и полями лимитов, которые сохраняются по Enter или уходу из поля.

import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useList, useT } from "../api/hooks";
import { EmptyState, Hint, IconTile, Notice } from "../ui/Bits";
import { IconButton } from "../ui/Button";
import { Card } from "../ui/Card";
import { Field, RangeSlider } from "../ui/Forms";
import { Page } from "../ui/Page";
import { useMotionPrefs } from "../ui/theme";

export function Budget() {
  const t = useT();
  const ctl = useCtl("budget");
  const items = useList("budget", "model");
  return (
    <Page
      title={t("bud.title")}
      subtitle={t("bud.subtitle")}
      icon="wallet"
      actions={<IconButton icon="refresh-cw" tip={t("common.refresh")} onClick={() => call("budget", "refresh")} />}
    >
      {ctl.alert && (
        <Notice tone={ctl.alertTone} action={<IconButton icon="x" size={26} onClick={() => call("budget", "dismissAlert")} />}>
          {ctl.alert}
        </Notice>
      )}
      <Card padding={16}>
        <div className="row" style={{ gap: 18, alignItems: "flex-start" }}>
          <Hint icon="shield-check">{t("bud.h_before")}</Hint>
          <Hint icon="hand">{t("bud.h_ask")}</Hint>
          <Hint icon="database">{t("bud.h_log")}</Hint>
        </div>
      </Card>
      {items.length === 0 && <EmptyState icon="wallet" title={t("bud.nothing_title")} text={t("bud.nothing")} />}
      {items.map((b, i) => <BudgetCard key={b.id} b={b} index={i} />)}
    </Page>
  );
}

function BudgetCard({ b, index }: { b: any; index: number }) {
  const t = useT();
  const m = useMotionPrefs();
  const [tokens, setTokens] = useState(b.tokenLimit);
  const [cost, setCost] = useState(b.costLimit);
  const [error, setError] = useState("");
  useEffect(() => setTokens(b.tokenLimit), [b.tokenLimit]);
  useEffect(() => setCost(b.costLimit), [b.costLimit]);

  const save = async (tok: string, usd: string, threshold: number) => {
    setError(await call<string>("budget", "save", b.scope, b.scopeId, tok, usd, threshold));
  };
  const color = b.ratio >= 1 ? "var(--danger)" : b.ratio >= b.threshold ? "var(--warning)" : "var(--primary)";

  return (
    <Card stagger={index} hoverable glow={b.exceeded} glowColor="var(--danger)">
      <div className="col" style={{ gap: 14 }}>
        <div className="row" style={{ gap: 12 }}>
          <IconTile icon={b.icon} />
          <div className="grow">
            <div className="h3 ellipsis">{b.name}</div>
            <div className="small mute">{b.scopeTitle}</div>
          </div>
          <div className="col" style={{ gap: 1, alignItems: "flex-end" }}>
            <span className="mono">{b.tokensText} {t("bud.tokens_short")}</span>
            <span className="mono" style={{ color: "var(--teal)" }}>{b.costText}</span>
          </div>
        </div>
        {b.isSet && (
          <div className="col" style={{ gap: 6 }}>
            <div className="budget-bar">
              <div className="track" style={{ height: 10 }}>
                <motion.div
                  className="track-fill"
                  style={{ background: `linear-gradient(90deg, color-mix(in srgb, ${color} 70%, transparent), ${color})` }}
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.min(1, b.ratio) * 100}%` }}
                  transition={m.ease(840)}
                />
              </div>
              <span className="budget-mark" style={{ left: `${b.threshold * 100}%` }} />
            </div>
            <span className="small" style={{ color }}>
              {b.exceeded ? t("bud.exceeded") : t("bud.used_pct", { pct: Math.round(b.ratio * 100) })}
            </span>
          </div>
        )}
        <div className="row" style={{ gap: 14, alignItems: "flex-start" }}>
          <Field
            label={t("bud.token_limit")}
            placeholder={t("bud.no_limit")}
            icon="coins"
            mono
            value={tokens}
            onChange={setTokens}
            onCommit={(v) => v !== b.tokenLimit && save(v, cost, b.threshold)}
          />
          <Field
            label={t("bud.cost_limit")}
            placeholder={t("bud.no_limit")}
            icon="dollar-sign"
            mono
            value={cost}
            onChange={setCost}
            onCommit={(v) => v !== b.costLimit && save(tokens, v, b.threshold)}
          />
          <div style={{ width: 220, flex: "none" }}>
            <RangeSlider
              label={t("bud.alert_at")}
              min={0.1}
              max={1}
              step={0.05}
              value={b.threshold}
              format={(v) => `${Math.round(v * 100)}%`}
              onCommit={(v) => b.isSet && save(tokens, cost, v)}
            />
          </div>
        </div>
        {error && <div className="small" style={{ color: "var(--danger)" }}>{error}</div>}
      </div>
    </Card>
  );
}
