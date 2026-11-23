# Clanstack hub (H1)
The always-on node of the free ladder (D-089). Logic lives in `engine/crates/hub-core` (tested in CI);
this crate only connects it to Cloudflare Workers and a SQLite-backed Durable Object.

- Endpoints: `GET /health` (public); `GET /status`, `GET /pull?after=N&limit=M` (read or write token); `POST /push`, `POST /import` (write token). The record is never public (F-23).
- Nodach Studio (D-171 S1): `GET /studio` serves `studio/index.html` with `studio/standalone.js` as its host, behind GitHub
  sign-in (same OAuth App and `/callback` as the connector; Studio sign-ins carry a `studio-` state). The session is a signed
  cookie (`hub_core::session`, key derived from `CONNECTOR_SEED`), checked against `ALLOWED_GITHUB_LOGINS` on every request.
  `GET/POST /studio/api/events` read and write the same record as the connector; writes need the `x-nodach-studio` header.
  Private items stay in the browser. Sign out: `/studio/logout`.
- Staging (Q24 B, Q26 C): `wrangler deploy --env staging` → `clanstack-hub-staging` with its own Durable Object, so its own
  test record. `.github/workflows/hub-staging.yml` puts every branch that passes `ci` there; production still deploys only
  from main. Studio shows a staging ribbon there, and on both hubs shows "new version — reload when ready" (never reloads itself).
  Staging sign-in needs a second GitHub OAuth App (`GH_OAUTH_CLIENT_ID_STAGING` / `GH_OAUTH_CLIENT_SECRET_STAGING`).
- Canvas libraries (D-172): `GET /studio/vendor/canvas-lib.js` and `/studio/vendor/xyflow.css`, built into the Worker from
  `studio/vendor` (open-source code, no record data, so no sign-in needed).
- Runnable flows (D-174): `POST /studio/api/run {board, start}` (signed in, `x-nodach-studio` header). Step secrets come
  only from `NODE_SECRET_*` (GitHub secrets `NODE_SECRET_REED`, `NODE_SECRET_GEMINI`, `NODE_SECRET_ADZUNA_ID`, `NODE_SECRET_ADZUNA_KEY`).
- Money v1 (BQ-1 step 4, `hub_core::money`): each teamspace's ledger is events (invoice.create, payment.received,
  fee.charged, refund), fee rules are the node `FEE-rules`. Stripe is TEST MODE ONLY and staging only: GitHub secrets
  `NODE_SECRET_STRIPE_TEST` (sk_test_… / rk_test_…; anything else is refused) and `NODE_SECRET_STRIPE_TEST_WEBHOOK`
  (the whsec_… of a test-mode webhook endpoint pointing at `https://staging.nodach.com/stripe/webhook`, events
  `checkout.session.completed` and `charge.refunded`). Flow steps can't name either secret. Without the key, Studio says
  so in one line and invoices, recorded payments and the ledger still work.
- Scheduled flows (D-176): the record's Durable Object sets an hourly alarm on first request; each wake-up runs start steps
  whose `schedule` holds the current UK hour, once per slot, as `schedule`. Logged as `/alarm`.
- Private tier (I1, D-nodach-identity, Q53 A): `GET/POST /studio/api/private` keep each owner's private items on the hub,
  sealed with AES-256-GCM under each owner's random data key, which is stored only wrapped by a key from `PRIVATE_SEED`
  (envelope, with key versions; `PRIVATE_SEED_PREVIOUS` lets the secret rotate). Rows live in a `private` table apart
  from the signed record. Owner, path and doc id are bound into each row, so rows can't be moved between owners.
  `GET /studio/api/private/export` is the owner's decrypted backup. The connector's `read_private` tool serves the same
  rows to the signed-in owner when `CONNECTOR_PRIVATE_READ = "on"`. Browser-only items from before I1 upload only after the owner presses Upload.
- Local run: `npx wrangler@4 dev --local` with a `.dev.vars` file holding `HUB_WRITE_TOKEN` and `IMPORTER_SEED` (never committed).
- Deploy: automatic after green CI on main (`.github/workflows/hub-deploy.yml`), once the owner adds the Cloudflare secrets.
- Tested locally 2026-11-06: 24 Studio events imported as 25 signed facts (importer identity + 24), re-import added 0,
  write without token → 401, restart kept all 25 with the chain verified.
