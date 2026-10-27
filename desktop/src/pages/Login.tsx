// Экран входа: слева знак и суть продукта, справа карточка входа или создания
// профиля с индикатором надёжности пароля.

import { motion, useAnimate } from "motion/react";
import { useEffect, useState } from "react";
import { call } from "../api/bridge";
import { useCtl, useT } from "../api/hooks";
import { Button } from "../ui/Button";
import { Notice } from "../ui/Bits";
import { Field, Segmented, Select, Toggle } from "../ui/Forms";
import { Icon } from "../ui/Icon";
import { useMotionPrefs } from "../ui/theme";
import { OrbitLogo } from "../ui/Visuals";

const MIN_PASSWORD = 8;

/** Оценка 0..4 для индикатора надёжности при создании профиля. */
export function passwordStrength(password: string): number {
  if (!password) return 0;
  let score = 0;
  if (password.length >= MIN_PASSWORD) score += 1;
  if (password.length >= 12) score += 1;
  const classes = [/[a-zа-яё]/, /[A-ZА-ЯЁ]/, /\d/, /[^\p{L}\p{N}]/u].filter((re) => re.test(password)).length;
  if (classes >= 2) score += 1;
  if (classes >= 3 && password.length >= 10) score += 1;
  return Math.min(score, 4);
}

export function Login() {
  const t = useT();
  const b = useCtl("backend");
  const m = useMotionPrefs();
  const profiles: string[] = b.profiles ?? [];
  const [signUp, setSignUp] = useState(profiles.length === 0);
  const [error, setError] = useState("");
  const [user, setUser] = useState<string>(b.lastUsername ?? "");
  const [password, setPassword] = useState("");
  const [saved, setSaved] = useState(false);
  const [remember, setRemember] = useState(Boolean(b.rememberDefault));
  const [newName, setNewName] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newPass2, setNewPass2] = useState("");
  const [scope, animate] = useAnimate();

  useEffect(() => {
    if (profiles.length === 0) setSignUp(true);
  }, [profiles.length]);
  useEffect(() => {
    if (!user && b.lastUsername) setUser(b.lastUsername);
  }, [b.lastUsername, user]);
  useEffect(() => {
    // Сам сохранённый пароль окну не передаётся: только признак, что он есть.
    let alive = true;
    if (user) call<boolean>("backend", "hasSavedPassword", user).then((v) => alive && setSaved(v)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [user]);

  const fail = (message: string) => {
    setError(message);
    if (message && m.on && scope.current) {
      animate(scope.current, { x: [0, -10, 10, -6, 0] }, { duration: 0.26 });
    }
  };

  const submit = async () => {
    if (b.authBusy) return;
    const err = signUp
      ? await call<string>("backend", "signUp", newName, newPass, newPass2)
      : await call<string>("backend", "signIn", user, password, remember);
    fail(err);
  };

  const strength = passwordStrength(newPass);
  const strengthColor = strength <= 1 ? "var(--danger)" : strength === 2 ? "var(--warning)" : "var(--success)";
  const features = [
    { icon: "users", text: t("login.f1") },
    { icon: "shield-check", text: t("login.f2") },
    { icon: "lock", text: t("login.f3") },
  ];
  const enter = (delay: number) => ({
    initial: m.on ? { opacity: 0, y: 24 } : false,
    animate: { opacity: 1, y: 0 },
    transition: { ...m.ease(840), delay: m.rich ? delay : 0 },
  });

  return (
    <div className="login">
      <div className="login-brand">
        <motion.div className="login-brand-inner" {...enter(0)}>
          <OrbitLogo size={132} speed={0.8} />
          <div className="login-name">{b.appName}</div>
          <div className="login-tagline dim">{t("login.tagline")}</div>
          <div className="col" style={{ gap: 14, marginTop: 6 }}>
            {features.map((f, i) => (
              <motion.div key={f.icon} className="row" style={{ gap: 12 }} {...enter(0.15 + i * 0.08)}>
                <div className="login-feature-icon">
                  <Icon name={f.icon} size={16} color="var(--primary-soft)" />
                </div>
                <span className="dim">{f.text}</span>
              </motion.div>
            ))}
          </div>
        </motion.div>
      </div>

      <div className="login-side">
        <motion.div {...enter(0.1)}>
          <div ref={scope} className="card login-card">
            <div className="row" style={{ alignItems: "flex-start" }}>
              <div className="h1 grow">{signUp ? t("login.create_title") : t("login.title")}</div>
              <Segmented
                options={(b.languages ?? []).map((l: any) => ({ value: l.code, title: String(l.code).toUpperCase() }))}
                value={b.lang}
                onChange={(v) => call("backend", "setLanguage", v)}
              />
            </div>
            <div className="dim">{t("login.subtitle")}</div>

            {!signUp ? (
              <div className="col" style={{ gap: 14 }}>
                <Select
                  label={t("login.username")}
                  icon="user"
                  options={profiles}
                  value={user}
                  onChange={(v) => {
                    setUser(v);
                    setPassword("");
                  }}
                />
                <Field
                  label={t("login.password")}
                  icon="lock"
                  password
                  autoFocus
                  value={password}
                  placeholder={saved ? t("login.saved_password") : ""}
                  onChange={setPassword}
                  onEnter={submit}
                />
                <Toggle
                  label={t("login.remember")}
                  checked={remember}
                  disabled={!b.keyringAvailable}
                  onChange={setRemember}
                />
              </div>
            ) : (
              <div className="col" style={{ gap: 14 }}>
                <Notice tone="warning">{t("login.warning")}</Notice>
                <Field label={t("login.username")} icon="user" autoFocus value={newName} onChange={setNewName} />
                <Field label={t("login.password")} icon="lock" password value={newPass} onChange={setNewPass} />
                <div className="row" style={{ gap: 6 }}>
                  {[0, 1, 2, 3].map((i) => (
                    <span
                      key={i}
                      className="strength-bar"
                      style={{ background: i < strength ? strengthColor : "var(--surface3)" }}
                    />
                  ))}
                  <span className="micro mute strength-text">{t(`login.s${strength}`)}</span>
                </div>
                <Field
                  label={t("login.password2")}
                  icon="lock"
                  password
                  value={newPass2}
                  onChange={setNewPass2}
                  error={newPass2 && newPass2 !== newPass ? t("login.password_mismatch") : ""}
                  onEnter={submit}
                />
              </div>
            )}

            {error && <Notice tone="error">{error}</Notice>}

            <Button
              variant="primary"
              block
              icon={signUp ? "sparkles" : "arrow-right"}
              loading={Boolean(b.authBusy)}
              onClick={submit}
            >
              {signUp ? t("login.create") : t("login.signin")}
            </Button>
            {profiles.length > 0 && (
              <Button
                variant="ghost"
                block
                onClick={() => {
                  setSignUp(!signUp);
                  setError("");
                }}
              >
                {signUp ? t("login.have_profile") : t("login.create")}
              </Button>
            )}
          </div>
        </motion.div>
        <div className="login-foot small mute">
          {b.appName} {b.appVersion} · {t("login.local")}
        </div>
      </div>
    </div>
  );
}
