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
    fams = [k for k, v in t.items() if isinstance(v, dict) and "tokens" in v]
    names = [x["name"] for fam in fams for x in t[fam]["tokens"]]
    assert len(names) == len(set(names))


def test_motion_tokens_present():
    css = CSS.read_text(encoding="utf-8")
    for n in ("--duration-nav-open-delay: 150ms;", "--duration-hover: 180ms;", "--easing-out: cubic-bezier(0, 0, 0.58, 1);"):
        assert n in css, n


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
