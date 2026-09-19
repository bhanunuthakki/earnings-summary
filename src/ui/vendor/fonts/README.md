# Self-hosted webfonts (vendored)

These are the Google Fonts the Work OS shell (the prototype
`mockups/harvey_sidebar_flow.html`, rendered at `/` by
`src/pipeline/work_os_shell.py`) previously hot-linked. They are vendored here
so the shell's first paint never depends on an external DNS/TLS origin, and so
offline rendering is exact rather than a fallback approximation.

## Provenance

Upstream request (fetched 2026-09-18 with a Chrome woff2-capable user agent):

```
https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap
```

The CSS2 response contains 39 `@font-face` rules (Inter v20 x 7 subsets x
weights 400/500/600; JetBrains Mono v24 x 6 subsets x weights 400/500/600).
Google serves each family as a **variable font**: one binary per subset is
shared across the three weights. 13 distinct binaries are vendored below; the
`@font-face` rules in the prototype reference the same file per subset with
per-weight `font-weight` descriptors, exactly as upstream serves them, so
family/weight/metric rendering is identical to the hot-linked version.

## File manifest

SHA-256 is over the vendored bytes; `size` in bytes.

| vendored file | upstream URL | SHA-256 | size |
| --- | --- | --- | --- |
| `inter-cyrillic-ext.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2JL7W0Q5n-wU.woff2 | `fccca918fea40089dacadc7045861314d1a6bc91f1f323cc1eeb22ebcdb321b5` | 25,844 |
| `inter-cyrillic.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa0ZL7W0Q5n-wU.woff2 | `aebf2ab4a4ce6810d73c1ac7be7cafb4e5ec4cee2d6db5fb3e09691747ec4bd6` | 18,744 |
| `inter-greek-ext.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2ZL7W0Q5n-wU.woff2 | `a2e2c783ca6f9c20486e81e72a279203e86730bbf8f01ff6a5ee9dbd09e1c271` | 11,272 |
| `inter-greek.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1pL7W0Q5n-wU.woff2 | `46dd4cdca58c26ae87cc6927657bf83b2e8abfc39ffd0ab176e301a8d28d22bf` | 19,044 |
| `inter-vietnamese.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa2pL7W0Q5n-wU.woff2 | `8db00ff46c67b22cda8bed865acf7077651cac8d2841d5b40980556b48961931` | 10,280 |
| `inter-latin-ext.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa25L7W0Q5n-wU.woff2 | `a28eb6d3ccb534ae0c94ca999371df024aab60b08c3c8a5720ee9e32fa0faaa2` | 85,272 |
| `inter-latin.woff2` | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7W0Q5nw.woff2 | `c940764593d0fe5d596be327ca7558855e018039fb78509aa21921fd3644c3e4` | 48,432 |
| `jetbrains-mono-cyrillic-ext.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx3cwgknk-6nFg.woff2 | `9343de2ca5d9549f792e7962375af8efb0f320c7643bfd36c884b5a30e5c396f` | 1,664 |
| `jetbrains-mono-cyrillic.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxTcwgknk-6nFg.woff2 | `4995a9a43ac659ec32fcd8b463755cd6a07b31a6e6b3894a6a153b661cf490e2` | 8,892 |
| `jetbrains-mono-greek.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxPcwgknk-6nFg.woff2 | `49c3da6c9a2b279b0f1f860f5cfb1f5dc38d88a5c7be9c9b1837bbc4e3db6111` | 6,800 |
| `jetbrains-mono-vietnamese.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx_cwgknk-6nFg.woff2 | `d44eb1936043a56038eb02dd70b243f379bef65783f94ec12f277550720411f1` | 5,872 |
| `jetbrains-mono-latin-ext.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPx7cwgknk-6nFg.woff2 | `9c38cb2d0d2d93c1ee6e21fa78db76f13ea7e15e15cc64214c7ca89b6aaa35c4` | 11,596 |
| `jetbrains-mono-latin.woff2` | https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2 | `2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4` | 31,340 |

Total: 13 binaries, 285,052 bytes.

## Licenses

Both families are SIL Open Font License 1.1; the license texts that accompany
redistribution are vendored beside the binaries:

- `OFL-Inter.txt` (https://github.com/google/fonts/blob/main/ofl/inter/OFL.txt)
- `OFL-JetBrainsMono.txt` (https://github.com/google/fonts/blob/main/ofl/jetbrainsmono/OFL.txt)

## How they are referenced and served

- `mockups/harvey_sidebar_flow.html` declares the 39 `@font-face` rules in a
  `<style id="work-os-fonts">` block whose `src` URLs are the relative path
  `../src/ui/vendor/fonts/<file>`. Relative form keeps the raw prototype
  renderable straight from disk (`file://`) and, served at `/`, resolves to
  the loopback route below.
- `execution/comments_server_content_routes.py` serves
  `GET /src/ui/vendor/fonts/<filename>` with
  `Cache-Control: public, max-age=31536000, immutable` — the binaries are
  content-stable, and a re-vendor is the only reason bytes change.

## Re-vendoring procedure

1. Fetch the CSS2 URL above with a modern Chrome user agent (woff2 response).
2. Download every referenced binary into this directory using the
   `<family>-<subset>.woff2` naming, record upstream URL + SHA-256 here.
3. Regenerate the `@font-face` block in the prototype from the same response,
   keeping family/weight/unicode-range descriptors identical to upstream and
   pointing `src` at the local relative path.
4. Re-run `python scripts/check_design_sync.py` and the Work OS shell tests;
   capture before/after shell screenshots as the rendering-evidence receipt.
