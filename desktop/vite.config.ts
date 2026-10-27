import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Политика безопасности для собранного окна: скрипты только свои, сеть только
// к ядру на этой машине. В режиме разработки не ставится: dev-сервер Vite
// вставляет в страницу собственный встроенный скрипт.
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self' data:",
  "img-src 'self' data:",
  "connect-src 'self' ws://127.0.0.1:*",
].join("; ");

function contentSecurityPolicy(): Plugin {
  return {
    name: "agent-forge-csp",
    apply: "build",
    transformIndexHtml: () => [
      { tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: CSP }, injectTo: "head-prepend" },
    ],
  };
}

// base: "./" - собранная страница открывается в Electron как файл, а не с сервера.
export default defineConfig({
  base: "./",
  plugins: [react(), contentSecurityPolicy()],
  server: { port: 5173, strictPort: true, host: "127.0.0.1" },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 1500 },
});
