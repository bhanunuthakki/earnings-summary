// Build script for @earnings-summary/design-system.
//
// Produces (all in dist/):
//   - index.js    ESM bundle of src/index.ts, react/react-dom left external
//   - index.css   bundle of src/index.css (which @imports the token/control CSS)
//   - index.d.ts  (+ nested .d.ts) via `tsc --emitDeclarationOnly`
//
// Usage: `node build.mjs` (one-shot) or `node build.mjs --watch` (rebuild on change).

import { build, context } from "esbuild";
import { execSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";

const watch = process.argv.includes("--watch");

if (existsSync("dist")) {
  rmSync("dist", { recursive: true, force: true });
}

const jsOptions = {
  entryPoints: ["src/index.ts"],
  outfile: "dist/index.js",
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2022",
  external: ["react", "react-dom", "react/jsx-runtime"],
  sourcemap: true,
  logLevel: "info",
};

const cssOptions = {
  entryPoints: ["src/index.css"],
  outfile: "dist/index.css",
  bundle: true,
  loader: { ".css": "css" },
  logLevel: "info",
};

function emitTypes() {
  execSync("npx tsc --emitDeclarationOnly --outDir dist", {
    stdio: "inherit",
  });
}

if (watch) {
  const jsCtx = await context(jsOptions);
  const cssCtx = await context(cssOptions);
  await jsCtx.watch();
  await cssCtx.watch();
  emitTypes();
  console.log("Watching for changes... (types are one-shot; re-run build for updated .d.ts)");
} else {
  await build(jsOptions);
  await build(cssOptions);
  emitTypes();
  console.log("Build complete: dist/index.js, dist/index.css, dist/index.d.ts");
}
