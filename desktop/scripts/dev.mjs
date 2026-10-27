// Режим разработки: dev-сервер Vite с горячей перезагрузкой и окно Electron,
// которое открывает страницу с него, а ядро поднимает как обычно.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const require = createRequire(import.meta.url);

const server = await createServer({ root, configFile: path.join(root, "vite.config.ts") });
await server.listen();
const url = server.resolvedUrls.local[0];
console.log(`Vite: ${url}`);

const electron = require("electron");
const child = spawn(electron, [root], {
  stdio: "inherit",
  env: { ...process.env, AGENTFORGE_DEV_URL: url },
});
child.on("exit", async (code) => {
  await server.close();
  process.exit(code ?? 0);
});
