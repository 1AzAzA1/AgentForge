// Мост страницы к главному процессу: только узкий набор действий, без Node.js.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("agentForge", {
  backend: () => ipcRenderer.invoke("backend:info"),
  saveFile: (defaultPath) => ipcRenderer.invoke("dialog:save", defaultPath),
  chooseFolder: () => ipcRenderer.invoke("dialog:folder"),
  copy: (text) => ipcRenderer.invoke("clipboard:write", text),
  openExternal: (url) => ipcRenderer.invoke("shell:external", url),
  onBackendExit: (callback) => {
    ipcRenderer.on("backend:exit", (_event, code, restarting) => callback(code, restarting));
  },
});
