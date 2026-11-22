from risk_class import classify


def test_pages_tests_and_docs_are_low():
    assert classify(["studio/vault.html", "studio/tests/vault_page.py", "actionmap/HANDOFF.md", "README.md"])[0] == "low"


def test_any_risky_file_makes_it_high():
    for p in [".github/workflows/watch.yml", "hub/src/lib.rs", "engine/crates/hub-core/src/session.rs",
              "hub/wrangler.toml", "ops/reliability-baseline.json", "studio/login.html", "site/terms.html", "LICENSE"]:
        assert classify(["studio/vault.html", p]) == ("high", [p]), p


def test_design_folder_is_high_even_for_docs():
    for p in ["design/BRAND.md", "design/README.md", "design/tokens.json"]:
        assert classify(["README.md", p]) == ("high", [p]), p


def test_empty_is_high():
    assert classify([])[0] == "high"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
