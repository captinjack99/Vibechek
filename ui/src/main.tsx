import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// Bundled fonts: identical rendering on Windows/macOS/Linux instead of the
// Segoe-UI / SF-Pro / DejaVu lottery (CSP font-src 'self' already allows them;
// "Inter"/"JetBrains Mono" in the Tailwind stacks were phantoms before this —
// never shipped, resolving differently per machine).
import "@fontsource-variable/inter";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./styles/globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
