"""FIG-3c: design/tokens.json is Nodach's design source of truth; design/tokens.css is generated from it.

Usage: python scripts/design_tokens.py          (rewrites design/tokens.css)
       python scripts/design_tokens.py --check  (exit 1 if tokens.css is out of date)
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOKENS = ROOT / "design" / "tokens.json"
CSS = ROOT / "design" / "tokens.css"
RESERVED = {"name", "version", "color", "type", "spacing", "radius", "meta"}
HEADER = "/* Nodach — generated from design/tokens.json. Edit tokens.json, then regenerate; never edit this file by hand. */"


def render(t: dict) -> str:
    themes = [x["id"] for x in t["color"]["themes"]]

    def block(sel: str, theme: str) -> str:
        lines = [f"{sel} {{"]
        for tok in t["color"]["tokens"]:
            v = tok["value"]
            v = v if isinstance(v, str) else v.get(theme, v[themes[0]])
            lines.append(f"  --{tok['name']}: {v};")
        if theme == themes[0]:
            for fam in ("spacing", "radius") + tuple(k for k in t if k not in RESERVED):
                for tok in t[fam]["tokens"]:
                    lines.append(f"  --{tok['name']}: {tok['value']};")
            for k, v in t["type"]["families"].items():
                lines.append(f"  --font-{k}: {v};")
        lines.append("}")
        return "\n".join(lines)

    out = [HEADER, block(f':root, [data-theme="{themes[0]}"]', themes[0])]
    out += [block(f'[data-theme="{th}"]', th) for th in themes[1:]]
    for g in t["type"]["groups"]:
        for s in g["styles"]:
            css = [f"font-family: var(--font-{s.get('family', g['family'])})", f"font-size: {s['fontSize']}",
                   f"line-height: {s['lineHeight']}", f"font-weight: {s['fontWeight']}"]
            if "letterSpacing" in s:
                css.append(f"letter-spacing: {s['letterSpacing']}")
            out.append(f".type-{s['name']} {{ " + "; ".join(css) + "; }")
    return "\n\n".join(out) + "\n"


if __name__ == "__main__":
    want = render(json.loads(TOKENS.read_text(encoding="utf-8")))
    if "--check" in sys.argv:
        if CSS.read_text(encoding="utf-8") != want:
            print("design/tokens.css is out of date: run python scripts/design_tokens.py", file=sys.stderr)
            sys.exit(1)
        print("design/tokens.css is up to date")
    else:
        CSS.write_text(want, encoding="utf-8")
        print("wrote", CSS.relative_to(ROOT))
