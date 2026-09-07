/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Vitest config — kept separate from vite.config.ts so the Tauri dev server
// stays untouched. Matches the `@/*` alias from tsconfig.json.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // Tests live alongside source. Exclude built artifacts and node deps.
    // These MUST be globs: `exclude` is matched with picomatch against the
    // relative path, so the bare basenames that used to sit here ("dist",
    // "src-tauri") matched nothing at all.
    //
    // The `[(]1[)]` pair drops Google-Drive sync-conflict copies ("Settings
    // (1).test.tsx", and whole directories: "api (1)/rpc.test.ts"). Drive
    // recreates them without warning, and a stale copy written against a
    // renamed prop reds the whole local gate with a type error in a file
    // nobody edited. .gitignore alone does not help — vitest globs the working
    // tree, not the index.
    //
    // The brackets are not decoration. vitest hands `exclude` straight to
    // tinyglobby, which matches with picomatch, and picomatch reads a bare
    // "(1)" as a REGEX CAPTURE GROUP: "**/* (1)*" compiles to `[^/]*? (1)[^/]*?`,
    // which matches no Drive copy at all (the char before the 1 is "(") and
    // DOES silently swallow any honest test whose name contains a space then a
    // "1" ("migration step 1.test.ts"). A bracket class is literal to
    // picomatch, so it means what it looks like. tsconfig.json's `exclude` is
    // spelled with plain parens instead — see the comment there.
    // src/test/collectionGlobs.test.ts pins both spellings against their real
    // matchers.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: [
      "**/node_modules/**",
      "**/dist/**",
      "**/src-tauri/**",
      "**/*[(]1[)]*",
      "**/*[(]1[)]/**",
    ],
  },
});
