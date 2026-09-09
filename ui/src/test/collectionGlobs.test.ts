import { describe, it, expect } from "vitest";
import picomatch from "picomatch";

import vitestConfigSource from "../../vitest.config.ts?raw";
import tsconfigRaw from "../../tsconfig.json?raw";

/**
 * Guard for the Google-Drive sync-conflict copies ("Settings (1).test.tsx",
 * "api (1)/rpc.test.ts").
 *
 * Drive recreates them without warning, and neither .gitignore nor a one-off
 * `rm` stops vitest and tsc from collecting the next one: both glob the
 * working tree, not the git index. A stale copy compiled against a renamed
 * prop reds the entire local gate with a type error in a file nobody touched —
 * so the exclusion has to live in the collection configs themselves, and this
 * pins it there.
 *
 * The two configs are spelled DIFFERENTLY on purpose ("**\/*[(]1[)]*" for
 * vitest, "src/**\/* (1)*" for tsc) because they are consumed by two matchers
 * that disagree about parentheses. This file therefore runs each spelling
 * through the matcher that actually consumes it — an earlier version of this
 * test rolled its own translator which escaped the parens, i.e. it diverged
 * from picomatch at exactly the character that decides the outcome, and so
 * went green against a vitest pattern that excluded nothing.
 */

// --- the configs, as the tools themselves see them --------------------------

/**
 * Pull one `key: [ "…", "…" ]` array out of the vitest config SOURCE.
 *
 * The config is read as text, not imported: importing it evaluates
 * `@vitejs/plugin-react`, which loads esbuild, which refuses to start under
 * jsdom ("new TextEncoder().encode('') instanceof Uint8Array is incorrectly
 * false"). A regex is not enough either — the patterns now contain "[" and "]",
 * so `\[[^\]]*\]` stops inside the first entry. Hence a scanner that tracks
 * string literals. The entries are plain double-quoted strings with no escapes;
 * anything else throws rather than being silently mis-read.
 */
function configArray(key: "include" | "exclude"): string[] {
  const marker = `${key}: [`;
  const start = vitestConfigSource.indexOf(marker);
  if (start < 0) throw new Error(`vitest.config.ts has no \`${key}:\` array`);
  const entries: string[] = [];
  let current = "";
  let inString = false;
  let depth = 0;
  for (let i = vitestConfigSource.indexOf("[", start); i < vitestConfigSource.length; i++) {
    const ch = vitestConfigSource[i];
    if (inString) {
      if (ch === "\\") throw new Error(`vitest.config.ts: escape in a ${key} entry`);
      if (ch === '"') {
        entries.push(current);
        current = "";
        inString = false;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "[") depth++;
    else if (ch === "]" && --depth === 0) {
      if (!entries.length) throw new Error(`vitest.config.ts: empty \`${key}\``);
      return entries;
    }
  }
  throw new Error(`vitest.config.ts: unterminated \`${key}\` array`);
}

function vitestInclude(): string[] {
  return configArray("include");
}

function vitestExclude(): string[] {
  return configArray("exclude");
}

function tsconfigExclude(): string[] {
  // tsconfig.json is JSONC — strip the whole-line comments before parsing.
  // Read as text rather than imported as a module precisely so the file is
  // free to carry the comment explaining its spelling.
  const json = tsconfigRaw.replace(/^\s*\/\/.*$/gm, "");
  const parsed = JSON.parse(json) as { exclude?: string[] };
  const exclude = parsed.exclude ?? [];
  if (!exclude.length) throw new Error("tsconfig.json has no `exclude`");
  return exclude;
}

// --- matchers ---------------------------------------------------------------

/**
 * vitest hands `test.exclude` straight to tinyglobby as `ignore`, which matches
 * with picomatch and `dot: true`. This is that exact matcher: since picomatch
 * is now a direct devDependency it deduplicates with the copies tinyglobby and
 * vite resolve, so the module under test here is the module that ships.
 */
function isCollectedByVitest(file: string): boolean {
  const included = picomatch(vitestInclude(), { dot: true });
  const ignored = picomatch(vitestExclude(), { dot: true });
  return included(file) && !ignored(file);
}

/**
 * TypeScript's config-file glob expander, which understands only `**`, `*` and
 * `?` and treats every other character — parentheses and brackets alike — as a
 * literal. That is the whole reason tsconfig.json keeps the plain-paren
 * spelling; picomatch is the wrong model for it.
 */
function tsGlobToRegExp(glob: string): RegExp {
  const segments = glob.split("/");
  let body = "";
  segments.forEach((segment, i) => {
    const last = i === segments.length - 1;
    if (segment === "**") {
      // A non-final "**" spans zero or more directories and carries its own
      // separator; a trailing "**" is everything below.
      body += last ? ".+" : "(?:[^/]+/)*";
      return;
    }
    body += segment
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*/g, "[^/]*")
      .replace(/\?/g, "[^/]");
    if (!last) body += "/";
  });
  return new RegExp(`^${body}$`);
}

function isExcludedByTsc(file: string): boolean {
  return tsconfigExclude().some((glob) => tsGlobToRegExp(glob).test(file));
}

// --- fixtures ---------------------------------------------------------------

/** File-level sync conflicts — the shape Drive gives a duplicated file. */
const DUPLICATE_FILES = [
  "src/App (1).test.tsx",
  "src/components/Settings (1).test.tsx",
  "src/lib/review (1).test.ts",
];

/** Directory-level sync conflicts. Drive produced exactly these in this repo
 *  ("src/api (1)", "hooks (1)", "stores (1)"). `*` does not cross a "/", so the
 *  file-level pattern alone leaves every one of them collected. */
const DUPLICATE_DIRECTORIES = [
  "src/api (1)/rpc.test.ts",
  "src/components (1)/Settings.test.tsx",
  "src/stores (1)/nested/library.test.ts",
];

/** Real test files. The last one is the trap: under the old bare-paren
 *  spelling picomatch compiled the pattern to `[^/]*? (1)[^/]*?`, so a
 *  filename containing a space followed by "1" was silently dropped from
 *  collection while every actual duplicate stayed. */
const LEGITIMATE_TESTS = [
  "src/App.test.tsx",
  "src/components/Settings.test.tsx",
  "src/lib/review.test.ts",
  "src/lib/migration step 1.test.ts",
];

/** Non-test sources: not collected by vitest either way, but tsc compiles
 *  them, so the tsconfig half has to tell the two apart. */
const DUPLICATE_SOURCES = [
  "src/hooks/useConfigPersistence (1).tsx",
  "src/types (1)/generated.ts",
];
const LEGITIMATE_SOURCES = ["src/hooks/useConfigPersistence.tsx"];

// --- the guard --------------------------------------------------------------

describe("vitest collection (real picomatch matcher)", () => {
  it("skips Drive sync-conflict files at any depth", () => {
    for (const dup of DUPLICATE_FILES) {
      expect(isCollectedByVitest(dup), `vitest would collect ${dup}`).toBe(false);
    }
  });

  it("skips whole Drive sync-conflict directories", () => {
    for (const dup of DUPLICATE_DIRECTORIES) {
      expect(isCollectedByVitest(dup), `vitest would collect ${dup}`).toBe(false);
    }
  });

  it("still collects every real test, including names with a space before a digit", () => {
    for (const ok of LEGITIMATE_TESTS) {
      expect(isCollectedByVitest(ok), `vitest would skip the real ${ok}`).toBe(true);
    }
  });

  it("uses globs, not bare basenames, for every exclude entry", () => {
    // "dist" / "src-tauri" as bare names matched nothing at all — `exclude` is
    // matched against the relative PATH. An entry without a wildcard is
    // decoration, and reads as protection that isn't there.
    for (const entry of vitestExclude()) {
      expect(entry, `exclude entry ${entry} is not a glob`).toContain("*");
    }
  });

  it("spells the duplicate marker with bracket classes, never bare parens", () => {
    // Pins the reason. A bare "(1)" is a capture group to picomatch: it matches
    // no duplicate and swallows an honest "step 1" test. If someone "tidies"
    // the brackets away, this fails with the evidence attached.
    const bare = picomatch("**/* (1)*", { dot: true });
    expect(bare("src/components/Settings (1).test.tsx")).toBe(false);
    expect(bare("src/lib/migration step 1.test.ts")).toBe(true);

    const bracketed = vitestExclude().filter((p) => p.includes("[(]1[)]"));
    expect(bracketed.length, "no bracket-class duplicate pattern in exclude").toBe(2);
    for (const entry of vitestExclude()) {
      expect(entry, `exclude entry ${entry} uses bare parens`).not.toMatch(/(?<!\[)\(/);
    }
  });
});

describe("tsc program (TypeScript's own literal-paren expander)", () => {
  it("drops Drive sync-conflict files and directories", () => {
    for (const dup of [...DUPLICATE_FILES, ...DUPLICATE_DIRECTORIES, ...DUPLICATE_SOURCES]) {
      expect(isExcludedByTsc(dup), `tsc would compile ${dup}`).toBe(true);
    }
  });

  it("keeps every real source and test in the program", () => {
    for (const ok of [...LEGITIMATE_TESTS, ...LEGITIMATE_SOURCES]) {
      expect(isExcludedByTsc(ok), `tsc would skip the real ${ok}`).toBe(false);
    }
  });

  it("keeps the plain-paren spelling — brackets would be literal to tsc", () => {
    for (const entry of tsconfigExclude()) {
      expect(entry, `tsconfig exclude entry ${entry} uses a bracket class`).not.toContain("[");
    }
  });
});
