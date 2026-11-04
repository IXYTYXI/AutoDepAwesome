# Lucide icons

Source: https://lucide.dev/ · npm `lucide-static@1.60.0`
Package: https://registry.npmjs.org/lucide-static/-/lucide-static-1.60.0.tgz
Integrity: sha512-Y8/IYjBYST40TuqzknGt1XJOcZp83UH5fLndgXlQCQ4lM97eZO3EQo6cIyDcBrprredIGDVxge8yslRyM4wD0Q==
License: see LUCIDE-LICENSE.txt (upstream notices retained).

Only the 15 used icons are bundled in lucide.svg. SVG paths are unchanged.
Use a local SVG `<use href="assets/icons/lucide.svg#search">` with the shared `ui-icon` class.
All icons are decorative (`aria-hidden="true"`); icon-only buttons must retain an accessible label.
Default size: 16 px; stroke: 1.75; color: currentColor. Context sizes are defined in portal.css.
No CDN, runtime initialization, or observer is needed; dynamically rendered content uses the same markup.

## Vue migration

Use the official Lucide Vue package and import matching icons by name. Preserve size,
stroke width, currentColor, accessibility attributes and surrounding button hit areas.
Keep the company logo as the existing brand image.

## Included icons

- search
- plus
- x
- arrow-up-right
- arrow-left
- arrow-right
- arrow-down
- user-round
- blocks
- receipt-text
- gift
- notebook-pen
- coins
- file-clock
- image
