"""AS2 3e: sort a pull request's changed files into low or high risk. Low-risk PRs merge themselves once CI and
staging pass; anything else waits for Nowed. When in doubt a file is high risk.

Low: Studio pages (not sign-in), tests, fixtures, docs, the public front page, ActionMap.
High: everything else, including workflows, hub and engine code, wrangler.toml, the watch baseline, legal pages,
LICENSE, TRADEMARKS and everything under design/ (the brand source of truth). Usage: python scripts/risk_class.py < changed-files.txt  (prints low|high, reasons on stderr)
"""
import fnmatch
import sys

LOW = [
    "studio/*.html", "studio/tests/*", "studio/tests/**", "studio/*.js",
    "fixtures/*", "fixtures/**",
    "scripts/test_*.py",
    "actionmap/*", "actionmap/**",
    "site/index.html",
    "*.md", "**/*.md",
]
HIGH_EVEN_IF_LOW = [
    "studio/login.html",          # sign-in
    "LICENSE", "TRADEMARKS.md", "SECURITY.md",
    "site/privacy.html", "site/terms.html",
    "design/*", "design/**",      # brand source of truth: AI review, then Nowed approves (TOK-3)
]


def classify(paths: list[str]) -> tuple[str, list[str]]:
    high = []
    for p in (x.strip() for x in paths):
        if not p:
            continue
        if any(fnmatch.fnmatch(p, g) for g in HIGH_EVEN_IF_LOW) or not any(fnmatch.fnmatch(p, g) for g in LOW):
            high.append(p)
    return ("high" if high or not any(x.strip() for x in paths) else "low"), high


if __name__ == "__main__":
    verdict, why = classify(sys.stdin.read().splitlines())
    for p in why:
        print("high risk:", p, file=sys.stderr)
    print(verdict)
