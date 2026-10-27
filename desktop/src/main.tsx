import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { bridge } from "./api/bridge";
import { App } from "./App";
import "./styles/theme.css";
import "./styles/components.css";
import "./styles/pages.css";

void bridge.connect();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
