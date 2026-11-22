// Nodach Studio served by the hub (D-171 S1): the host shim in studio/standalone.js, run against a fake hub.
// No browser needed:  node studio/tests/standalone.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SHIM = readFileSync(new URL("../standalone.js", import.meta.url), "utf8");
const GENESIS = { label: "test", created: "2026-11-04T00:00:00Z", items: [{ id: "D-001", type: "decision", title: "x" }] };

function fakeHub({ signedIn = true, docs = [], privateStore = true } = {}) {
  const H = { docs: [...docs], posts: [], redirects: [], priv: [], privPosts: 0, privateStore };
  H.fetch = async (url, opts = {}) => {
    const u = new URL(url, "https://hub.test");
    const json = (v, status = 200) => ({ ok: status < 400, status, json: async () => v, text: async () => JSON.stringify(v) });
    if (u.pathname === "/health") return { ok: true, status: 200, text: async () => "ok " + (H.live || "dev") };
    if (!signedIn) return json({ error: "sign in" }, 401);
    if (opts.credentials !== "same-origin") return json({ error: "no cookie sent" }, 401);
    if (u.pathname === "/studio/api/me") return json({ login: "Morami49", id: "github:Morami49", owner: true });
    if (u.pathname === "/studio/api/genesis") return json(GENESIS);
    if (u.pathname === "/studio/api/events" && (opts.method || "GET") === "GET") {
      const after = +u.searchParams.get("after"), lim = +u.searchParams.get("limit");
      const page = H.docs.slice(after, after + lim);
      return json({ events: page, next: after + page.length, total: H.docs.length });
    }
    if (u.pathname === "/studio/api/events" && opts.method === "POST") {
      if (opts.headers?.["x-nodach-studio"] !== "1") return json({ error: "csrf" }, 403);
      H.profiles = [...(H.profiles || []), opts.headers?.["x-nodach-profile"] ?? null];
      if (H.refuseProfile && opts.headers?.["x-nodach-profile"] === H.refuseProfile)
        return json({ error: `profile ${H.refuseProfile} isn't one of yours`, refused: true, profile_mismatch: true }, 403);
      const b = JSON.parse(opts.body); H.posts.push(b);
      if (!H.docs.some(d => d.doc_id === b.doc_id)) H.docs.push({ doc_id: b.doc_id, event: b.event });
      return json({ id: b.doc_id, imported: 1 });
    }
    if (u.pathname === "/studio/api/private") {
      if (!H.privateStore) return json({ error: "no private store" }, 503);
      if ((opts.method || "GET") === "GET") {
        const path = u.searchParams.get("path");
        return json({ events: H.priv.filter(r => r.path === path).map(r => ({ doc_id: r.doc_id, event: r.event, path })), unreadable: 0 });
      }
      if (opts.headers?.["x-nodach-studio"] !== "1") return json({ error: "csrf" }, 403);
      const b = JSON.parse(opts.body); H.privPosts++;
      const dup = H.priv.some(r => r.path === b.path && r.doc_id === b.doc_id);
      if (!dup) H.priv.push(b);
      return json({ id: b.doc_id, stored: !dup });
    }
    return json({ error: "not found" }, 404);
  };
  return H;
}

function boot(H, extra = {}, store = new Map()) {
  const ctx = {
    fetch: H.fetch, setTimeout, clearTimeout, setInterval: () => 0, URL, console,
    document: { hidden: false, addEventListener() {} },
    localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
    __nodachPollMs: 0, ...extra,
  };
  ctx.window = ctx;
  ctx.window.location = { assign: to => H.redirects.push(to) };
  vm.createContext(ctx);
  vm.runInContext(SHIM, ctx);
  return ctx;
}

const next = () => new Promise(r => setTimeout(r, 5));
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("leaves the real claude.ai host alone", async () => {
  const H = fakeHub();
  const real = { use: async () => "real" };
  const ctx = boot(H, { claude: real });
  assert.equal(ctx.window.claude, real);
});

test("signed in: user is the GitHub login, the same id the connector writes with", async () => {
  const ctx = boot(fakeHub());
  const user = await ctx.window.claude.use("user");
  assert.equal(await user.id(), "github:Morami49");
  assert.equal(await user.isOwner(), true);
  assert.equal(await user.can("data.write"), true);
  assert.equal(await ctx.window.claude.use("mcp"), null);
});

test("signed out: no db, and the page is sent to sign in", async () => {
  const H = fakeHub({ signedIn: false });
  const ctx = boot(H);
  assert.equal(await ctx.window.claude.use("db"), null);
  assert.deepEqual(H.redirects, ["/studio/login"]);
});

test("genesis and every page of events arrive, newest first when asked", async () => {
  const docs = Array.from({ length: 1203 }, (_, i) => ({ doc_id: "d" + i, event: { ts: new Date(Date.UTC(2026, 10, 1) + i * 1000).toISOString(), kind: "comment", item: "D-001", body: { text: "n" + i } } }));
  const ctx = boot(fakeHub({ docs }));
  const db = await ctx.window.claude.use("db");
  let g; db.doc("seed/genesis").onSnapshot(s => (g = s)); await next(); await next();
  assert.equal(g.exists, true); assert.equal(g.data().items[0].id, "D-001");
  let q; db.collection("events").orderBy("ts", "desc").limit(1000).onSnapshot(s => (q = s));
  for (let i = 0; i < 20 && !q; i++) await next();
  assert.equal(q.size, 1000);
  assert.equal(q.docs[0].id, "d1202");
  assert.equal(q.docs[0].data()._id, undefined, "doc data carries no _id; the page adds it from the doc id");
  const older = await db.collection("events").where("ts", "<", q.docs[999].data().ts).orderBy("ts", "desc").limit(1000).get();
  assert.equal(older.size, 203);
});

test("a write goes to the hub with the anti-forgery header and shows at once", async () => {
  const H = fakeHub();
  const ctx = boot(H);
  const db = await ctx.window.claude.use("db");
  const seen = [];
  db.collection("events").orderBy("ts", "desc").limit(1000).onSnapshot(s => seen.push(s.size));
  await next(); await next();
  const ref = await db.collection("events").add({ ts: "2026-11-11T19:00:00Z", kind: "answer", item: "Q-x", body: { text: "A" }, author: { id: "github:Morami49" } });
  assert.match(ref.id, /^w[0-9a-z]+$/);
  assert.equal(H.posts.length, 1);
  assert.equal(H.posts[0].doc_id, ref.id);
  assert.equal(H.posts[0].event._id, undefined);
  assert.equal(seen.at(-1), 1);
  await next();
  assert.equal(seen.at(-1), 1, "the echo from the hub is not counted twice");
});

const PATH = "data/users/github:Morami49/vault/events";
const P1 = { ts: "2026-11-11T19:00:00Z", kind: "item.create", body: { id: "P-1", private: true } };

test("private items go to the hub's private store, never the shared record (I1)", async () => {
  const H = fakeHub();
  const db = await boot(H).window.claude.use("db");
  await db.collection(PATH).add(P1);
  await next();
  assert.equal(H.posts.length, 0, "nothing private in the shared record");
  assert.equal(H.priv.length, 1);
  assert.equal(H.priv[0].path, PATH);
  assert.equal(H.priv[0].event.body.id, "P-1");
  assert.equal(H.docs.length, 0);
  const got = await db.collection(PATH).orderBy("ts", "desc").limit(10).get();
  assert.equal(got.size, 1);
});

test("private items follow you to another browser or device (I1)", async () => {
  const H = fakeHub();
  const db1 = await boot(H).window.claude.use("db");
  await db1.collection(PATH).add(P1);
  await next();
  const db2 = await boot(H).window.claude.use("db");          // fresh browser: empty localStorage
  const got = await db2.collection(PATH).orderBy("ts", "desc").limit(10).get();
  assert.equal(got.size, 1);
  assert.equal(got.docs[0].data().body.id, "P-1");
});

test("items kept only in this browser before I1 are uploaded only after you press Upload, and only once", async () => {
  const H = fakeHub();
  const store = new Map([["nodach.priv." + PATH, JSON.stringify([{ ...P1, _id: "wold1" }])]]);
  const dom = fakeDom();
  const ctx = boot(H, { document: dom }, store);
  const db = await ctx.window.claude.use("db");
  const got = await db.collection(PATH).orderBy("ts", "desc").limit(10).get();
  assert.equal(got.size, 1, "still shown");
  assert.equal(H.priv.length, 0, "nothing uploaded without consent");
  const bar = dom.getElementById("nodach-private-offer");
  assert.match(bar.textContent, /1 private item is saved only in this browser/);
  bar.children[0].onclick(); await next(); await next();
  assert.equal(H.priv.length, 1);
  assert.equal(H.priv[0].doc_id, "wold1");
  const db2 = await boot(H, { document: fakeDom() }, store).window.claude.use("db");   // same browser, next visit
  await db2.collection(PATH).orderBy("ts", "desc").limit(10).get();
  assert.equal(H.priv.length, 1, "no duplicate upload");
});

test("a hub without the private store keeps private items in this browser, and sends them later", async () => {
  const H = fakeHub({ privateStore: false });
  const store = new Map();
  const db = await boot(H, {}, store).window.claude.use("db");
  await db.collection(PATH).add(P1);
  await next();
  assert.equal(H.priv.length, 0);
  assert.equal(H.posts.length, 0, "still never in the shared record");
  assert.equal((await db.collection(PATH).orderBy("ts", "desc").limit(10).get()).size, 1);
  H.privateStore = true;                                       // the hub gets its private store
  const db2 = await boot(H, {}, store).window.claude.use("db");
  const got = await db2.collection(PATH).orderBy("ts", "desc").limit(10).get();
  assert.equal(got.size, 1);
  assert.equal(H.priv.length, 1, "sent once the hub could take it");
});

test("a refused write surfaces as invalid_argument, as the page expects", async () => {
  const H = fakeHub();
  const orig = H.fetch;
  H.fetch = async (u, o = {}) => (o.method === "POST" ? { ok: false, status: 400, json: async () => ({}), text: async () => "bad" } : orig(u, o));
  const ctx = boot(H);
  const db = await ctx.window.claude.use("db");
  await assert.rejects(db.collection("events").add({ ts: "x", kind: "comment" }), e => e.code === "invalid_argument");
});

function fakeDom() {
  const els = [];
  const mk = tag => ({ tag, id: "", style: {}, children: [], textContent: "", setAttribute() {}, append(c) { this.children.push(c); }, onclick: null });
  return {
    els, hidden: false, addEventListener() {},
    body: { append: e => els.push(e), prepend: e => els.unshift(e) },
    createElement: mk,
    getElementById: id => els.find(e => e.id === id) || null,
  };
}

test("a newer build on the hub shows a reload banner, and never reloads by itself", async () => {
  const H = fakeHub(); H.live = "bbbbbbb";
  const dom = fakeDom(); let reloads = 0;
  const ctx = boot(H, { __nodachBuild: "aaaaaaa", document: dom });
  ctx.window.location.reload = () => reloads++;
  const v = await ctx.window.__nodach.checkVersion();
  assert.deepEqual({ ...v }, { current: "aaaaaaa", live: "bbbbbbb", stale: true });
  const b = dom.getElementById("nodach-update");
  assert.ok(b, "banner shown");
  assert.match(b.textContent, /Save what you're writing/);
  assert.equal(reloads, 0, "no reload until the person clicks");
  b.children[0].onclick(); assert.equal(reloads, 1);
  await ctx.window.__nodach.checkVersion();
  assert.equal(dom.els.filter(e => e.id === "nodach-update").length, 1, "one banner, not one per check");
});

test("same build: no banner", async () => {
  const H = fakeHub(); H.live = "aaaaaaa";
  const dom = fakeDom();
  const ctx = boot(H, { __nodachBuild: "aaaaaaa", document: dom });
  assert.equal((await ctx.window.__nodach.checkVersion()).stale, false);
  assert.equal(dom.getElementById("nodach-update"), null);
});

test("B-profile-windows: a write names the window's profile; a refused profile comes back as profile_mismatch with the hub's words", async () => {
  const H = fakeHub();
  const ctx = boot(H, { __nodachProfile: "P-work" });
  const db = await ctx.window.claude.use("db");
  await db.collection("events").add({ ts: "2026-11-22T10:00:00.000Z", kind: "comment", item: "D-001", body: { text: "hi" } });
  assert.deepEqual(H.profiles, ["P-work"]);
  H.refuseProfile = "P-work";
  let err = null;
  try { await db.collection("events").add({ ts: "2026-11-22T10:00:01.000Z", kind: "comment", item: "D-001", body: { text: "again" } }); } catch (e) { err = e; }
  assert.equal(err?.profile_mismatch, true);
  assert.match(err.message, /isn't one of yours/);
  ctx.window.__nodachProfile = undefined;
  await db.collection("events").add({ ts: "2026-11-22T10:00:02.000Z", kind: "comment", item: "D-001", body: { text: "no profile" } });
  assert.equal(H.profiles.at(-1), null, "no profile chosen: no header (the hub counts it as Personal)");
});

test("staging always wears a ribbon; production never does", async () => {
  const dom = fakeDom();
  const ctx = boot(fakeHub(), { __nodachBuild: "aaaaaaa", __nodachEnv: "staging", document: dom });
  assert.equal(ctx.window.__nodach.env, "staging");
  assert.match(dom.getElementById("nodach-staging").textContent, /Staging · test record/);
  const dom2 = fakeDom();
  boot(fakeHub(), { __nodachBuild: "aaaaaaa", __nodachEnv: "production", document: dom2 });
  assert.equal(dom2.getElementById("nodach-staging"), null);
});

let failed = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log("ok  ", name); }
  catch (e) { failed++; console.log("FAIL", name, "\n     ", e.message); }
}
console.log(`${tests.length - failed}/${tests.length} standalone shim tests passed`);
process.exit(failed ? 1 : 0);
