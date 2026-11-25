# Claude Code in nodach-studio

This repository is Nodach Studio's main codebase (131a). The plan, decisions and work queue live on the **Nodach Studio
hub** (the `Nodach Studio` MCP connector), not in this file. Rule G-dev-lanes: claude.ai chats think and decide,
Claude Code builds, and the hub carries everything between them.

## When Nowed says "go" (or "resume")
1. Call the hub's `resume` tool once. Follow every active `G-` and `RULE-` item it lists. If a rule's title isn't
   enough, read its body with `read_events`. Check past answers before asking anything (RULE-check-before-ask).
2. Pick the work:
   - If Nowed named an item, take that one.
   - Otherwise take the oldest `BQ-` item with status `queued` that no other session claims (`read_claims`).
   - Read the item's body and the comments and answers on it. They say what "done" means and what was already decided.
3. Call `begin_answer` with role `builder`. Pass every item you expect to change, including your reply id. Pass the
   returned session to every `write_event`.
4. Build on a branch and open a PR (below). Post progress as comments on the BQ item.
5. Write your reply as `R-YYMMDD-NNN` (`item.create`, type `reply`), then call `end_answer` with that reply id.

**Reply numbers.** Use the block Nowed gives for this session (e.g. "use 152-159"). If he gives none, take the number
after the highest `R-` id in `resume`'s recent replies. Claim it in `begin_answer`, so a clash with the chat lane shows.

**Reply form.** Follow G-reply-order, G-reply-load and G-reply-moves:
- reading order, numbered lines (`152.1`, `152.2` …);
- at most 3 decisions per reply;
- one closing **Your moves** block, which is the only place answer codes (`152a`, `152b`) appear;
- keep it short (Q-reply-budget 137b a).

## Hard limits
- **Ask before moving or deleting files** outside your own branch or worktree.
- **Never touch** `C:\Users\akabd\Desktop\Nodach\nodach_sec` (WSL: `/mnt/c/Users/akabd/Desktop/Nodach/nodach_sec`).
- **Never put personal material on the shared record.** It belongs in `write_private` (G-compartments).
- **HIGH RISK PRs wait for Nowed's click.** That means money, legal, privacy, sign-in, access and governance changes.
  Label them `hold`, start the body with **HIGH RISK**, and never merge them yourself. `scripts/risk_class.py`
  decides low vs high from the changed files. When in doubt, treat a change as high risk.
- **Stripe runs in test mode only, on staging.** Production gets no Stripe secrets.
- **Every change goes on a branch with a PR.** Never push to `main`. Stacked PRs say what they sit on.

## Working copy
In WSL this clone sits at `~/workspace/demo-project/test code`. demo-project is an experiment repo and ignores this
folder. Build in a git worktree (`git worktree add <dir> -b b-<name> origin/main`) so this copy stays on `main`.
The root has three untracked Antigravity prototypes (`*_prototype.html`, `prototype.html`). Leave them where they are.

## Tests (the host has no Rust; use Docker)
These are what CI runs (`.github/workflows/ci.yml`). Run them from the worktree root and keep the caches outside it.
`$C` is any cache folder.

```sh
# engine
docker run --rm -v "$PWD":/src -v $C/cargo-registry:/usr/local/cargo/registry -v $C/target:/target \
  -e CARGO_TARGET_DIR=/target -w /src/engine rust:1.91-slim cargo test -p foundation-core -p exchange-core -p hub-core
# hub Worker
docker run --rm -v "$PWD":/src -v $C/cargo-registry:/usr/local/cargo/registry -v $C/target-hub:/target \
  -e CARGO_TARGET_DIR=/target -w /src/hub rust:1.91-slim \
  sh -c "rustup target add wasm32-unknown-unknown && cargo check --target wasm32-unknown-unknown && cargo test"
# Studio scenarios (STUDIO_ONLY=s78,s79 runs a subset)
docker run --rm -v "$PWD":/src -w /src mcr.microsoft.com/playwright/python:v1.55.0-noble \
  sh -c "pip install -q playwright==1.55.0 && python studio/tests/scenarios.py studio/index.html"
# scripts
for t in scripts/test_*.py; do python3 "$t" || echo "FAIL $t"; done
```

Pushing a branch redeploys staging through the train (`hub-staging`). The result lands on the hub as a comment on
`RUNS-hub-staging`.
