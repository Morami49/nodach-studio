"""Clanstack Studio scenario tests: simulated people, states and organisations.

Each scenario is a person in a situation (tired, on a phone, view-only, a
collaborator, an outside AI relaying answers, two people at once, a store
that fails...) doing something they need to work. It passes only if the
page behaves the way that person would need it to.

    python tests/scenarios.py [path/to/index.html]

Runs the real page in Chromium against an in-memory store shared by every
open page, so multi-person scenarios are real concurrency, not mocks of it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PAGE = (Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "index.html").resolve()
GENESIS = json.loads((HERE / "genesis.json").read_text())
OUT = Path(os.environ.get("STUDIO_TEST_OUT", HERE / "out")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
# Wrap the page in the same skeleton the Artifact host adds at publish time.
WRAPPED = OUT / "page.html"
WRAPPED.write_text('<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
                   '<style>:root{color-scheme:light}body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}</style></head><body>'
                   + PAGE.read_text() + '</body></html>')

MOCK = r"""
(() => {
const C = window.__cfg;
let seq = 0;
const snapOf = e => ({id:e._id, data:()=>e, metadata:{fromCache:false}});
const listeners = [];
window.__evs = C.paths || {events: C.events || []};
window.__push = (path, evs) => { window.__evs[path] = evs; listeners.forEach(f => f()); };
const mine = path => !path.startsWith("data/users/") || path.startsWith("data/users/" + C.me + "/");
function query(path, dir, lim, filt){
  const rows = () => { let a=mine(path) ? [...(window.__evs[path]||[])] : []; if (filt) a = a.filter(filt);
    a.sort((x,y)=>x.ts<y.ts?-1:x.ts>y.ts?1:0); if(dir==="desc")a.reverse(); if(lim)a=a.slice(0,lim); return a; };
  const q = {
    orderBy:(f,d)=>query(path, d||"asc", lim, filt), limit:n=>query(path, dir, n, filt),
    where:(f,op,v)=>query(path, dir, lim, e => op==="<" ? e[f] < v : op===">" ? e[f] > v : e[f] === v),
    get: async()=>{ const a = rows(); return {docs:a.map(snapOf), size:a.length, empty:!a.length}; },
    doc: id => ({ set: async d => { if (C.readOnly || !mine(path)) throw {code:"invalid_argument", message:"no"}; await window.__bridgeAdd(path, {...d, _id:id}); } }),
    onSnapshot:(next, err)=>{ const f=()=>{ const a = rows();
        next({docs:a.map(snapOf), size:a.length, empty:!a.length, metadata:{fromCache:false}, docChanges:()=>[]}); };
      listeners.push(f); setTimeout(f, 30); return ()=>{}; },
    add: async d => { if (C.failWrites) throw {code:"unavailable", message:"offline"}; if (C.readOnly || !mine(path)) throw {code:"invalid_argument", message:"no"};
      // s13 root cause (18 Nov): ids were time + a per-page counter, so two pages writing in the same millisecond made
      // the same id and each page kept only one answer. The real store's ids are unique; so are these now.
      const e = {...d, _id:"e"+Date.now().toString(36)+Math.random().toString(36).slice(2, 8)+(seq++)}; await window.__bridgeAdd(path, e); return {id:e._id}; },
  };
  return q;
}
window.__autoSyncDelay = C.autoSyncDelay ?? 60000;   // tests opt in to the on-open sync
window.claude = { use: async n => {
  if (n === "user") return { id: async()=>C.me, isOwner: async()=>C.owner, canEdit: async()=>C.canEdit,
      can: async k => k==="data.write" ? C.canWrite : null,
      profiles: async ids => Object.fromEntries(ids.map(i => [i, {id:i, name:(C.names||{})[i]||""}])), me: async()=>({id:C.me}) };
  if (n === "db") return { doc: p => ({ onSnapshot:(next)=>{ setTimeout(()=>next({exists:true, data:()=>C.genesis, metadata:{fromCache:false}}), 20); return ()=>{}; } }),
                           collection: p => query(p, "asc", null) };
  if (n === "assets" && C.assets) {
    // a stand-in for the file store: keeps text uploads in memory and serves them at /_blob/<id>
    window.__blobs = {}; let k = 0;
    const realFetch = window.fetch.bind(window);
    window.fetch = async (u, o) => { const m = String(u).match(/^\/_blob\/(\w+)$/); if (m) return new Response(window.__blobs[m[1]] ?? "", {status: window.__blobs[m[1]] ? 200 : 404}); return realFetch(u, o); };
    return { upload: async (blob, opt) => { if ((opt?.type || blob.type) !== "text/plain") throw {code:"unsupported_type"}; const id = "a" + (++k);
      window.__blobs[id] = await blob.text(); return {id, url:"/_blob/" + id, sizeBytes:blob.size, contentType:"text/plain"}; } };
  }
  if (n === "mcp" && C.hub) {
    // A stand-in for the Clanstack hub connector: read_events pages by cursor, import skips doc ids it has.
    const H = C.hub; H.calls = [];
    return { callTool: async (server, tool, input) => {
      H.calls.push(tool); window.__hubCalls = H.calls;
      if (server !== "Clanstack hub") throw {code:"server_not_connected"};
      if (H.fail) throw {code:H.fail};
      if (tool === "read_events") { const after = input.after||0, lim = input.limit||100, page = H.docs.slice(after, after+lim);
        return {payload:{events:page, next:after+page.length, total:H.docs.length}}; }
      if (tool === "import_studio_events") { let n = 0; for (const d of input.docs) if (!H.docs.some(x => x.doc_id === d.doc_id)) { H.docs.push(d); n++; }
        window.__hubDocs = H.docs.map(x => x.doc_id); return {payload:{imported:n, already_there:input.docs.length-n}}; }
      if (tool === "hub_status") return {payload:{events:H.docs.length, imported_studio_events:H.docs.length, verified:true, detail:"", build:"test"}};
      throw {code:"tool_error", message:"unknown tool"}; } };
  }
  return null; } };
})();
"""


class Store:
    """One in-memory database shared by every open page. Paths under data/users/<id>/ reach only that person's pages."""
    def __init__(self, events=None):
        self.paths = {"events": list(events or [])}
        self.pages = []

    @property
    def events(self):
        return self.paths["events"]

    def all(self):
        return [e for v in self.paths.values() for e in v]

    async def add(self, path, ev):
        # F-64: two writes at once could deliver an older snapshot after a newer one, so a page kept only its own
        # answer. Delivery is now one at a time, always the full current list (as the real store's snapshots are).
        self.paths.setdefault(path, []).append(ev)
        if not hasattr(self, "_lock"):
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._deliver(path)

    async def _deliver(self, path):
        for p in self.pages:
            if path.startswith("data/users/") and not path.startswith(f"data/users/{p.me}/"):
                continue
            # F-nodach s13 (17 Nov): a delivery that failed was dropped silently, so a page could keep only its own
            # answer. The real store re-sends its latest snapshot until a listener has it, so retry a few times.
            for attempt in range(5):
                try:
                    await p.evaluate("([path, evs]) => window.__push(path, evs)", [path, list(self.paths[path])])
                    break
                except Exception:
                    await asyncio.sleep(0.1 * (attempt + 1))


async def open_page(browser, store: Store, cfg: dict, width=1440, height=900, name="p"):
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    full = {"genesis": GENESIS, "me": "u_nowed", "owner": True, "canEdit": True, "canWrite": True,
            "names": {"u_nowed": "Nowed", "u_rad": "Rad"}, **cfg, "paths": store.paths}
    await page.expose_function("__bridgeAdd", store.add)
    await page.add_init_script("window.__cfg = " + json.dumps(full) + ";" + MOCK)
    await page.goto(WRAPPED.as_uri())
    await page.wait_for_timeout(900)
    page.me = full["me"]
    store.pages.append(page)
    page.errors = errors
    return page


def ev(kind, item, body, who="u_nowed", ts="2026-11-01T15:00:00.000Z", branch="main", label="Nowed"):
    return {"_id": f"x{abs(hash((kind, item, ts, who))) % 10**9}", "ts": ts, "kind": kind, "item": item, "branch": branch,
            "body": body, "author": {"id": who, "label": label, "role": "owner" if who == "u_nowed" else "member"}}


async def exists(page, sel):
    return await page.locator(sel).count() > 0


async def visible_text(page):
    return await page.evaluate("document.body.innerText")


# ---------------------------------------------------------------- scenarios
async def s01_tired_misclick(b):
    """Tired, clicks Challenge with nothing typed. Nothing irreversible should happen; it asks for a reason."""
    st = Store(); p = await open_page(b, st, {})
    await p.evaluate("select('A-actionmap-v02')"); await p.wait_for_timeout(200)
    await p.click("#mkChallenge"); await p.wait_for_timeout(300)
    return len(st.events) == 0, f"{len(st.events)} events written by an empty challenge"


async def s02_undo(b):
    """Changes their mind right after answering. One tap undoes it; history keeps both (no silent overwrite)."""
    st = Store(); p = await open_page(b, st, {})
    await p.evaluate("select('Q-layers')"); await p.wait_for_timeout(200)
    await p.fill("#reply", "A"); await p.click("#sendAnswer"); await p.wait_for_timeout(300)
    if not await exists(p, "[data-undo]"):
        return False, "no undo control after answering"
    await p.click("[data-undo]"); await p.wait_for_timeout(300)
    ans = await p.evaluate("F.items.get('Q-layers').answers.length")
    return ans == 0 and len(st.events) == 2, f"answers shown after undo: {ans}; events kept: {len(st.events)}"


async def s03_stable_codes(b):
    """An outside AI answers from yesterday's pack. Codes must still point at the same items after new questions appear and others close."""
    st = Store(); p = await open_page(b, st, {})
    before = await p.evaluate("codeOf('Q-confirm-budget')")
    await st.add("events", ev("item.create", None, {"id": "Q-new1", "type": "question", "title": "New early question", "body": "", "status": "open"}, ts="2026-11-04T15:01:00.000Z"))
    await st.add("events", ev("status", "Q-layers", {"status": "closed"}, ts="2026-11-04T15:02:00.000Z"))
    await p.wait_for_timeout(300)
    after = await p.evaluate("codeOf('Q-confirm-budget')")
    return before == after and before is not None, f"code before {before}, after {after}"


async def s04_past_view_lock(b):
    """Looking at last week to remember a decision. Typing there must not silently write into today."""
    st = Store([ev("comment", "D-001", {"text": "hi"})]); p = await open_page(b, st, {})
    await p.evaluate("S.asof='0'; refresh(); select('Q-layers')"); await p.wait_for_timeout(300)
    dis = await p.evaluate("!!document.querySelector('#sendAnswer')?.disabled")
    banner = "Return to live" in await visible_text(p)
    return dis and banner, f"answer disabled={dis}, return-to-live shown={banner}"


async def s05_branch_awareness(b):
    """Exploring an idea on a branch. They must see that changes stay on the branch until merged."""
    st = Store([ev("branch.create", None, {"name": "try-quiet", "fork_ts": "2026-11-04T15:00:00.000Z", "from": "main"})])
    p = await open_page(b, st, {})
    await p.evaluate("S.branch='try-quiet'; refresh()"); await p.wait_for_timeout(200)
    t = await visible_text(p)
    ok = "try-quiet" in t and "until" in t.lower() and "merged" in t.lower()
    return ok, "branch banner explains merging" if ok else "no branch banner explaining merge"


async def s06_view_only_client(b):
    """A client opened a shared link with view-only access. They should know before typing that they can't change things."""
    st = Store(); p = await open_page(b, st, {"me": "u_client", "owner": False, "canEdit": False, "canWrite": False, "readOnly": True})
    await p.evaluate("select('Q-layers')"); await p.wait_for_timeout(200)
    dis = await p.evaluate("!!document.querySelector('#sendAnswer')?.disabled")
    banner = "view only" in (await visible_text(p)).lower()
    return dis and banner, f"answer disabled={dis}, view-only notice={banner}"


async def s07_names(b):
    """A co-founder answered. Nowed must see who it was, not 'Member'."""
    st = Store([ev("answer", "Q-layers", {"code": "Q1", "text": "B"}, who="u_rad", label="Member")])
    p = await open_page(b, st, {})
    await p.evaluate("select('Q-layers')"); await p.wait_for_timeout(400)
    t = await p.inner_text("#insp")
    return "Rad" in t, "author shown by name" if "Rad" in t else "author shown as generic label"


async def s08_long_history(b):
    """A busy community after months: 1,200 changes. The newest must still show."""
    evs = [ev("comment", "D-001", {"text": f"c{i}"}, ts=f"2026-11-{4 + i // 1000:02d}T{(i // 60) % 24:02d}:{i % 60:02d}:00.000Z") for i in range(1200)]
    evs.append(ev("answer", "Q-layers", {"code": "Q1", "text": "NEWEST"}, ts="2026-12-30T00:00:00.000Z"))
    st = Store(evs); p = await open_page(b, st, {})
    await p.wait_for_timeout(500)
    n = await p.evaluate("F.items.get('Q-layers').answers.length")
    return n == 1, f"newest answer visible: {n == 1}"


async def s09_ai_relay(b):
    """Pasting ChatGPT's reply. Multiple lines, a new idea, and the source recorded."""
    st = Store(); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=ask]")
    src = await exists(p, "#codeSource")
    if src:
        await p.select_option("#codeSource", "chatgpt")
    await p.fill("#codes", "Q1 A\nQ3 B, quieter at night\nNEW let voice notes become captures")
    await p.click("#codesGo"); await p.wait_for_timeout(500)
    kinds = sorted(e["kind"] for e in st.events)
    sourced = all((e["body"] or {}).get("source") == "chatgpt" for e in st.events) if st.events else False
    ok = kinds.count("answer") == 2 and "item.create" in kinds and sourced
    return ok, f"events {kinds}, source recorded={sourced}"


async def s10_keyboard_list(b):
    """Keyboard-only or screen-reader user. Every item must be reachable without the canvas."""
    st = Store(); p = await open_page(b, st, {})
    if not await exists(p, "#tabs button[data-v=list]"):
        return False, "no list view"
    await p.click("#tabs button[data-v=list]"); await p.wait_for_timeout(200)
    btns = await p.locator("#listView button[data-open]").count()
    await p.locator("#listView button[data-open]").first.focus(); await p.keyboard.press("Enter"); await p.wait_for_timeout(200)
    return btns >= 150 and await p.evaluate("!!S.sel"), f"{btns} items reachable by keyboard"


async def s11_offline_capture(b):
    """On a train with no signal, captures an idea. The text must not be lost."""
    st = Store(); p = await open_page(b, st, {"failWrites": True})
    await p.click("#tabs button[data-v=capture]")
    await p.fill("#capTitle", "Train idea"); await p.fill("#capBody", "Pools for community kitchens")
    await p.click("#capGo"); await p.wait_for_timeout(300)
    kept = await p.input_value("#capBody") == "Pools for community kitchens"
    await p.reload(); await p.wait_for_timeout(900)
    await p.click("#tabs button[data-v=capture]")
    survived = await p.input_value("#capBody") == "Pools for community kitchens"
    return kept and survived, f"kept after failure={kept}, survived reload={survived}"


async def s12_member_rights(b):
    """A collaborator can answer and propose, but marking things agreed is the owner's (charter)."""
    st = Store(); p = await open_page(b, st, {"me": "u_rad", "owner": False, "canEdit": False})
    await p.evaluate("select('T-scope')"); await p.wait_for_timeout(200)
    can_answer = await exists(p, "#sendAnswer") and not await p.evaluate("document.querySelector('#sendAnswer').disabled")
    no_agree = not await exists(p, "#mkAgreed")
    return can_answer and no_agree, f"can answer={can_answer}, agree hidden={no_agree}"


async def s13_two_people_at_once(b):
    """Nowed and Rad answer the same question at the same time. Both answers survive and both see them."""
    st = Store()
    a = await open_page(b, st, {}); c = await open_page(b, st, {"me": "u_rad", "owner": False, "canEdit": False})
    for pg, txt in ((a, "A from Nowed"), (c, "B from Rad")):
        await pg.evaluate("select('Q-delegation')"); await pg.fill("#reply", txt)
    await asyncio.gather(a.click("#sendAnswer"), c.click("#sendAnswer"))
    for _ in range(80):   # both writes travel to both pages; slow runners need more than 3 s (CI flakes 8 and 17 Nov)
        await a.wait_for_timeout(100)
        na = await a.evaluate("F.items.get('Q-delegation').answers.length"); nc = await c.evaluate("F.items.get('Q-delegation').answers.length")
        if na == 2 and nc == 2:
            break
    return na == 2 and nc == 2, f"answers seen: Nowed {na}, Rad {nc}"


async def s14_phone(b):
    """Mid-shift on a phone: opens on Capture, nothing scrolls sideways, and the Ask tab says how much needs him."""
    st = Store(); p = await open_page(b, st, {}, width=390, height=800)
    overflow = await p.evaluate("document.documentElement.scrollWidth > innerWidth")
    view = await p.evaluate("S.view")
    badge = await p.inner_text("#tabs button[data-v=ask]")
    has_badge = any(ch.isdigit() for ch in badge)
    await p.screenshot(path=str(OUT / "phone.png"))
    return (not overflow) and view == "capture" and has_badge, f"overflow={overflow}, view={view}, ask tab='{badge}'"


async def s15_undo_old_status(b):
    """Nowed's real case: a status set by mistake earlier must be revertible from its history line."""
    st = Store([ev("comment", "A-actionmap-v02", {"text": "⚑ Challenged"}), ev("status", "A-actionmap-v02", {"status": "challenged"}, ts="2026-11-01T15:00:01.000Z")])
    p = await open_page(b, st, {})
    await p.evaluate("select('A-actionmap-v02')"); await p.wait_for_timeout(200)
    if not await exists(p, "[data-revert]"):
        return False, "no revert on history lines"
    await p.locator("[data-revert]").last.click(); await p.wait_for_timeout(300)
    status = await p.evaluate("F.items.get('A-actionmap-v02').status")
    return status == "built", f"status after revert: {status}"


async def s16_overwhelm(b):
    """Low on energy, opens Ask. The badge counts only what truly needs him, and 28 proposed terms can be agreed in one go (and undone)."""
    st = Store(); p = await open_page(b, st, {})
    badge = await p.inner_text("#askBadge")
    await p.click("#tabs button[data-v=ask]")
    has_all = await exists(p, "[data-agreeall]")
    if has_all:
        await p.click("[data-agreeall]")
        # F-65: 28 writes now travel one at a time (F-64), so wait for the result instead of a fixed 2.5 s
        try: await p.wait_for_selector(".toast [data-undo]", timeout=20000)
        except Exception: pass
    left = await p.evaluate("askable().filter(i=>i.type==='term').length")
    undo = await exists(p, ".toast [data-undo]")
    undone = None
    if undo:
        await p.click(".toast [data-undo]")
        try: await p.wait_for_function("askable().filter(i=>i.type==='term').length === 28", timeout=20000)
        except Exception: pass
        undone = await p.evaluate("askable().filter(i=>i.type==='term').length")
    ok = badge.isdigit() and int(badge) <= 15 and has_all and left == 0 and undo and undone == 28
    return ok, f"badge={badge}, agree-all={has_all}, terms left={left}, undo offered={undo}, back after undo={undone}"


async def s17_disagreement(b):
    """Nowed and Rad gave different answers. The card must say so instead of hiding the conflict."""
    st = Store([ev("answer", "Q-layers", {"code": "Q1", "text": "A"}), ev("answer", "Q-layers", {"code": "Q1", "text": "B"}, who="u_rad", ts="2026-11-01T15:05:00.000Z")])
    p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=ask]")
    t = await p.inner_text("#askList")
    ok = "answered differently" in t
    return ok, "conflict surfaced on the card" if ok else "conflict not surfaced"


async def s18_clock_skew(b):
    """A device with a fast clock wrote in the 'future'. Reverting it from a correct device must still win."""
    st = Store([ev("status", "A-actionmap-v02", {"status": "challenged"}, ts="2027-01-01T00:00:00.000Z")])
    p = await open_page(b, st, {})
    await p.evaluate("select('A-actionmap-v02')"); await p.wait_for_timeout(200)
    await p.locator("[data-revert]").last.click(); await p.wait_for_timeout(300)
    status = await p.evaluate("F.items.get('A-actionmap-v02').status")
    return status == "built", f"status after revert: {status} (device clocks disagree; needs the Core's causal clock)"


async def s19_private_area(b):
    """Nowed notes something personal. It lands in his private area: invisible to a co-founder, left out of the AI pack unless he opts in."""
    st = Store()
    a = await open_page(b, st, {}); c = await open_page(b, st, {"me": "u_rad", "owner": False, "canEdit": False})
    await a.click("#tabs button[data-v=capture]")
    await a.check("#capPrivate"); await a.fill("#capTitle", "Sleep after night shifts"); await a.fill("#capBody", "private note")
    await a.click("#capGo"); await a.wait_for_timeout(500)
    in_private = len(st.paths.get("data/users/u_nowed/vault/events", [])) == 1 and len(st.events) == 0
    a_sees = await a.evaluate("[...F.items.values()].some(i=>i.title==='Sleep after night shifts' && i.private)")
    c_sees = await c.evaluate("[...F.items.values()].some(i=>i.title==='Sleep after night shifts')")
    await a.click("#tabs button[data-v=pack]"); pack = await a.inner_text("#pack")
    in_pack = "Sleep after night shifts" in pack
    await a.check("#packPriv"); in_pack_opt = "Sleep after night shifts" in await a.inner_text("#pack")
    ok = in_private and a_sees and not c_sees and not in_pack and in_pack_opt
    return ok, f"stored privately={in_private}, owner sees={a_sees}, co-founder sees={c_sees}, in pack by default={in_pack}, with opt-in={in_pack_opt}"


SETTINGS = [("SET-cut-small", "Cut: small seller", 500), ("SET-cut-large", "Cut: after the threshold", 800),
            ("SET-cut-community", "Cut: community / non-profit", 300), ("SET-tier-threshold", "Threshold over 12 months", 5000000)]


def setting_events():
    return [ev("item.create", None, {"id": k, "type": "setting", "title": t, "body": "", "status": "agreed", "value": v},
               who="claude", label="Claude", ts=f"2026-11-01T14:0{i}:00.000Z") for i, (k, t, v) in enumerate(SETTINGS)]


async def s20_addresses(b):
    """Nowed pastes an address from a chat into Go to. It must land on the exact item, version and line, and code addresses must link to GitHub."""
    reply = ev("item.create", None, {"id": "R-261105-2", "type": "asset", "title": "Reply R-261105-2", "status": "built",
                                     "body": "1) first\n3.1) the line that matters\n4) last"}, who="claude", label="Claude", ts="2026-11-01T14:30:00.000Z")
    st = Store([reply, ev("item.edit", "D-001", {"body": "second wording"}, ts="2026-11-02T10:00:00.000Z")])
    p = await open_page(b, st, {})
    await p.fill("#goAddr", "chat/R-261105-2#3.1"); await p.press("#goAddr", "Enter"); await p.wait_for_timeout(400)
    hit = await p.evaluate("document.querySelector('#inspBody .ln.hit')?.textContent || ''")
    await p.fill("#goAddr", "studio/D-001@0"); await p.press("#goAddr", "Enter"); await p.wait_for_timeout(400)
    past = await p.evaluate("S.asof !== null && document.querySelector('#inspector h2')?.textContent || ''")
    body0 = await p.evaluate("document.querySelector('#inspBody')?.textContent || ''")
    await p.evaluate("S.asof=null; refresh()")
    await p.fill("#goAddr", "repo/b254818:engine/crates/exchange-core/src/lib.rs#L10"); await p.press("#goAddr", "Enter"); await p.wait_for_timeout(300)
    link = await p.evaluate("document.querySelector('.toast a')?.href || ''")
    ok = "3.1) the line" in hit and past and "second wording" not in body0 \
        and link == "https://github.com/Morami49/clanstack-studio/blob/b254818/engine/crates/exchange-core/src/lib.rs#L10" and not p.errors
    return ok, f"line hit={hit[:30]!r}, past view={bool(past)}, old body shown={'second wording' not in body0}, link={link[-40:]}, errors={p.errors[:1]}"


async def s21_money_settings(b):
    """A seller checks what they'd keep; the owner raises a cut, then undoes it. Maths must match the engine and every change must be a version."""
    st = Store(setting_events()); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=money]"); await p.wait_for_timeout(200)
    await p.fill("#calcAmt", "1200"); await p.fill("#calcSales", "0"); await p.wait_for_timeout(100)
    small = await p.inner_text("#calcOut")
    await p.fill("#calcSales", "50000"); await p.wait_for_timeout(100)
    large = await p.inner_text("#calcOut")
    await p.fill('[data-set="SET-cut-small"]', "600"); await p.click('[data-save="SET-cut-small"]'); await p.wait_for_timeout(300)
    after = await p.evaluate("setting('SET-cut-small')")
    await p.click("[data-undo]"); await p.wait_for_timeout(300)
    undone = await p.evaluate("setting('SET-cut-small')")
    # a co-founder without edit rights sees no edit boxes
    c = await open_page(b, st, {"me": "u_rad", "owner": False, "canEdit": False})
    await c.click("#tabs button[data-v=money]"); await c.wait_for_timeout(200)
    c_inputs = await c.locator("[data-set]").count()
    ok = "£1,140.00" in small and "£60.00" in small and "£96.00" in large and "growth £32.40" in small.replace("\n", " ") \
        and after == 600 and undone == 500 and c_inputs == 0
    return ok, f"small ok={'£1,140.00' in small}, large 8%={'£96.00' in large}, split ok={'growth £32.40' in small}, set→{after}, undo→{undone}, co-founder edit boxes={c_inputs}"


async def s22_duplicate_id(b):
    """An AI (or a stale tab) re-creates an id that exists. The original and its history must survive, and the UI must refuse a duplicate."""
    dup = ev("item.create", None, {"id": "D-001", "type": "decision", "title": "Imposter", "body": "overwritten", "status": "agreed"},
             who="claude", label="Claude", ts="2026-11-03T10:00:00.000Z")
    st = Store([dup]); p = await open_page(b, st, {})
    title = await p.evaluate("F.items.get('D-001').title")
    hist = await p.evaluate("F.items.get('D-001').history.length")
    refused = await p.evaluate("write('item.create', null, {id:'D-001', type:'decision', title:'x', body:'y'})")
    ok = title != "Imposter" and hist >= 2 and refused is None and not p.errors
    return ok, f"title kept={title!=chr(73)+'mposter'} ({title[:30]!r}), history={hist}, duplicate write refused={refused is None}"


async def s23_same_order_everywhere(b):
    """Two nodes write to the same item at the same instant (Q44 C now). Every viewer must fold them in the same order."""
    ts = "2026-11-06T10:00:00.000Z"
    a = ev("item.edit", "D-001", {"title": "From Studio Web"}, ts=ts); a["_id"] = "claude-zz"
    h = ev("item.edit", "D-001", {"title": "From the PC node"}, ts=ts); h["_id"] = "hub-aa"
    seen = []
    for order in ([a, h], [h, a]):
        p = await open_page(b, Store(order), {})
        seen.append(await p.evaluate("F.items.get('D-001').title"))
    ok = seen[0] == seen[1]
    return ok, f"titles by load order: {seen} (must match)"


async def s24_hub_sync(b):
    """Nowed syncs Studio with the hub (D-129): missing changes go up with their authors, hub writes come down, and a second sync changes nothing."""
    e1 = ev("comment", "D-001", {"text": "one"}, ts="2026-11-08T10:00:00.000Z"); e1["_id"] = "claude-a1"
    e2 = ev("comment", "D-001", {"text": "two"}, who="claude", label="Claude", ts="2026-11-08T10:01:00.000Z"); e2["_id"] = "claude-a2"
    hubw = {"ts": "2026-11-08T10:02:00.000Z", "kind": "comment", "item": "D-001", "branch": "main", "body": {"text": "from Claude via hub"},
            "author": {"id": "github:Morami49", "label": "Morami49", "role": "owner"}, "via": "claude-connector"}
    hub = {"docs": [{"doc_id": "claude-a1", "event": {k: v for k, v in e1.items() if k != "_id"}}, {"doc_id": "hub-xyz", "event": hubw}]}
    st = Store([e1, e2]); p = await open_page(b, st, {"hub": hub})
    await p.click("#tabs button[data-v=hub]"); await p.click("#hubSync"); await p.wait_for_timeout(900)
    note1 = await p.inner_text("#hubNote")
    hub_ids = await p.evaluate("window.__hubDocs || []")
    came = any(e.get("_id") == "hub-xyz" for e in st.events)
    runs = [e for e in st.events if e.get("item") == "RUNS-hub-sync" or (e.get("body") or {}).get("id") == "RUNS-hub-sync"]
    await p.click("#hubSync"); await p.wait_for_timeout(900)
    note2 = await p.inner_text("#hubNote")
    # the second sync copies only the first run's own record (P-13): the run-log item and its comment
    ok = ("1 copied" in note1 and "1 brought here" in note1 and "claude-a2" in hub_ids and came and len(runs) == 2
          and "2 copied" in note2 and "0 brought here" in note2 and not p.errors)
    return ok, f"first: {note1[:80]!r}; hub has a2={'claude-a2' in hub_ids}; hub write landed={came}; run records={len(runs)}; second: {note2[:60]!r}"


async def s25_more_than_1000(b):
    """A Studio with more than 1,000 changes (F-62). The oldest item must not drop out of the fold."""
    first = ev("item.create", None, {"id": "D-900", "type": "decision", "title": "Oldest", "body": "x", "status": "agreed"},
               ts="2026-01-01T00:00:00.000Z")
    rest = [ev("comment", "D-001", {"text": str(i)}, ts=f"2026-02-01T00:{i//60:02d}:{i%60:02d}.000Z") for i in range(1005)]
    for i, e in enumerate(rest): e["_id"] = f"c{i:05d}"
    p = await open_page(b, Store([first] + rest), {}); await p.wait_for_timeout(800)
    has = await p.evaluate("F.items.has('D-900')")
    n = await p.evaluate("S.events.length")
    ok = has and n == 1006 and not p.errors
    return ok, f"oldest item present={has}, changes folded={n}/1006"


async def s26_hub_not_connected(b):
    """Someone opens the Hub view without the connector. They must be told exactly how to fix it, and nothing breaks."""
    p = await open_page(b, Store([]), {"hub": {"docs": [], "fail": "server_not_connected"}})
    await p.click("#tabs button[data-v=hub]"); await p.click("#hubSync"); await p.wait_for_timeout(500)
    note = await p.inner_text("#hubNote")
    ok = "Settings" in note and "Connectors" in note and not p.errors
    return ok, f"message: {note[:100]!r}"


async def s27_read_on_open(b):
    """Q102 A: Nowed opens Studio; hub changes arrive by themselves, but nothing is copied up until enough clean runs."""
    hubw = {"ts": "2026-11-08T10:02:00.000Z", "kind": "comment", "item": "D-001", "branch": "main", "body": {"text": "via hub"},
            "author": {"id": "github:Morami49", "label": "Morami49", "role": "owner"}, "via": "claude-connector"}
    st = Store([]); p = await open_page(b, st, {"hub": {"docs": [{"doc_id": "hub-q1", "event": hubw}]}, "autoSyncDelay": 300})
    await p.wait_for_timeout(1500)
    came = any(e.get("_id") == "hub-q1" for e in st.events)
    calls = await p.evaluate("window.__hubCalls || []")
    ok = came and "import_studio_events" not in calls and not p.errors
    return ok, f"hub change arrived={came}; calls={calls}"


async def s28_push_after_clean_runs(b):
    """After 5 clean recorded runs (the default), copying up runs by itself on open; a viewer who isn't the owner never auto-syncs."""
    runs = [ev("item.create", None, {"id": "RUNS-hub-sync", "type": "asset", "title": "Hub sync runs", "body": "", "status": "built"},
               ts="2026-11-08T09:00:00.000Z")]
    runs += [ev("comment", "RUNS-hub-sync", {"text": "ok", "clean": True, "pushed": 0, "pulled": 0}, ts=f"2026-11-08T09:0{i+1}:00.000Z") for i in range(5)]
    local = ev("comment", "D-001", {"text": "local"}, ts="2026-11-08T11:00:00.000Z")
    hub = {"docs": []}
    st = Store(runs + [local]); p = await open_page(b, st, {"hub": hub, "autoSyncDelay": 300})
    await p.wait_for_timeout(1800)
    pushed = await p.evaluate("(window.__hubDocs || []).length")
    st2 = Store([local]); q = await open_page(b, st2, {"hub": {"docs": []}, "autoSyncDelay": 300, "owner": False, "me": "u_rad"})
    await q.wait_for_timeout(1200)
    other = await q.evaluate("window.__hubCalls || []")
    ok = pushed >= 7 and other == [] and not p.errors and not q.errors
    return ok, f"copied up on open={pushed}; non-owner calls={other}"


async def s29_runs_timeline(b):
    """Nowed checks what ran overnight: every executor's runs in one list, newest first, failures marked, filterable, each with its undo."""
    def mk(ex):
        e = ev("item.create", None, {"id": f"RUNS-{ex}", "type": "asset", "title": ex, "body": "", "status": "built"}, ts="2026-11-09T00:00:00.000Z")
        e["_id"] = "runs-" + ex
        return e
    runs = [mk("branch-cleanup"), mk("hub-deploy"), mk("hub-sync"),
            ev("comment", "RUNS-branch-cleanup", {"text": "Deleted 1 merged branch(es): b-x@abc.", "outcome": "success", "policy": "auto",
               "trigger": "push", "undo": "Restore from sha", "run_url": "https://github.com/x/y/actions/runs/1"}, who="github:service", ts="2026-11-09T01:00:00.000Z"),
            ev("comment", "RUNS-hub-deploy", {"text": "Deploy of abc1234 failure", "outcome": "failure", "policy": "auto"}, who="github:service", ts="2026-11-09T02:00:00.000Z"),
            ev("comment", "RUNS-hub-sync", {"text": "Read from the hub on open: 1 brought here.", "clean": True, "mode": "read", "auto": True}, ts="2026-11-09T03:00:00.000Z")]
    p = await open_page(b, Store(runs), {})
    await p.click("#tabs button[data-v=runs]"); await p.wait_for_timeout(200)
    items = await p.locator("#runList li").all_inner_texts()
    first_sync = "hub-sync" in items[0] if items else False
    failed = any("failed" in t and "hub-deploy" in t for t in items)
    undo = any("Undo: Restore from sha" in t for t in items)
    await p.select_option("#runOutcome", "failure"); await p.wait_for_timeout(100)
    only = await p.locator("#runList li").all_inner_texts()
    ok = len(items) == 3 and first_sync and failed and undo and len(only) == 1 and "hub-deploy" in only[0] and not p.errors
    return ok, f"runs={len(items)}, newest first={first_sync}, failure marked={failed}, undo shown={undo}, filtered={len(only)}"


async def s30_builder_pipeline(b):
    """Nowed adds a builder, moves them along, and tries to publish. They stay private, publishing waits for a release, and Next up points at the right step."""
    kit = ev("item.create", None, {"id": "KIT-builder-interview", "type": "asset", "title": "Kit", "status": "built",
             "body": "## Outreach\nHi {name}, a {kind} chat?\n## Questions\n1. What do you build, {name}?"}, ts="2026-11-09T00:00:00.000Z")
    st = Store([kit]); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=builders]")
    await p.fill("#bName", "Amina"); await p.select_option("#bKind", index=0); await p.click("#bAdd"); await p.wait_for_timeout(300)
    await p.fill("#bName", "Joe"); await p.select_option("#bKind", index=2); await p.click("#bAdd"); await p.wait_for_timeout(300)
    private_only = not any((e.get("body") or {}).get("title") in ("Amina", "Joe") for e in st.events)
    in_vault = sum(1 for k, v in st.paths.items() if k.startswith("data/users/u_nowed/") for e in v if (e.get("body") or {}).get("title") in ("Amina", "Joe"))
    bid = await p.evaluate("[...F.items.values()].find(i => i.title === 'Amina').id")
    await p.select_option(f'[data-bstage="{bid}"]', "drafted"); await p.wait_for_timeout(300)
    nxt = await p.inner_text("#bNext")
    await p.select_option(f'[data-bstage="{bid}"]', "published"); await p.wait_for_timeout(300)
    blocked = await p.evaluate(f"F.items.get('{bid}').status") == "drafted"
    await p.click(f'[data-brel="{bid}"]'); await p.fill(f'[data-relform="{bid}"] [data-relparts]', "quotes only")
    await p.check(f'[data-relform="{bid}"] [data-relfmt="quotes"]')
    await p.click(f'[data-relsave="{bid}"]'); await p.wait_for_timeout(300)
    await p.select_option(f'[data-bstage="{bid}"]', "published"); await p.wait_for_timeout(300)
    published = await p.evaluate(f"F.items.get('{bid}').status") == "published"
    await p.select_option("#bKitFor", bid); kit_txt = await p.inner_text("#bKit")
    ok = (private_only and in_vault == 2 and nxt.strip().startswith("Amina") and "Confirm their release" in nxt and blocked and published
          and "Hi Amina" in kit_txt and not p.errors)
    return ok, f"private only={private_only} ({in_vault} in vault), next up first={nxt.split(chr(10))[0][:40]!r}, publish blocked without release={blocked}, published after={published}, kit personalised={'Hi Amina' in kit_txt}"


async def s31_content_cycles(b):
    """Q109: one interview becomes pieces in several formats. Only formats the guest released can be published; results feed the content tests."""
    kit = ev("item.create", None, {"id": "KIT-builder-interview", "type": "asset", "title": "Kit", "status": "built", "body": "## Outreach\nHi {name}"}, ts="2026-11-09T00:00:00.000Z")
    st = Store([kit]); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=builders]")
    await p.fill("#bName", "Amina"); await p.click("#bAdd"); await p.wait_for_timeout(300)
    bid = await p.evaluate("[...F.items.values()].find(i => i.title === 'Amina').id")
    await p.click(f'[data-brel="{bid}"]'); f = f'[data-relform="{bid}"]'
    await p.fill(f"{f} [data-relparts]", "quotes and a clip"); await p.check(f'{f} [data-relfmt="quotes"]'); await p.check(f'{f} [data-relfmt="video"]')
    await p.click(f'[data-relsave="{bid}"]'); await p.wait_for_timeout(300)
    await p.click(f'[data-bmat="{bid}"]'); m = f'[data-matpanel="{bid}"]'
    await p.fill(f"{m} [data-rawwhere]", "drive/amina.m4a"); await p.click(f'[data-rawadd="{bid}"]'); await p.wait_for_timeout(300)
    for fmt, ch in (("quotes", "LinkedIn"), ("audio", "Spotify")):
        await p.select_option(f"{m} [data-piecefmt]", fmt); await p.fill(f"{m} [data-piecech]", ch); await p.click(f'[data-pieceadd="{bid}"]'); await p.wait_for_timeout(300)
    ps = await p.evaluate(f"pieces(F.items.get('{bid}'))")
    q = next(x for x in ps if x["format"] == "quotes"); a = next(x for x in ps if x["format"] == "audio")
    await p.select_option(f'[data-piecestat="{bid}|{a["id"]}"]', "published"); await p.wait_for_timeout(300)
    audio_blocked = (await p.evaluate(f"pieces(F.items.get('{bid}')).find(x => x.id === '{a['id']}').status")) == "planned"
    await p.select_option(f'[data-piecestat="{bid}|{q["id"]}"]', "published"); await p.wait_for_timeout(300)
    form = f'{m} [data-pieceres="{bid}|{q["id"]}"] >> xpath=..'
    await p.fill(f'{form} >> [data-metric="channel measure"]', "42"); await p.fill(f'{form} >> [data-metric="replies"]', "3")
    await p.click(f'[data-pieceres="{bid}|{q["id"]}"]'); await p.wait_for_timeout(300)
    tests = await p.inner_text("#bTests")
    raw = await p.inner_text(m)
    ok = audio_blocked and "Quote cards" in tests and "42" in tests and "drive/amina.m4a" in raw and not p.errors
    return ok, f"audio blocked by release={audio_blocked}, tests show quotes avg 42={'42' in tests}, raw listed={'drive/amina.m4a' in raw}"


async def s32_private_files_and_views(b):
    """Q111 B + Q110: a guest's recording is encrypted before it leaves the browser; the wrong passphrase can't open it; Nowed designs and saves his own view."""
    kit = ev("item.create", None, {"id": "KIT-builder-interview", "type": "asset", "title": "Kit", "status": "built", "body": "## Outreach\nHi"}, ts="2026-11-09T00:00:00.000Z")
    st = Store([kit]); p = await open_page(b, st, {"assets": True})
    await p.click("#tabs button[data-v=builders]")
    await p.fill("#bName", "Amina"); await p.click("#bAdd"); await p.wait_for_timeout(300)
    bid = await p.evaluate("[...F.items.values()].find(i => i.title === 'Amina').id")
    await p.click(f'[data-bmat="{bid}"]'); m = f'[data-matpanel="{bid}"]'
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    await p.set_input_files(f"{m} [data-rawfile]", files=[{"name": "photo.png", "mimeType": "image/png", "buffer": png}])
    await p.click(f'[data-rawup="{bid}"]'); await p.wait_for_timeout(300)
    no_pass = await p.evaluate("Object.keys(window.__blobs || {}).length") == 0
    await p.fill("#vPass", "correct horse battery"); await p.click(f'[data-rawup="{bid}"]'); await p.wait_for_timeout(1500)
    stored = await p.evaluate("Object.values(window.__blobs)[0] || ''")
    sealed = stored.startswith("CLANSTACK-ENC1") and "IHDR" not in stored and "photo.png" not in stored.split("\n", 2)[2]
    await p.fill("#vPass", "wrong passphrase!!"); await p.click(f"{m} [data-vopen]"); await p.wait_for_timeout(1500)
    wrong_blocked = await p.locator(f"{m} [data-vview] img").count() == 0
    await p.fill("#vPass", "correct horse battery"); await p.click(f"{m} [data-vopen]"); await p.wait_for_timeout(1500)
    shown = await p.locator(f"{m} [data-vview] img").count() == 1
    shared_leak = any("photo.png" in json.dumps(e) for e in st.events)
    await p.select_option("#dGroup", "kind"); await p.select_option("#dAgg", "sum"); await p.click("#dSave"); await p.wait_for_timeout(300)
    view = await p.evaluate("F.items.get('DASH-content')")
    ok = (no_pass and sealed and wrong_blocked and shown and not shared_leak and view and view["private"] and '"group":"kind"' in view["value"] and not p.errors)
    return ok, f"refused without passphrase={no_pass}, stored sealed={sealed}, wrong passphrase blocked={wrong_blocked}, opened with right one={shown}, name kept out of shared record={not shared_leak}, private view saved={bool(view and view['private'])}"


async def s33_studio_edits_studio(b):
    """D-152/153: Nowed adds an item type and changes the look from Studio's own Studio tab; both take effect live and are versions; a change request can be proposed and approved."""
    st = Store([]); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=self]")
    await p.fill("#tyKey", "venue"); await p.fill("#tyLabel", "Venue"); await p.click("#tyAdd"); await p.wait_for_timeout(400)
    in_capture = await p.evaluate("[...document.querySelectorAll('#capType option')].some(o => o.value === 'venue')")
    in_filters = await p.evaluate("TYPES.venue === 'Venue'")
    await p.evaluate("""() => { const i = document.querySelector('[data-tok="--accent"]'); i.value = '#aa0000'; i.dispatchEvent(new Event('input')); }""")
    await p.click("#lkSave"); await p.wait_for_timeout(400)
    accent = await p.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    saved = await p.evaluate("F.items.get('SET-ui-theme')?.value || ''")
    await p.fill("#crTitle", "Show a weekly digest"); await p.fill("#crTest", "Digest lists last week's decisions"); await p.click("#crAdd"); await p.wait_for_timeout(400)
    cid = await p.evaluate("[...F.items.values()].find(i => i.type === 'change')?.id")
    await p.select_option(f'[data-crstage="{cid}"]', "approved"); await p.wait_for_timeout(400)
    status = await p.evaluate(f"F.items.get('{cid}').status")
    q = await open_page(b, st, {}); fresh_accent = await q.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    ok = in_capture and in_filters and accent == "#aa0000" and "#aa0000" in saved and status == "approved" and fresh_accent == "#aa0000" and not p.errors
    return ok, f"type in capture={in_capture}, in types={in_filters}, accent live={accent}, saved={'#aa0000' in saved}, change approved={status}, other page sees look={fresh_accent}"


async def s34_command_map(b):
    """Q119 C: Nowed types commands; in suggest mode they wait as nodes until confirmed; act runs reversible ones at once; irreversible ones always ask; the log stays private."""
    st = Store([]); p = await open_page(b, st, {})
    await p.click("#tabs button[data-v=command]")
    await p.fill("#cmdText", "propose change weekly digest"); await p.press("#cmdText", "Enter"); await p.wait_for_timeout(500)
    waiting = await p.evaluate("commands()[0]?.state") == "suggested" and not await p.evaluate("[...F.items.values()].some(i => i.type === 'change')")
    nodes = await p.locator("#cmdMap [data-cmd]").count()
    await p.click("#cmdPanel [data-cmdgo]"); await p.wait_for_timeout(500)
    cr = await p.evaluate("[...F.items.values()].find(i => i.type === 'change')?.id")
    await p.select_option("#cmdMode", "act")
    await p.fill("#cmdText", "capture idea venue list"); await p.press("#cmdText", "Enter"); await p.wait_for_timeout(600)
    idea = await p.evaluate("[...F.items.values()].some(i => i.type === 'idea' && i.title === 'venue list')")
    await p.fill("#cmdText", f"approve {cr}"); await p.press("#cmdText", "Enter"); await p.wait_for_timeout(500)
    still_asks = await p.evaluate(f"F.items.get('{cr}').status") == "proposed"
    log_private = not any((e.get("body") or {}).get("cmd") for e in st.events)
    ok = waiting and nodes == 1 and bool(cr) and idea and still_asks and log_private and not p.errors
    return ok, f"suggest waits={waiting}, node drawn={nodes}, confirmed → {cr}, act ran capture={idea}, approve still asks={still_asks}, log private={log_private}"


async def s35_history_words(b):
    """Opens an item's history. Every line must say what happened in words; no line may read 'undefined' (F-73)."""
    st = Store([ev("comment", "D-001", {"text": "looks right"}), ev("link", "D-001", {"refs": ["D-002"], "rel": "depends_on"}, ts="2026-11-01T15:01:00.000Z")])
    p = await open_page(b, st, {})
    await p.evaluate("select('D-001')"); await p.wait_for_timeout(300)
    txt = await p.locator("#inspector").inner_text()
    ok = "undefined" not in txt and "recorded from the chats" in txt and "(depends on)" in txt and not p.errors
    return ok, f"undefined shown={'undefined' in txt}, genesis line={'recorded from the chats' in txt}, typed link line={'(depends on)' in txt}"


async def s40_point_mode(b):
    """Points at a part of Studio and leaves a note. One idea item is written, saying where (named region, element,
    view); clicking in point mode never triggers the thing under it; Esc leaves point mode (U1, pointer.v1)."""
    st = Store(); p = await open_page(b, st, {})
    await p.click("#pointBtn"); await p.wait_for_timeout(150)
    await p.click('#tabs button[data-v="list"]'); await p.wait_for_timeout(200)
    view_after_click = await p.evaluate("S.view")
    if not await exists(p, "#pointNote textarea"):
        return False, "no note box after clicking in point mode"
    await p.fill("#pointNote textarea", "Make this tab bigger"); await p.press("#pointNote textarea", "Enter"); await p.wait_for_timeout(300)
    made = [e for e in st.events if e.get("kind") == "item.create" and (e.get("body") or {}).get("source") == "point"]
    pt = (made[0]["body"].get("point") if made else {}) or {}
    await p.wait_for_selector("#pointNote", state="detached", timeout=3000)  # the save finishes before Esc (CI run 1 was flaky)
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    off = await p.evaluate("document.querySelector('#pointBtn').getAttribute('aria-pressed')")
    ok = (len(made) == 1 and pt.get("part") == "studio.tabs" and pt.get("schema") == "pointer.v1" and view_after_click == "map"
          and off == "false" and not p.errors)
    return ok, f"items={len(made)}, part={pt.get('part')}, view after click={view_after_click}, pressed after Esc={off}, errors={p.errors[:1]}"


VENDOR = HERE.parent / "vendor"


async def open_canvas_page(browser, store: Store, cfg: dict):
    """Like open_page, but over http so the page can load the canvas bundle the hub serves (/studio/vendor/...)."""
    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    async def serve(route):
        path = route.request.url.split("studio.test", 1)[1].split("?")[0]
        if path == "/page.html":
            return await route.fulfill(body=WRAPPED.read_text(), content_type="text/html")
        f = VENDOR / path.rsplit("/", 1)[-1]
        if path.startswith("/studio/vendor/") and f.exists():
            return await route.fulfill(body=f.read_bytes(), content_type="text/javascript" if f.suffix == ".js" else "text/css")
        return await route.fulfill(status=404, body="")
    await ctx.route("http://studio.test/**", serve)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    full = {"genesis": GENESIS, "me": "u_nowed", "owner": True, "canEdit": True, "canWrite": True,
            "names": {"u_nowed": "Nowed"}, **cfg, "paths": store.paths}
    await page.expose_function("__bridgeAdd", store.add)
    await page.add_init_script("window.__cfg = " + json.dumps(full) + ";" + MOCK)
    await page.goto("http://studio.test/page.html")
    await page.wait_for_selector(".react-flow__node-item", timeout=20000)
    await page.wait_for_timeout(800)
    page.me = full["me"]; store.pages.append(page); page.errors = errors
    return page


async def s36_canvas(b):
    """D-172 C1: the map is a canvas. Dragging saves a position; dragging from one item's dot to another asks the link type and saves it; a group can be added; positions survive a reload."""
    st = Store([]); p = await open_canvas_page(b, st, {})
    nodes = await p.locator(".react-flow__node-item").count()
    n = p.locator('.react-flow__node[data-id="D-001"]')
    for _ in range(8):
        bb = await n.bounding_box(); await p.mouse.move(bb["x"] + 5, bb["y"] + 5); await p.mouse.wheel(0, -300); await p.wait_for_timeout(120)
    await p.wait_for_timeout(300)
    bb = await n.bounding_box()
    await p.mouse.move(bb["x"] + 60, bb["y"] + 15); await p.mouse.down(); await p.mouse.move(bb["x"] + 160, bb["y"] + 115, steps=10); await p.mouse.up()
    await p.wait_for_timeout(600)
    moved = [e for e in st.events if e["kind"] == "canvas.layout" and "D-001" in e["body"].get("moves", {})]
    sb = await p.locator('.react-flow__node[data-id="D-001"] .react-flow__handle.source').bounding_box()
    db = await p.locator('.react-flow__node[data-id="D-002"] .react-flow__handle.target').bounding_box()
    await p.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2); await p.mouse.down()
    await p.mouse.move(db["x"] + db["width"] / 2, db["y"] + db["height"] / 2, steps=12); await p.mouse.up()
    await p.wait_for_selector(".nd-pick", timeout=4000)
    await p.get_by_role("button", name="depends on").click(); await p.wait_for_timeout(600)
    linked = [e for e in st.events if e["kind"] == "link" and e["item"] == "D-001" and e["body"].get("rel") == "depends_on"]
    label = await p.locator(".react-flow__edge-text").all_text_contents()
    await p.get_by_role("button", name="+ Group").click(); await p.wait_for_timeout(600)
    grouped = [e for e in st.events if e["kind"] == "canvas.frame"]
    q = await open_canvas_page(b, st, {})
    style = await q.locator('.react-flow__node[data-id="D-001"]').get_attribute("style") or ""
    x, y = moved[-1]["body"]["moves"]["D-001"] if moved else (None, None)
    kept = moved and f"translate({x}px, {y}px)" in style
    clean_items = all(e["item"] is None for e in st.events if e["kind"].startswith("canvas."))
    ok = nodes > 100 and bool(moved) and bool(linked) and "depends on" in label and len(grouped) == 1 and kept and clean_items and not p.errors and not q.errors
    return ok, f"nodes={nodes}, drag saved={bool(moved)}, typed link saved={bool(linked)}, label={label[:1]}, group={len(grouped)}, position kept after reload={bool(kept)}, canvas events keep items clean={clean_items}, errors={(p.errors + q.errors)[:1]}"


async def s37_classic_map_fallback(b):
    """Where the canvas bundle can't load (e.g. inside claude.ai), the classic map still shows everything."""
    st = Store([]); p = await open_page(b, st, {})
    await p.wait_for_timeout(600)
    canvas = await p.evaluate("!!window.NodachCanvas")
    classic = await p.evaluate("!!(window.cy || (typeof cy !== 'undefined' && cy)) && !document.getElementById('cy').hidden")
    return (not canvas) and classic and not p.errors, f"canvas loaded={canvas}, classic map shown={classic}"


async def s38_runnable_steps(b):
    """D-174 C2: adds the Shift Scout template (5 steps, 4 wires), edits a filter in plain lines (saved as data), wires a new step, and a bad line is refused, not guessed."""
    st = Store([]); p = await open_canvas_page(b, st, {})
    await p.get_by_role("button", name="+ Step").click()
    await p.get_by_role("button", name="Shift Scout (5 steps)").click()
    for _ in range(100):   # the template writes 9 events one after another; slow machines need longer
        if sum(e["kind"] == "flow.wire" for e in st.events) >= 4: break
        await p.wait_for_timeout(150)
    await p.wait_for_timeout(500)
    nodes = [e for e in st.events if e["kind"] == "flow.node"]
    wires = [e for e in st.events if e["kind"] == "flow.wire"]
    shown = await p.locator(".react-flow__node-step").count()
    filt = next(e["body"]["id"] for e in nodes if e["body"].get("step") == "filter")
    await p.evaluate(f"selectStep('{filt}')")
    await p.wait_for_selector("#stConds", timeout=10000)
    await p.fill("#stConds", "minimumSalary >= 12\nnot jobTitle contains driver"); await p.click("#stSave"); await p.wait_for_timeout(500)
    saved = [e for e in st.events if e["kind"] == "flow.node" and e["body"].get("id") == filt and "config" in e["body"]][-1]["body"]["config"]
    good = saved == {"all": [{"field": "minimumSalary", "op": ">=", "value": 12}], "none": [{"field": "jobTitle", "op": "contains", "value": "driver"}]}
    before = len(st.events)
    await p.fill("#stConds", "pay is more than twelve"); await p.click("#stSave"); await p.wait_for_timeout(400)
    refused = len(st.events) == before
    run_disabled = await p.locator("#stRun").is_disabled()   # no hub here, so Run can't be pressed
    clean = all(e["item"] is None for e in st.events if e["kind"].startswith("flow."))
    await p.click("#stClose")   # a step's panel closes like an item's does
    closed = await p.evaluate("document.querySelector('#inspector').classList.contains('closed')")
    ok = len(nodes) == 5 and len(wires) == 4 and shown == 5 and good and refused and run_disabled and clean and closed and not p.errors
    return ok, f"steps={len(nodes)}, wires={len(wires)}, drawn={shown}, filter saved as data={good}, bad line refused={refused}, run off without hub={run_disabled}, items clean={clean}, panel closes={closed}, errors={p.errors[:1]}"


async def s39_scheduled_steps(b):
    """S3: the start step of Shift Scout gets UK hours (saved as numbers, shown on the canvas), a wired-into step offers no schedule, a bad hour is refused, and the post step only posts new jobs by default."""
    st = Store([]); p = await open_canvas_page(b, st, {})
    await p.get_by_role("button", name="+ Step").click()
    await p.get_by_role("button", name="Shift Scout (5 steps)").click()
    for _ in range(100):
        if sum(e["kind"] == "flow.wire" for e in st.events) >= 4: break
        await p.wait_for_timeout(150)
    for _ in range(100):   # wait until the canvas has drawn all five steps (slow CI browsers)
        if await p.locator(".react-flow__node-step").count() >= 5: break
        await p.wait_for_timeout(150)
    await p.wait_for_timeout(500)

    async def pick(step_id, sel):   # select a step and wait for its panel; retried, as a late redraw can replace it
        for _ in range(4):
            await p.evaluate(f"selectStep('{step_id}')")
            try:
                await p.wait_for_selector(f"#inspector .step-form", timeout=5000)
                if sel is None or await p.locator(sel).count(): return True
            except Exception:
                pass
            await p.wait_for_timeout(500)
        return False
    nodes = [e for e in st.events if e["kind"] == "flow.node"]
    fetch = next(e["body"]["id"] for e in nodes if e["body"].get("step") == "fetch")
    filt = next(e["body"]["id"] for e in nodes if e["body"].get("step") == "filter")
    post = next(e["body"] for e in nodes if e["body"].get("step") == "post")
    only_new = post["config"].get("once") == "{{jobId}}"
    await pick(filt, "#stConds")
    no_sched_mid = await p.locator("#stSched").count() == 0
    if not await pick(fetch, "#stSched"):
        panel = (await p.locator('#inspector').inner_text())[:120]
        return False, f"fetch panel had no schedule field; panel={panel!r}"
    await p.fill("#stSched", "18, 7"); await p.click("#stSave"); await p.wait_for_timeout(500)
    saved = [e for e in st.events if e["kind"] == "flow.node" and e["body"].get("id") == fetch and "config" in e["body"]][-1]["body"]["config"]
    hours = saved.get("schedule") == [7, 18] and saved.get("url", "").startswith("https://www.reed.co.uk")
    await p.wait_for_timeout(400)
    badge = await p.locator(".nd-sched", has_text="7, 18").count() == 1
    before = len(st.events)
    await p.fill("#stSched", "7, 25"); await p.click("#stSave"); await p.wait_for_timeout(400)
    refused = len(st.events) == before
    ok = only_new and no_sched_mid and hours and badge and refused and not p.errors
    return ok, f"post only-new by default={only_new}, no schedule on wired-into step={no_sched_mid}, hours saved={hours}, badge shown={badge}, bad hour refused={refused}, errors={p.errors[:1]}"


async def s41_my_queue(b):
    """Back after a day away, opens My queue. Only what waits on him shows, oldest first: a decision pack, an unanswered
    question, a re-asked one and a finding in his lane. Answered items and Claude-lane items stay out; nothing is written (AS3)."""
    t = lambda h: f"2026-11-19T{h}:00:00.000Z"
    st = Store([
        ev("item.create", None, {"id": "Q-PACK-terms", "type": "decision-pack", "title": "PK1 pilot terms", "body": "a, b, c or d", "status": "draft-v1"}, ts=t("10")),
        ev("item.create", None, {"id": "Q-open1", "type": "question", "title": "Open one", "status": "open"}, ts=t("11")),
        ev("item.create", None, {"id": "Q-done1", "type": "question", "title": "Answered one", "status": "open"}, ts=t("09")),
        ev("answer", "Q-done1", {"code": "Q-done1", "text": "b"}, ts=t("12")),
        ev("item.create", None, {"id": "Q-reask", "type": "question", "title": "Re-asked one", "status": "open"}, ts=t("08")),
        ev("answer", "Q-reask", {"code": "Q-reask", "text": "a"}, ts=t("09")),
        ev("item.edit", "Q-reask", {"body": "re-posed with new options"}, ts=t("13")),
        ev("item.create", None, {"id": "F-mine", "type": "finding", "title": "Lane nowed finding", "status": "open", "lane": "nowed"}, ts=t("07")),
        ev("item.create", None, {"id": "Q-claude", "type": "question", "title": "Claude lane question", "status": "open", "lane": "claude"}, ts=t("07")),
        ev("item.create", None, {"id": "Q-settled", "type": "question", "title": "Settled one", "status": "done"}, ts=t("07")),
    ])
    p = await open_page(b, st, {})
    before = len(st.events)
    await p.click('#tabs button[data-v="queue"]'); await p.wait_for_timeout(300)
    ids = await p.evaluate("[...document.querySelectorAll('#queueList [data-q]')].map(x => x.dataset.q)")
    mine = [i for i in ids if i in ("F-mine", "Q-PACK-terms", "Q-open1", "Q-reask", "Q-done1", "Q-claude", "Q-settled")]
    want = ["F-mine", "Q-PACK-terms", "Q-open1", "Q-reask"]
    badge = await p.inner_text("#queueBadge")
    await p.click('#queueList [data-qopen="Q-PACK-terms"]'); await p.wait_for_timeout(200)
    opened = await p.evaluate("S.sel")
    ok = mine == want and badge.isdigit() and int(badge) == len(ids) and opened == "Q-PACK-terms" and len(st.events) == before and not p.errors
    return ok, f"queue={mine} (want {want}), badge={badge}/{len(ids)}, opened={opened}, writes={len(st.events)-before}, errors={p.errors[:1]}"



async def open_served_studio(browser, me):
    """Studio as the hub serves it: the real host shim (standalone.js) over a stand-in hub with sign-in and sign-out.
    The stand-in keeps a session cookie; /studio/logout clears it, and /studio/api/* then answers 401."""
    import urllib.parse
    shim = (HERE.parent / "standalone.js").read_text()
    H = {"signed_in": True, "visits": []}
    html = ('<!doctype html><html lang=en><head><meta charset=utf-8><style>body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style>'
            '<script>window.__nodachBuild="test";window.__nodachEnv="production";window.__nodachPollMs=60000;</script>'
            f'<script>{shim}</script></head><body>{PAGE.read_text()}</body></html>')

    async def serve(route):
        req = route.request; path = urllib.parse.urlparse(req.url).path
        H["visits"].append(path)
        js = lambda v, status=200: route.fulfill(status=status, body=json.dumps(v), content_type="application/json")
        if path == "/studio/logout":
            H["signed_in"] = False
            return await route.fulfill(body="<!doctype html><p>Signed out</p>", content_type="text/html",
                                       headers={"set-cookie": "nodach_session=; Path=/; Max-Age=0"})
        if path == "/studio/login":
            return await route.fulfill(body="<!doctype html><title>Sign in</title><p id=login>Sign in</p>", content_type="text/html")
        if path == "/health":
            return await route.fulfill(body="ok test", content_type="text/plain")
        if path in ("/studio", "/studio/"):
            return await route.fulfill(body=html, content_type="text/html") if H["signed_in"] else await route.fulfill(status=302, headers={"location": "/studio/login"})
        if path.startswith("/studio/api/") and not H["signed_in"]:
            return await js({"error": "sign in"}, 401)
        if path == "/studio/api/me":
            return await js(me)
        if path == "/studio/api/genesis":
            return await js(GENESIS)
        if path == "/studio/api/events":
            return await js({"events": [], "next": 0, "total": 0}) if req.method == "GET" else await js({"imported": 1})
        if path == "/studio/api/private":
            return await js({"error": "no private store"}, 503)
        return await route.fulfill(status=404, body="")
    ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
    await ctx.route("http://hub.test/**", serve)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto("http://hub.test/studio")
    await page.wait_for_timeout(1200)
    page.errors = errors; page.hub = H
    return page


async def s54_sign_out_switch_account(b):
    """Signed in through a linked email, wants to switch to another account: the header shows who is signed in (login
    and role, never an address) next to a Sign out / switch account link, which clears the session at /studio/logout
    and lands on the sign-in page. Inside claude.ai (no hub) the link isn't shown."""
    p = await open_served_studio(b, {"login": "Morami49", "id": "github:Morami49", "owner": True})
    acct = await p.inner_text("#acct") if await p.locator("#acct:visible").count() else ""
    shown = await p.locator("#signOut:visible").count() == 1
    await p.click("#signOut"); await p.wait_for_url("**/studio/login", timeout=5000)
    landed = p.url.endswith("/studio/login"); out = not p.hub["signed_in"]
    order = [v for v in p.hub["visits"] if v in ("/studio/logout", "/studio/login")]
    m = await open_served_studio(b, {"login": "email:0123456789abcdef", "id": "email:0123456789abcdef", "owner": False})
    macct = await m.inner_text("#acct")
    st = Store(); c = await open_page(b, st, {})
    hidden_in_claude = await c.locator("#signOut:visible").count() == 0
    ok = (acct == "Morami49 · owner" and shown and landed and out and order[:2] == ["/studio/logout", "/studio/login"]
          and macct == "email account · member" and hidden_in_claude and not p.errors and not m.errors)
    return ok, f"account={acct!r}, link shown={shown}, signed out={out}, went {order[:2]}, member shows {macct!r}, hidden in claude.ai={hidden_in_claude}, errors={(p.errors + m.errors)[:1]}"

SCENARIOS = [s01_tired_misclick, s02_undo, s03_stable_codes, s04_past_view_lock, s05_branch_awareness, s06_view_only_client,
             s07_names, s08_long_history, s09_ai_relay, s10_keyboard_list, s11_offline_capture, s12_member_rights,
             s13_two_people_at_once, s14_phone, s15_undo_old_status, s16_overwhelm, s17_disagreement, s18_clock_skew, s19_private_area,
             s20_addresses, s21_money_settings, s22_duplicate_id, s23_same_order_everywhere,
             s24_hub_sync, s25_more_than_1000, s26_hub_not_connected, s27_read_on_open, s28_push_after_clean_runs, s29_runs_timeline, s30_builder_pipeline, s31_content_cycles, s32_private_files_and_views, s33_studio_edits_studio, s34_command_map, s35_history_words, s36_canvas, s37_classic_map_fallback, s38_runnable_steps, s39_scheduled_steps, s40_point_mode, s41_my_queue,
             s54_sign_out_switch_account]


async def main():
    results = []
    async with async_playwright() as pw:
        # STUDIO_CHROME: use an installed Chrome instead of Playwright's (e.g. where its download is blocked)
        b = await pw.chromium.launch(executable_path=os.environ["STUDIO_CHROME"]) if os.environ.get("STUDIO_CHROME") else await pw.chromium.launch()
        for sc in SCENARIOS:
            try:
                ok, note = await sc(b)
            except Exception as e:
                ok, note = False, f"crashed: {type(e).__name__}: {str(e)[:140]}"
            results.append({"id": sc.__name__.split("_")[0], "who": sc.__doc__.strip().split(".")[0], "pass": bool(ok), "note": note})
            print(("PASS " if ok else "FAIL ") + sc.__name__ + " — " + note)
        await b.close()
    (OUT / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} passed")
    if not all(r["pass"] for r in results):
        sys.exit(1)   # a failing scenario must fail CI (D-061)


if __name__ == "__main__":
    asyncio.run(main())
