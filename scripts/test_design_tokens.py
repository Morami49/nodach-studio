import json

from design_tokens import CSS, TOKENS, render


def test_css_matches_tokens():
    assert CSS.read_text(encoding="utf-8") == render(json.loads(TOKENS.read_text(encoding="utf-8"))), \
        "design/tokens.css was edited by hand or not regenerated: run python scripts/design_tokens.py"


def test_dark_theme_and_brand_colours_present():
    css = CSS.read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css
    assert "--nodach-green: #0E4A3D;" in css and "--nodach-cream: #F3E3CC;" in css


def test_token_names_unique():
    t = json.loads(TOKENS.read_text(encoding="utf-8"))
    names = [x["name"] for fam in ("color", "spacing", "radius") for x in t[fam]["tokens"]]
    assert len(names) == len(set(names))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
