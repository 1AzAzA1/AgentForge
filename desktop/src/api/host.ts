// Возможности окна, которые даёт Electron. В браузере (режим разработки
// `npm run web`) вместо них простые заменители.

export interface BackendInfo {
  port: number;
  token: string;
}

interface HostApi {
  backend(): Promise<BackendInfo>;
  saveFile(defaultPath: string): Promise<string | null>;
  chooseFolder(): Promise<string | null>;
  copy(text: string): Promise<void>;
  openExternal(url: string): Promise<void>;
  onBackendExit(callback: (code: number, restarting: boolean) => void): void;
}

declare global {
  interface Window {
    agentForge?: HostApi;
  }
}

const electron = window.agentForge;

export const inElectron = Boolean(electron);

// В браузере адрес ядра берётся из строки запроса: ?port=8765&token=dev
function browserBackend(): Promise<BackendInfo> {
  const params = new URLSearchParams(window.location.search);
  return Promise.resolve({
    port: Number(params.get("port") || 8765),
    token: params.get("token") || "dev",
  });
}

export const host = {
  backend: () => (electron ? electron.backend() : browserBackend()),
  saveFile: (defaultPath: string) =>
    electron ? electron.saveFile(defaultPath) : Promise.resolve(window.prompt("Путь к файлу", defaultPath)),
  chooseFolder: () =>
    electron ? electron.chooseFolder() : Promise.resolve(window.prompt("Путь к каталогу", "")),
  copy: async (text: string) => {
    if (electron) return electron.copy(text);
    await navigator.clipboard.writeText(text);
  },
  openExternal: (url: string) => {
    if (electron) void electron.openExternal(url);
    else window.open(url, "_blank", "noopener");
  },
  onBackendExit: (callback: (code: number, restarting: boolean) => void) => electron?.onBackendExit(callback),
};
