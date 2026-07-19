# @earnings-summary/design-system

A self-contained npm package used as a **prototyping sandbox** for `claude.ai/design` via the
`/design-sync` skill. It is not a replacement for the Flask surfaces earnings-summary actually
ships. The Python design system under `src/ui/` (one level up, in the main repo) is canonical —
tokens and components are defined there first; changes flow forward into this package (generated
tokens, hand-ported components), never the other way. Full plan:
`../docs/design_system_react_port_plan.md`.

## Status

Phase 0 (scaffold) only. `src/index.ts` exports a single placeholder component to prove the build
pipeline (esbuild bundle + `tsc --emitDeclarationOnly` + CSS bundle) works end-to-end. No real kit
components yet.

## Build

```sh
npm install
npm run build   # -> dist/index.js, dist/index.d.ts, dist/index.css
npm run check   # tsc --noEmit (type-check only)
```
