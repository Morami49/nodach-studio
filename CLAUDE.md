# Claude Code in nodach-studio

This repository is Nodach Studio's main codebase (131a). The plan, decisions and work queue live on the **Nodach Studio
hub** (the `Nodach Studio` MCP connector), not in this file. Since HANDOFF-chat-to-code-261125, Claude Code is Nowed's
single place for Nodach: planning, deciding and building all happen here, in the same reply style the claude.ai chats
used. The hub is the memory, so nothing has to live in a long session.

## When Nowed says "go" (or "resume")
1. Call the hub's `resume` tool once per session. After that, call `begin_answer` without the digest (short mode).
   Follow every active `G-` and `RULE-` item it lists. If a rule's title isn't enough, read its body with
   `read_events`. Check past answers before asking anything (RULE-check-before-ask).
2. Read the latest `HANDOFF-` item (now HANDOFF-chat-to-code-261125). Its open items roll over into your reply until
   Nowed answers them or marks them done.
3. Pick the work:
   - If Nowed named an item, take that one.
   - Otherwise take the oldest `BQ-` item with status `queued` that no other session claims (`read_claims`).
   - Read the item's body and the comments and answers on it. They say what "done" means and what was already decided.
4. Call `begin_answer` with role `builder`. Pass every item you expect to change, including your reply id. Pass the
   returned session to every `write_event`.
5. Build on a branch and open a PR (below). Post progress as comments on the BQ item.
   **Working for a Brief** (`BRIEF-…`, BQ-in-flight-briefs): pass `brief` to `begin_answer`; it returns the Brief's goal,
   done-means, limits, budget and any redirect. Stay inside its limits. After each step, report under it:
   `write_event` comment `{text, report: {did, why, files, prs}}`. Every write returns `brief` with its state: on a
   `redirect`, follow it from the next step; on `paused` or `stopped`, stop at once, post one report saying where you
   stopped, and end the session. Never change a Brief's status yourself: pausing, stopping, resuming and redirecting
   belong to whoever holds Run agents (In flight in Studio).
6. Record each code Nowed sends (`200a a`) as an `answer` event on the item it answers (G-chat-graph).
7. Write your reply as `R-YYMMDD-NNN` (`item.create`, type `reply`), then call `end_answer` with that reply id.

**Reply numbers.** Use the block Nowed gives for this session (e.g. "use 152-159"). If he gives none, take the number
after the highest `R-` id in `resume`'s recent replies. Claim it in `begin_answer`, so a clash with the chat lane shows.

**Reply form.** Follow G-reply-order, G-reply-load and G-reply-moves:
- reading order; every line numbered with the reply's own number as prefix (`152.1`, `152.2` …). A number is never
  reused across replies;
- plain language; at most 3 decisions per reply, and decide what you can yourself;
- one closing **Your moves** block, which is the only place answer codes (`152a`, `152b`) appear. Each ask has coded
  options and your recommendation;
- unanswered asks roll over into the next reply. Nowed can close one in a single token (`152a done`,
  `152a deferred`);
- `?` from Nowed means: give new scenarios and real cases, not the same explanation again;
- give a direct link wherever Nowed has to act (a PR, a page, a setting);
- never ask him to review what he can't see (G-review-visible): a working link, click steps, and screenshots;
- interview first: state your assumptions, then ask;
- keep it short (Q-reply-budget 137b a).

## Addresses (BQ-domain-links)
Always give people these, never the workers.dev ones:
- Studio (production): https://studio.nodach.com
- Staging: https://staging.nodach.com/studio

The `*.ndab-utube.workers.dev` addresses are the Workers themselves. They're only for smoke tests, and a fallback
when nodach.com is down.

## Hard limits
- **Ask before moving or deleting files** outside your own branch or worktree.
- **Never touch** `C:\Users\akabd\Desktop\Nodach\nodach_sec` (WSL: `/mnt/c/Users/akabd/Desktop/Nodach/nodach_sec`).
- **Never put personal material on the shared record.** It belongs in `write_private` (G-compartments).
- **HIGH RISK PRs wait for Nowed's click.** That means money, legal, privacy, sign-in, access and governance changes.
  Label them `hold`, start the body with **HIGH RISK**, and never merge them yourself. `scripts/risk_class.py`
  decides low vs high from the changed files. When in doubt, treat a change as high risk.
- **Stripe runs in test mode only, on staging.** Production gets no Stripe secrets.
- **Every change goes on a branch with a PR.** Never push to `main`. Stacked PRs say what they sit on.

## Where a session runs
- **Cloud sessions** (claude.ai/code) get a fresh clone and have no WSL or Windows paths. They can build, test and
  push. A job that needs Nowed's PC (Desktop apps, local folders, Windows tools) goes to a local Claude Code session.
  Say so in the reply rather than trying it.
- **Keep sessions short:** one topic per session, then start a new one. The hub carries everything between them.

## Working copy (local sessions)
In WSL this clone sits at `~/workspace/demo-project/test code`. demo-project is an experiment repo and ignores this
folder. Build in a git worktree (`git worktree add <dir> -b b-<name> origin/main`) so this copy stays on `main`.
The root has three untracked Antigravity prototypes (`*_prototype.html`, `prototype.html`). Leave them where they are.

## Tests
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

The local host has no Rust, so it uses the Docker commands above. A cloud session can run them directly instead:
`cargo test` in `engine/`, and `python3 studio/tests/scenarios.py studio/index.html` after
`pip install playwright==1.55.0`, with `STUDIO_CHROME=/opt/pw-browsers/chromium`. `STUDIO_SHOTS=1` saves the
screenshots a PR needs.

Pushing a branch redeploys staging through the train (`hub-staging`). The result lands on the hub as a comment on
`RUNS-hub-staging`.
