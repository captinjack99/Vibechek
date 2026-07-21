import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Tauri expects a fixed port and ignores VITE_*-prefixed env vars
export default defineConfig({
  // @tailwindcss/vite replaces the v3 PostCSS pipeline (tailwindcss +
  // autoprefixer). v4 handles vendor-prefixing internally, so postcss.config.js
  // is gone.
  plugins: [tailwindcss(), react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      // Tell vite to ignore watching the Rust source tree
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    target: ["es2022", "chrome105", "safari15"],
    minify: !process.env.TAURI_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_DEBUG,
  },
});
