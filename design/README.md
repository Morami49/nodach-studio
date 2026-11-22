# design/

Nodach's design source of truth (decision FIG-3c, 22 Nov 2026). Everything else copies from here: the Claude design-system artifact, the Figma file, and Studio's own developer mode.

- `tokens.json`: every colour (light and dark), spacing, radius and type style. **Edit this file and nothing else.**
- `tokens.css`: generated from `tokens.json` as CSS variables (`var(--nodach-green)`). Theme switching uses `data-theme="dark"` on `<html>`.
- `BRAND.md`: the brand rules (mark, lockup, colour, type, naming, navigation). The master copy of them.
- `logos/`: the N mark at fixed proportions of 240 : 100, in green, cream and black, plus the favicon.

Regenerate `tokens.css` with `python scripts/design_tokens.py`; CI fails if it is out of date.

The rules for using these (the mark, the type, naming) are in the brand book: https://claude.ai/artifact/1z4jNEfHKTEN2WUM7Caz8Y
Figma mirror: https://www.figma.com/design/PVzf4eny8svQn1mGs7pg40
