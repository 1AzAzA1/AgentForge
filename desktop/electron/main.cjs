// Главный процесс Electron: поднимает Python-ядро, открывает окно и выполняет
// то, что странице самой недоступно (системные диалоги, буфер обмена, ссылки).

const { app, BrowserWindow, clipboard, dialog, ipcMain, nativeTheme, shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const DESKTOP = path.resolve(__dirname, "..");
const ROOT = path.resolve(DESKTOP, "..");
const DEV_URL = process.env.AGENTFORGE_DEV_URL || "";
const BG = "#09080F";

// Если ядро упало, его перезапускают; но не чаще трёх раз в минуту, иначе
// ошибка запуска крутилась бы по кругу.
const MAX_RESTARTS = 3;
const RESTART_WINDOW_MS = 60_000;

let backend = null;
let backendInfo = null;
let backendError = "";
let quitting = false;
let restarts = [];
let win = null;

function pythonPath() {
  if (process.env.AGENTFORGE_PYTHON) return process.env.AGENTFORGE_PYTHON;
  const venv = process.platform === "win32"
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
  return fs.existsSync(venv) ? venv : (process.platform === "win32" ? "python" : "python3");
}

// Ядро печатает строку AGENTFORGE_READY {"port": N}, когда сервер готов.
function startBackend() {
  const token = crypto.randomBytes(24).toString("base64url");
  return new Promise((resolve, reject) => {
    const child = spawn(pythonPath(), [path.join(ROOT, "main.py"), "--serve", "--exit-with-parent"], {
      cwd: ROOT,
      env: { ...process.env, AGENTFORGE_TOKEN: token, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    backend = child;
    let buffer = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (line.startsWith("AGENTFORGE_READY ")) {
          try {
            const { port } = JSON.parse(line.slice("AGENTFORGE_READY ".length));
            resolve({ port, token });
          } catch (err) {
            reject(err);
          }
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk).slice(-4000);
    });
    child.on("error", (err) => {
      // Python не нашёлся или не запустился вовсе: перезапуск не поможет.
      if (backend === child) {
        backend = null;
        backendError = `Не удалось запустить ${pythonPath()}: ${err.message}`;
      }
      reject(err);
    });
    child.on("exit", (code) => {
      reject(new Error(`Ядро завершилось с кодом ${code}.\n${stderr}`));
      // Остановили сами (выход из приложения) - ничего не делаем.
      if (backend !== child || quitting) return;
      backend = null;
      backendInfo = null;
      const now = Date.now();
      restarts = restarts.filter((t) => now - t < RESTART_WINDOW_MS);
      const again = restarts.length < MAX_RESTARTS;
      if (win && !win.isDestroyed()) win.webContents.send("backend:exit", code, again);
      if (again) {
        restarts.push(now);
        setTimeout(launchBackend, 500);
      } else {
        backendError = `Ядро завершилось с кодом ${code} и не запускается снова.\n${stderr}`;
      }
    });
  });
}

// Окно спрашивает адрес ядра через backend:info; пока ядро поднимается,
// ответ ждёт, поэтому после перезапуска окно само подключится к новому порту.
function launchBackend() {
  backendInfo = null;
  backendError = "";
  startBackend()
    .then((info) => { backendInfo = info; })
    .catch(() => {});
}

function stopBackend() {
  quitting = true;
  if (!backend) return;
  const child = backend;
  backend = null;
  // Закрытый stdin - сигнал ядру остановить агентов и выйти самому.
  child.stdin.end();
  setTimeout(() => {
    if (child.exitCode === null) child.kill();
  }, 3000);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1180,
    minHeight: 720,
    show: false,
    backgroundColor: BG,
    title: "Agent Forge",
    icon: path.join(DESKTOP, "assets", "icon.png"),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.removeMenu();
  win.once("ready-to-show", () => win.show());

  // Ссылки (документация провайдеров) открываются в системном браузере.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    const own = DEV_URL ? url.startsWith(DEV_URL) : url.startsWith("file://");
    if (!own) event.preventDefault();
  });

  if (DEV_URL) win.loadURL(DEV_URL);
  else win.loadFile(path.join(DESKTOP, "dist", "index.html"));
}

ipcMain.handle("backend:info", async () => {
  if (backendInfo) return backendInfo;
  if (backendError) throw new Error(backendError);
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (backendInfo) {
        clearInterval(timer);
        resolve(backendInfo);
      } else if (backendError) {
        clearInterval(timer);
        reject(new Error(backendError));
      }
    }, 50);
  });
});

ipcMain.handle("dialog:save", async (_event, defaultPath) => {
  const result = await dialog.showSaveDialog(win, { defaultPath: defaultPath || undefined });
  return result.canceled ? null : result.filePath;
});

ipcMain.handle("dialog:folder", async () => {
  const result = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("clipboard:write", (_event, text) => {
  clipboard.writeText(String(text || ""));
});

ipcMain.handle("shell:external", (_event, url) => {
  if (/^https?:\/\//.test(String(url))) shell.openExternal(String(url));
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    nativeTheme.themeSource = "dark";
    launchBackend();
    createWindow();
  });

  app.on("window-all-closed", () => {
    stopBackend();
    app.quit();
  });
  app.on("before-quit", stopBackend);
}
