/**
 * ESLint config for the Vibechek UI.
 *
 * Legacy (eslintrc) format on purpose: `npm run lint` is
 * `eslint . --ext ts,tsx`, which is the legacy invocation. Staying on eslintrc
 * (rather than migrating to flat config) keeps the diff small and works
 * natively on ESLint 8.57 without ESLINT_USE_FLAT_CONFIG. @typescript-eslint 8
 * still ships its eslintrc-style shared configs, so the bump rides along fine.
 */
module.exports = {
  root: true,
  env: {
    browser: true,
    es2022: true,
    node: true,
  },
  parser: "@typescript-eslint/parser",
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: "module",
    ecmaFeatures: { jsx: true },
  },
  plugins: ["@typescript-eslint", "react-hooks", "react-refresh"],
  extends: [
    "eslint:recommended",
    "plugin:@typescript-eslint/recommended",
    "plugin:react-hooks/recommended",
  ],
  ignorePatterns: [
    "dist/",
    "src-tauri/",
    "coverage/",
    "node_modules/",
    // Wire-contract codegen (dataclass -> TS). Never hand-edited; linting it is noise.
    "src/types/generated.ts",
    // This config and other CJS build tooling shouldn't be linted as ESM/TS.
    ".eslintrc.cjs",
    "postcss.config.js",
    "tailwind.config.js",
  ],
  rules: {
    // Vite HMR guard: warn when a module exports non-components alongside a
    // component (breaks Fast Refresh). Constants are a common, safe exception.
    "react-refresh/only-export-components": [
      "warn",
      { allowConstantExport: true },
    ],
    // Keep console out of shipped UI code; the one intentional use is
    // explicitly disabled inline (AnalysisProgress.tsx). Enabling the rule
    // keeps that disable directive meaningful rather than inert.
    "no-console": "warn",
  },
};
