#!/usr/bin/env node
// Token-drift / off-system lint for the React design-system package.
//
// The Python side has `tests/test_ui_controls.py` §7.1 enforcing: zero raw
// hex, only on-scale font sizes, one radius, no `transition: all`, and
// heavy font-weights confined to the `.k-*` kit. This is the JS-side
// analog (docs/design_system_react_port_plan.md §5 "Also carry over the
// spirit of the Python guard").
//
// Scans `design-system/src/**/*.{ts,tsx,css}`, EXCLUDING the two generated
// token files (`src/tokens/tokens.css`, `src/tokens/tokens.generated.ts`) —
// those are the source of raw hex by construction and are covered by the
// Python-side drift test (`tests/test_design_tokens_export.py`) instead.
//
// Denied dimensions (kept reasonably permissive per Phase 1 — there is
// almost no component code yet; this establishes the scanner):
//   1. Raw hex color literals (`#1d4ed8`, `#fff`, ...).
//   2. Off-scale `font-size` px values — compared against the TYPE_SCALE
//      px values read live from the generated `tokens.generated.ts`, so
//      this list can never itself drift from `src/ui/tokens.py`.
//   3. `transition: all` (or `all <duration>`).
//   4. `font-weight: 700` or higher, or `font-weight: bold`, used outside a
//      `.k-*` kit class (CSS) — best-effort, rule-block scoped.
//
// Exit 0 if clean, exit 1 (with a listing) otherwise. `npm run check` wires
// this in alongside `tsc --noEmit`.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const PKG_ROOT = join(__dirname, "..");
const SRC_ROOT = join(PKG_ROOT, "src");

const EXCLUDED_ABS = new Set(
  [join(SRC_ROOT, "tokens", "tokens.css"), join(SRC_ROOT, "tokens", "tokens.generated.ts")],
);

const SCAN_EXTENSIONS = new Set([".ts", ".tsx", ".css"]);

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      walk(full, out);
    } else if (SCAN_EXTENSIONS.has(entry.slice(entry.lastIndexOf(".")))) {
      out.push(full);
    }
  }
  return out;
}

function loadTypeScalePx() {
  // Read the *generated* TS file to get the live on-scale px values — this
  // is the one read of an excluded file, done deliberately so the allowed
  // set can never itself go stale relative to src/ui/tokens.py.
  const tsPath = join(SRC_ROOT, "tokens", "tokens.generated.ts");
  const text = readFileSync(tsPath, "utf8");
  const block = text.split("export const TYPE_SCALE = {")[1]?.split("} as const;")[0] ?? "";
  const px = new Set();
  for (const match of block.matchAll(/:\s*"([\d.]+px)"/g)) {
    px.add(match[1]);
  }
  return px;
}

function loadRadiusPx() {
  const tsPath = join(SRC_ROOT, "tokens", "tokens.generated.ts");
  const text = readFileSync(tsPath, "utf8");
  const block = text.split("export const CHROME_TOKENS = {")[1]?.split("} as const;")[0] ?? "";
  const px = new Set();
  for (const match of block.matchAll(/"radius[^"]*":\s*"([\d.]+px)"/g)) {
    px.add(match[1]);
  }
  return px;
}

const HEX_RE = /#[0-9a-fA-F]{3,8}\b/g;
const FONT_SIZE_PX_RE = /font-size\s*:\s*([\d.]+)px/gi;
const BORDER_RADIUS_PX_RE = /border-radius\s*:\s*([\d.]+)px/gi;
const TRANSITION_ALL_RE = /transition\s*:\s*all\b/gi;
const FONT_WEIGHT_HEAVY_RE = /font-weight\s*:\s*(bold|[7-9]\d\d|1000)\b/gi;

function lineNumberAt(text, index) {
  return text.slice(0, index).split("\n").length;
}

function checkFile(path, typeScalePx, radiusPx, violations) {
  const rel = relative(PKG_ROOT, path).replace(/\\/g, "/");
  const text = readFileSync(path, "utf8");
  const isCss = path.endsWith(".css");

  for (const match of text.matchAll(HEX_RE)) {
    violations.push({
      file: rel,
      line: lineNumberAt(text, match.index),
      rule: "raw-hex-color",
      detail: `raw hex literal ${match[0]} — use a var(--token) from the generated palette instead`,
    });
  }

  for (const match of text.matchAll(FONT_SIZE_PX_RE)) {
    const px = `${match[1]}px`;
    if (!typeScalePx.has(px)) {
      violations.push({
        file: rel,
        line: lineNumberAt(text, match.index),
        rule: "off-scale-font-size",
        detail: `font-size: ${px} is not one of the TYPE_SCALE steps (${[...typeScalePx].join(", ")})`,
      });
    }
  }

  for (const match of text.matchAll(BORDER_RADIUS_PX_RE)) {
    const px = `${match[1]}px`;
    if (!radiusPx.has(px)) {
      violations.push({
        file: rel,
        line: lineNumberAt(text, match.index),
        rule: "off-scale-border-radius",
        detail: `border-radius: ${px} is not var(--radius)/var(--radius-full) (${[...radiusPx].join(", ")})`,
      });
    }
  }

  for (const match of text.matchAll(TRANSITION_ALL_RE)) {
    violations.push({
      file: rel,
      line: lineNumberAt(text, match.index),
      rule: "transition-all",
      detail: "`transition: all` — name the explicit properties instead (design_language.md)",
    });
  }

  if (isCss) {
    // Rule-block-scoped: only flag a heavy font-weight if none of the
    // selectors feeding that block mention a `.k-*` kit class. Best-effort
    // (no real CSS parser) — deliberately permissive per Phase 1 scope.
    const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
    for (const rule of text.matchAll(ruleRe)) {
      const selector = rule[1];
      const body = rule[2];
      if (/\.k-[a-zA-Z0-9_-]*/.test(selector)) continue;
      for (const match of body.matchAll(FONT_WEIGHT_HEAVY_RE)) {
        violations.push({
          file: rel,
          line: lineNumberAt(text, rule.index + rule[1].length + 1 + match.index),
          rule: "heavy-font-weight-outside-kit",
          detail: `font-weight: ${match[1]} outside a .k-* kit selector (selector: ${selector.trim().slice(0, 80)})`,
        });
      }
    }
  } else {
    // ts/tsx: flag inline style font-weight >= 700 unconditionally (no
    // selector context to check against a .k-* class in JS-land).
    for (const match of text.matchAll(FONT_WEIGHT_HEAVY_RE)) {
      violations.push({
        file: rel,
        line: lineNumberAt(text, match.index),
        rule: "heavy-font-weight-outside-kit",
        detail: `font-weight: ${match[1]} in inline/JS style — heavy weights belong to the .k-* kit CSS, not component code`,
      });
    }
  }
}

function main() {
  const files = walk(SRC_ROOT).filter((f) => !EXCLUDED_ABS.has(f));
  const typeScalePx = loadTypeScalePx();
  const radiusPx = loadRadiusPx();

  const violations = [];
  for (const file of files) {
    checkFile(file, typeScalePx, radiusPx, violations);
  }

  if (violations.length === 0) {
    console.log(`check-tokens: clean (${files.length} files scanned, 2 generated files excluded)`);
    return 0;
  }

  console.error(`check-tokens: ${violations.length} violation(s) found:\n`);
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line} [${v.rule}] ${v.detail}`);
  }
  return 1;
}

process.exit(main());
