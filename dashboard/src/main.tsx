import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { configureAuth } from "./auth";
import { loadConfig } from "./strategist";
import "./styles.css";

const config = await loadConfig();
configureAuth({ config });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App config={config} />
  </StrictMode>,
);
