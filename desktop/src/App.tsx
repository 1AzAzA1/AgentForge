// Главное окно: живой фон, экран входа или оболочка приложения, уведомления.

import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useEffect } from "react";
import { host } from "./api/host";
import { tr, useBridgeEvent, useBridgeStatus, useCtl, useT } from "./api/hooks";
import { Login } from "./pages/Login";
import { Shell } from "./pages/Shell";
import { ConfirmHost } from "./ui/Sheet";
import { Icon } from "./ui/Icon";
import { Spinner } from "./ui/Spinner";
import { toast, ToastHost } from "./ui/Toasts";
import { useMotionPrefs } from "./ui/theme";
import { Aurora, OrbitLogo } from "./ui/Visuals";

export function App() {
  const { status, error } = useBridgeStatus();
  const backend = useCtl("backend");
  const t = useT();
  const m = useMotionPrefs();
  const ready = backend.appName !== undefined;
  const loggedIn = Boolean(backend.loggedIn);

  useEffect(() => {
    document.documentElement.dataset.motion = m.level;
  }, [m.level]);
  useEffect(() => {
    document.documentElement.lang = t.lang;
  }, [t.lang]);
  useEffect(() => {
    const name = backend.appName ?? "Agent Forge";
    document.title = loggedIn ? `${name} · ${backend.username}` : name;
  }, [backend.appName, backend.username, loggedIn]);
  useEffect(() => {
    host.onBackendExit((code, restarting) =>
      toast("error", tr("app.core_exited", { code }), tr(restarting ? "app.core_restarting" : "app.core_exited_text")),
    );
  }, []);

  useBridgeEvent("backend", "toast", toast);

  return (
    <MotionConfig reducedMotion={m.level === "off" ? "always" : "never"}>
      <Aurora intensity={loggedIn ? 0.75 : 1} />
      {!ready ? (
        <Splash failed={status === "lost"} error={error} />
      ) : (
        <AnimatePresence mode="wait">
          {loggedIn ? (
            <motion.div
              key="shell"
              className="screen"
              initial={{ opacity: 0, scale: 0.985 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              transition={m.ease(420)}
            >
              <Shell />
            </motion.div>
          ) : (
            <motion.div
              key="login"
              className="screen"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={m.ease(420)}
            >
              <Login />
            </motion.div>
          )}
        </AnimatePresence>
      )}
      {ready && status !== "open" && (
        <div className="lost-banner" title={error}>
          {error ? <Icon name="triangle-alert" size={14} /> : <Spinner size={14} color="var(--warning)" />}
          {error ? t("app.core_failed") : t("app.reconnecting")}
        </div>
      )}
      <ToastHost />
      <ConfirmHost />
    </MotionConfig>
  );
}

// До первого снимка состояния язык ещё неизвестен, поэтому подписи короткие.
function Splash({ failed, error }: { failed: boolean; error: string }) {
  return (
    <div className="screen splash">
      <OrbitLogo size={96} speed={0.8} />
      <div className="h2">Agent Forge</div>
      {failed && error ? (
        <pre className="splash-error selectable">{error}</pre>
      ) : (
        <div className="row mute">
          <Spinner size={14} />
          <span>Запуск ядра...</span>
        </div>
      )}
    </div>
  );
}
