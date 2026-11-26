/* Nodach Studio served by the hub (D-171 S1, Q21 A).
 *
 * The Studio page talks to its host through window.claude.use("db" | "user" | "mcp" | "assets").
 * Inside claude.ai the artifact host provides that. When the hub serves the page itself, this file
 * provides the same interface backed by the hub:
 *   db      shared "events" ↔ the hub's record (/studio/api/events), genesis ↔ /studio/api/genesis.
 *           Private paths (data/users/<id>/...) ↔ /studio/api/private (I1): kept on the hub encrypted with your
 *           own key, apart from the shared record; this browser keeps a copy and anything not yet sent.
 *           Every write sends x-nodach-profile: the profile this window works as (B-profile-windows).
 *   user    the GitHub login you signed in with (id "github:<login>", the same id the connector uses).
 *   mcp     none: the page is already talking to the hub, so there is nothing to sync.
 *   assets  none yet (uploads are not part of S1).
 * Sign-in is a cookie the page cannot read; every write also sends a header other sites cannot send.
 */
(() => {
  if (window.claude && window.claude.use) return; // inside claude.ai: the real host wins

  const API = (window.__nodachApi || "/studio/api/");
  const POLL_MS = window.__nodachPollMs ?? 15000;
  const toLogin = () => { try { window.location.assign("/studio/login"); } catch {} };

  async function api(path, opts = {}) {
    let r;
    try { r = await fetch(API + path, { credentials: "same-origin", cache: "no-store", ...opts }); }
    catch { throw { code: "unavailable", message: "can't reach the hub" }; }
    if (r.status === 401) { toLogin(); throw { code: "unauthenticated", message: "signed out" }; }
    if (r.status === 400 || r.status === 403) {
      const t = await r.text().catch(() => "");
      // B-profile-windows: the hub refused this window's profile; the page shows the hub's message (not "view only")
      let j = null; try { j = JSON.parse(t); } catch {}
      if (j && j.profile_mismatch) throw { code: "invalid_argument", profile_mismatch: true, message: String(j.error || t) };
      // BQ-1 step 2: a teamspace rule refused this one write (a role, a guest, a share level): show why, stay writable
      if (j && j.teamspace_refused) throw { code: "permission_denied", teamspace_refused: true, message: String(j.error || t) };
      throw { code: "invalid_argument", message: t };
    }
    if (!r.ok) throw { code: "unavailable", status: r.status, message: "hub answered " + r.status };
    return r.json();
  }

  /* ---------------------------------------------------------------- the shared record, kept in memory */
  const shared = [];            // Studio events, each with _id = doc id
  const seen = new Set();
  let cursor = 0, pulling = null;
  const listeners = new Set();
  const notify = () => listeners.forEach(f => { try { f(); } catch {} });

  function take(docs) {
    let n = 0;
    for (const d of docs || []) {
      if (!d || !d.doc_id || !d.event || typeof d.event !== "object" || seen.has(d.doc_id)) continue;
      seen.add(d.doc_id); shared.push({ ...d.event, _id: d.doc_id }); n++;
    }
    return n;
  }

  function pull() {
    if (pulling) return pulling;
    pulling = (async () => {
      let added = 0;
      for (let guard = 0; guard < 1000; guard++) {
        const p = await api(`events?after=${cursor}&limit=500`);
        added += take(p.events);
        const moved = p.next > cursor;
        cursor = p.next;
        if (!moved || cursor >= p.total) break;
      }
      if (added) notify();
      return added;
    })().finally(() => { pulling = null; });
    return pulling;
  }

  // 114b a: a support access opens events that sit before the cursor; read the whole record again (seen dedups)
  async function resync() {
    if (pulling) await pulling.catch(() => {});   // a pull in flight would move the cursor back to the end
    cursor = 0; return pull();
  }

  let timer = null;
  function startPolling() {
    if (timer || !POLL_MS) return;
    timer = setInterval(() => { if (!document.hidden) pull().catch(() => {}); }, POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) pull().catch(() => {}); });
  }

  // B-profile-windows: every write names the window's profile (the page sets window.__nodachProfile)
  const profileHeader = () => (window.__nodachProfile ? { "x-nodach-profile": String(window.__nodachProfile) } : {});

  const newId = () => "w" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);

  async function writeShared(id, ev) {
    const event = { ...ev }; delete event._id;
    await api("events", {
      method: "POST",
      headers: { "content-type": "application/json", "x-nodach-studio": "1", ...profileHeader() },
      body: JSON.stringify({ doc_id: id, event }),
    });
    if (!seen.has(id)) { seen.add(id); shared.push({ ...event, _id: id }); }
    notify();
    pull().catch(() => {});
    return { id };
  }

  /* ---------------------------------------------------------------- private areas (I1, Q53 A)
   * Kept on the hub, encrypted with your own key and apart from the shared record, so they follow you to any
   * device. This browser keeps a copy plus anything not yet sent (offline, or a hub without the private store),
   * and sends it when the hub can take it. Items saved only in this browser before I1 are uploaded only when you
 * press the button that offers it (consent first, A-spec-identity); until then they stay here and still show. */
  const isPrivate = path => path.startsWith("data/users/");
  const privKey = path => "nodach.priv." + path;
  const pendKey = "nodach.priv.pending";
  const priv = new Map();            // path -> rows (newest known state)
  const privLoaded = new Map();      // path -> promise of the first server load
  let privServer = null;             // true once the hub answered with a private store; false if it has none (503)
  const local = (k, d) => { try { return JSON.parse(localStorage.getItem(k) || d); } catch { return JSON.parse(d); } };
  const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } };
  function privRows(path) { return priv.get(path) || local(privKey(path), "[]"); }
  function remember(path, rows) {
    const byId = new Map(rows.map(r => [r._id, r]));
    priv.set(path, [...byId.values()]);
    save(privKey(path), priv.get(path));
  }
  async function sendPrivate(path, ev) {
    const d = { ...ev }; const doc_id = d._id; delete d._id;
    await api("private", { method: "POST", headers: { "content-type": "application/json", "x-nodach-studio": "1", ...profileHeader() },
      body: JSON.stringify({ path, doc_id, event: d }) });
  }
  async function flushPending() {
    const pend = local(pendKey, "[]"); if (!pend.length) return;
    const left = [];
    for (const p of pend) { try { await sendPrivate(p.path, p.ev); } catch { left.push(p); } }
    save(pendKey, left);
  }
  function loadPrivate(path) {
    if (privLoaded.has(path)) return privLoaded.get(path);
    const pr = (async () => {
      try {
        const r = await api("private?path=" + encodeURIComponent(path));
        privServer = true;
        const server = (r.events || []).map(x => ({ ...x.event, _id: x.doc_id }));
        const have = new Set(server.map(x => x._id));
        const pendIds = new Set(local(pendKey, "[]").map(p => p.ev._id));
        // one-time move of items that lived only in this browser before I1
        const onlyHere = local(privKey(path), "[]").filter(x => x && x._id && !have.has(x._id) && !pendIds.has(x._id));
        if (onlyHere.length) offerUpload(path, onlyHere);
        const mine = local(pendKey, "[]").filter(p => p.path === path).map(p => p.ev);   // shown now, sent or not
        await flushPending();
        remember(path, [...server, ...onlyHere, ...mine]);
      } catch (e) {
        if (e && e.status === 503) privServer = false;     // this hub has no private store yet: stay browser-only
        priv.set(path, local(privKey(path), "[]"));
      }
      notify();
    })();
    privLoaded.set(path, pr);
    return pr;
  }
  const offers = new Map();          // path -> items only in this browser, waiting for your go-ahead
  function offerUpload(path, rows) {
    offers.set(path, rows);
    const n = [...offers.values()].reduce((a, r) => a + r.length, 0);
    const el = typeof document.getElementById === "function" && document.getElementById("nodach-private-offer");
    if (el && el.remove) el.remove();
    bar("nodach-private-offer", `${n} private item${n === 1 ? " is" : "s are"} saved only in this browser. Upload ${n === 1 ? "it" : "them"} to your private store (encrypted, on any device)?`,
      "bottom:0;background:#2d3b2d;color:#fff", { label: "Upload", onclick: () => uploadPrivate() });
  }
  async function uploadPrivate() {
    const pend = local(pendKey, "[]");
    let n = 0;
    for (const [path, rows] of offers) for (const ev of rows) { pend.push({ path, ev }); n++; }
    save(pendKey, pend); offers.clear();
    await flushPending();
    const el = typeof document.getElementById === "function" && document.getElementById("nodach-private-offer");
    if (el && el.remove) el.remove();
    return n;
  }
  function privAdd(path, ev) {
    const rows = [...privRows(path), ev];
    priv.set(path, rows);
    if (!save(privKey(path), rows) && privServer !== true) throw { code: "quota_exceeded", message: "this browser's storage is full" };
    const pend = local(pendKey, "[]"); pend.push({ path, ev }); save(pendKey, pend);
    notify();
    flushPending().catch(() => {});
  }

  /* ---------------------------------------------------------------- the db interface the page expects */
  const snap = e => { const data = { ...e }; delete data._id; return { id: e._id, data: () => data, metadata: { fromCache: false } }; };

  function query(path, dir, lim, filters) {
    const rows = () => {
      let a = isPrivate(path) ? privRows(path) : path === "events" ? [...shared] : [];
      for (const f of filters) a = a.filter(f);
      a.sort((x, y) => x.ts < y.ts ? -1 : x.ts > y.ts ? 1 : 0);
      if (dir === "desc") a.reverse();
      if (lim) a = a.slice(0, lim);
      return a;
    };
    const result = () => { const a = rows(); return { docs: a.map(snap), size: a.length, empty: !a.length, metadata: { fromCache: false }, docChanges: () => [] }; };
    return {
      orderBy: (_f, d) => query(path, d || "asc", lim, filters),
      limit: n => query(path, dir, n, filters),
      where: (f, op, v) => query(path, dir, lim, [...filters, e => op === "<" ? e[f] < v : op === ">" ? e[f] > v : op === "<=" ? e[f] <= v : op === ">=" ? e[f] >= v : e[f] === v]),
      get: async () => { if (isPrivate(path)) await loadPrivate(path); else await pull().catch(() => {}); return result(); },
      onSnapshot: (next, err) => {
        let live = true;
        const f = () => { if (live) next(result()); };
        listeners.add(f);
        if (isPrivate(path)) { setTimeout(f, 0); loadPrivate(path).then(f); }
        else pull().then(() => { f(); startPolling(); }).catch(e => { if (err) err(e); });
        return () => { live = false; listeners.delete(f); };
      },
      add: async d => {
        const id = newId();
        if (isPrivate(path)) { privAdd(path, { ...d, _id: id }); return { id }; }
        if (path !== "events") throw { code: "invalid_argument", message: "unknown collection " + path };
        return writeShared(id, d);
      },
      doc: id => ({
        set: async d => {
          if (isPrivate(path)) { privAdd(path, { ...d, _id: id }); return; }
          if (path !== "events") throw { code: "invalid_argument", message: "unknown collection " + path };
          await writeShared(id, d);
        },
      }),
    };
  }

  const db = {
    doc: p => ({
      onSnapshot: (next, err) => {
        if (p !== "seed/genesis") { setTimeout(() => next({ exists: false, data: () => null, metadata: { fromCache: false } }), 0); return () => {}; }
        api("genesis").then(g => next({ exists: true, data: () => g, metadata: { fromCache: false } })).catch(e => { if (err) err(e); });
        return () => {};
      },
    }),
    collection: p => query(p, "asc", null, []),
  };

  let meP = null;
  const me = () => (meP = meP || api("me"));
  const user = {
    id: async () => (await me()).id,
    isOwner: async () => !!(await me()).owner,
    canEdit: async () => true,
    can: async k => (k === "data.write" ? true : null),
    me: async () => ({ id: (await me()).id }),
    // an email account is named email:<login> here but writes as github:<login> (F-email-author): both are you
    profiles: async ids => { const m = await me(); return Object.fromEntries(ids.map(i => [i, { id: i, name: i === m.id || i === "github:" + m.login ? m.login : "" }])); },
  };

  /* ---------------------------------------------------------------- release rules (Q26 C)
   * The hub tells the page its build and whether it is staging. The page never reloads itself: when the hub
   * is running a newer build it shows a banner, and the person reloads when they're ready (nothing half-typed
   * is lost). Staging always wears a ribbon, so a test record is never mistaken for the real one. */
  const BUILD = window.__nodachBuild || null;
  const ENV = window.__nodachEnv === "staging" ? "staging" : "production";
  const VERSION_MS = window.__nodachVersionMs ?? 60000;
  let staleShown = false;

  function whenBody(f) {
    if (typeof document.createElement !== "function") return;
    if (document.body) f(); else document.addEventListener("DOMContentLoaded", f);
  }
  function bar(id, text, css, button, top = false) {
    whenBody(() => {
      if (document.getElementById(id)) return;
      const d = document.createElement("div");
      d.id = id; d.setAttribute("role", "status");
      // In the page's own column (flex:none), not floating over it, so the bar pushes the header down and covers nothing.
      d.style.cssText = "flex:none;position:relative;z-index:30;padding:6px 12px;font:13px system-ui;text-align:center;" + css;
      d.textContent = text;
      if (button) {
        const b = document.createElement("button");
        b.textContent = button.label; b.style.cssText = "margin-left:10px;font:inherit;padding:2px 10px;cursor:pointer";
        b.onclick = button.onclick; d.append(b);
      }
      if (top) document.body.prepend(d); else document.body.append(d);
    });
  }
  if (ENV === "staging") bar("nodach-staging", `Staging · test record, not your real one · build ${BUILD || "?"}`, "background:#7a3e00;color:#fff", null, true);

  async function checkVersion() {
    let live = null;
    try { const r = await fetch("/health", { cache: "no-store" }); live = (await r.text()).trim().split(/\s+/)[1] || null; } catch {}
    const stale = !!(BUILD && live && live !== BUILD);
    if (stale && !staleShown) {
      staleShown = true;
      bar("nodach-update", `A new version of Nodach Studio is live (${live}). Save what you're writing, then reload.`,
        "background:#1f3a5f;color:#fff", { label: "Reload now", onclick: () => location.reload() });
    }
    return { current: BUILD, live, stale };
  }
  if (VERSION_MS && BUILD) {
    setInterval(() => { if (!document.hidden) checkVersion(); }, VERSION_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) checkVersion(); });
  }

  window.__nodach = { pull, resync, shared, standalone: true, checkVersion, env: ENV, build: BUILD, uploadPrivate };
  window.claude = {
    use: async n => {
      if (n === "db") { try { await me(); return db; } catch { return null; } }
      if (n === "user") { try { await me(); return user; } catch { return null; } }
      return null; // mcp, assets: not part of S1
    },
  };
})();
