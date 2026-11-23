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
if (C.fakeSpeech) {   // s47: a stand-in for the browser's speech recognition; records every start, speaks on demand
  window.__speech = {starts:0, last:null};
  window.SpeechRecognition = class { constructor(){ window.__speech.last = this; }
    start(){ window.__speech.starts++; } stop(){ this.onend && this.onend(); }
    say(t){ this.onresult && this.onresult({resultIndex:0, results:[Object.assign([{transcript:t}], {isFinal:true})]}); } };
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


def nav_init(classic=True, env=None):
    """NAV-2: the scenarios written before the new bar drive Studio through Classic tabs (Settings → Classic tabs), which
    must keep working until each move is verified; s55–s58 drive the new bar itself (cfg classic=False). env="staging"
    makes the page behave as the staging hub serves it (NVS-4: only there do Developer-mode saves apply)."""
    # only on a context's first load, so a scenario can reload and see the choice kept
    act = 'localStorage.setItem("studio.classicTabs","1")' if classic else 'localStorage.removeItem("studio.classicTabs")'
    js = 'try{if(!sessionStorage.getItem("navInit")){sessionStorage.setItem("navInit","1");' + act + '}}catch(e){}'
    return js + (f'window.__nodachEnv={json.dumps(env)};' if env else '')


async def open_page(browser, store: Store, cfg: dict, width=1440, height=900, name="p", touch=False, reduced=False):
    ctx = await browser.new_context(viewport={"width": width, "height": height}, has_touch=touch, reduced_motion="reduce" if reduced else "no-preference")
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    full = {"genesis": GENESIS, "me": "u_nowed", "owner": True, "canEdit": True, "canWrite": True,
            "names": {"u_nowed": "Nowed", "u_rad": "Rad"}, **cfg, "paths": store.paths}
    await page.expose_function("__bridgeAdd", store.add)
    await page.add_init_script(nav_init(full.get("classic", True), full.get("env")))
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
    """D-152/153: on staging, Nowed adds an item type and changes the look from Developer mode; both take effect at once and are versions; a change request can be proposed and approved. (Away from staging these saves are proposals: s59.)"""
    st = Store([]); p = await open_page(b, st, {"env": "staging"})
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
    q = await open_page(b, st, {"env": "staging"}); fresh_accent = await q.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
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


async def s41_multi_point(b):
    """Shift+clicks two parts of Studio, then Enter, and leaves one note on both. One idea item is written whose point
    has targets in click order (pointer.v1 each); numbered badges show while picking and go after saving; nothing underneath fires."""
    st = Store(); p = await open_page(b, st, {})
    await p.click("#pointBtn"); await p.wait_for_timeout(150)
    await p.click('#tabs button[data-v="list"]', modifiers=["Shift"]); await p.wait_for_timeout(100)
    await p.click('#tabs button[data-v="tree"]', modifiers=["Shift"]); await p.wait_for_timeout(100)
    badges = await p.locator(".ptBadge").all_inner_texts()
    await p.keyboard.press("Enter"); await p.wait_for_timeout(150)
    if not await exists(p, "#pointNote textarea"):
        return False, f"no note box after Enter, badges={badges}"
    await p.fill("#pointNote textarea", "Group these two"); await p.press("#pointNote textarea", "Enter"); await p.wait_for_timeout(300)
    made = [e for e in st.events if e.get("kind") == "item.create" and (e.get("body") or {}).get("source") == "point"]
    pt = (made[0]["body"].get("point") if made else {}) or {}
    tg = pt.get("targets") or []
    left = await p.locator(".ptBadge").count()
    view = await p.evaluate("S.view")
    body = made[0]["body"]["body"] if made else ""
    ok = (len(made) == 1 and badges == ["1", "2"] and len(tg) == 2 and all(t.get("schema") == "pointer.v1" and t.get("part") == "studio.tabs" for t in tg)
          and "data-v=list" in tg[0].get("element", "") and "data-v=tree" in tg[1].get("element", "") and "\n1. studio.tabs" in body and "\n2. studio.tabs" in body
          and left == 0 and view == "map" and not p.errors)
    return ok, f"items={len(made)}, badges={badges}, targets={[t.get('element') for t in tg]}, badges left={left}, view={view}, errors={p.errors[:1]}"


async def until(cond, timeout=8.0):
    """Wait for a store condition instead of a fixed sleep (CI runners are slower than a laptop)."""
    t = 0.0
    while not cond() and t < timeout:
        await asyncio.sleep(0.1); t += 0.1
    await asyncio.sleep(0.2)


def now_ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def stroke(p, x, y, pts=((60, 0), (60, 50), (0, 50), (0, 0))):
    await p.mouse.move(x, y); await p.mouse.down()
    for dx, dy in pts:
        await p.mouse.move(x + dx, y + dy, steps=3); await p.wait_for_timeout(45)
    await p.mouse.up(); await p.wait_for_timeout(150)


async def s42_show_record_save(b):
    """Records a show for one watcher: two clicks, a drawn stroke and a note. The item is live from the start, the watcher
    is granted it, each step is a live comment the watcher sees with Claude's reply in between, nothing underneath fires,
    and Stop saves one item with ordered show.v1 steps, addressable lines, and status open."""
    st = Store([ev("comment", "Q-layers", {"text": "hello"}, who="github:watcher1", label="Watcher")])
    p = await open_page(b, st, {})
    await p.click("#showBtn"); await p.wait_for_timeout(200)
    has_watcher = await exists(p, '#showBar [data-login="watcher1"]')
    if has_watcher:
        await p.click("#showAud summary"); await p.check('#showBar [data-login="watcher1"]'); await p.click("#showAud summary")
    await p.click("#showGo"); await p.wait_for_timeout(400)
    created = [e for e in st.events if e["kind"] == "item.create" and e["body"].get("source") == "show"]
    if not created:
        return False, "no live item created on start"
    sid = created[0]["body"]["id"]
    await p.click('#tabs button[data-v="list"]'); await p.wait_for_timeout(120)
    await p.click('#tabs button[data-v="ask"]'); await p.wait_for_timeout(300)
    view_mid = await p.evaluate("S.view")
    # a watcher opens the live item; Claude replies through the hub
    w = await open_page(b, st, {"me": "github:watcher1", "owner": False})
    await st.add("events", ev("comment", sid, {"text": "I see the tabs"}, who="claude", label="Claude", ts=now_ts()))
    await w.evaluate(f"select('{sid}')"); await w.wait_for_timeout(300)
    w_live = await exists(w, "#showLive"); w_steps = await w.locator(".showlog li.step").count(); w_reply = await w.locator(".showlog li.reply").count()
    await stroke(p, 700, 450)
    await p.keyboard.press("n"); await p.wait_for_timeout(120)
    await p.fill("#showNoteBox textarea", "Look here"); await p.press("#showNoteBox textarea", "Enter"); await p.wait_for_timeout(300)
    await w.wait_for_timeout(200); w_steps2 = await w.locator(".showlog li.step").count()
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    await p.fill("#showTitle", "Tabs walkthrough"); await p.click("#showSave")
    await until(lambda: any(e["kind"] == "status" and e["item"] == sid for e in st.events))   # CI run 1: a slow runner needed >700 ms
    live = [e for e in st.events if e["kind"] == "comment" and e["item"] == sid and (e["body"] or {}).get("step")]
    grants = [e for e in st.events if e["kind"] == "access.grant"]
    edits = [e for e in st.events if e["kind"] == "item.edit" and e["item"] == sid]
    status = [e["body"]["status"] for e in st.events if e["kind"] == "status" and e["item"] == sid]
    sh = (edits[-1]["body"].get("show") if edits else {}) or {}
    steps = sh.get("steps") or []
    kinds = [s["kind"] for s in steps]; ns = [s["n"] for s in steps]
    body = edits[-1]["body"].get("body", "") if edits else ""
    await p.evaluate(f"openAddress('{sid}#3')"); await p.wait_for_timeout(200)
    line3 = await p.evaluate("document.getElementById('L-3')?.textContent || ''")
    size = len(json.dumps(sh))
    ok = (has_watcher and created[0]["body"]["status"] == "live" and len(created) == 1
          and grants and grants[0]["body"] == {"login": "watcher1", "items": [sid]}
          and view_mid == "map" and kinds == ["point", "point", "stroke", "note"] and ns == [1, 2, 3, 4]
          and sh.get("schema") == "show.v1" and len(steps[2].get("path") or []) >= 3 and steps[3].get("text") == "Look here"
          and steps[0]["target"]["part"] == "studio.tabs" and isinstance(sh.get("cursor"), list) and sh.get("audience") == ["watcher1"]
          and len(live) == 4 and status == ["open"] and "\n3. Drew around" in body and "\n4. Note: Look here" in body
          and line3.startswith("3. Drew around") and size < 200000
          and w_live and w_steps == 2 and w_reply == 1 and w_steps2 == 4 and not p.errors and not w.errors)
    return ok, (f"watcher listed={has_watcher}, grants={[g['body'] for g in grants]}, view after clicks={view_mid}, kinds={kinds}, n={ns}, live comments={len(live)}, "
                f"status={status}, line 3={line3[:40]!r}, watcher saw live={w_live} steps={w_steps}->{w_steps2} replies={w_reply}, bytes={size}, errors={(p.errors + w.errors)[:1]}")


async def s43_show_pause(b):
    """Pauses mid-show. What he does while paused is not written live and is left out of the saved show unless he ticks
    include paused steps (default off); numbering of the shared steps stays 1, 2."""
    st = Store(); p = await open_page(b, st, {})
    await p.click("#showBtn"); await p.wait_for_timeout(150)
    await p.click("#showGo"); await p.wait_for_timeout(300)
    sid = next(e["body"]["id"] for e in st.events if e["kind"] == "item.create")
    await p.click('#tabs button[data-v="list"]'); await p.wait_for_timeout(150)
    await p.click("#showPause"); await p.wait_for_timeout(100)
    await p.click('#tabs button[data-v="tree"]'); await p.wait_for_timeout(150)
    paused_live = len([e for e in st.events if e["kind"] == "comment" and e["item"] == sid])
    await p.click("#showPause"); await p.wait_for_timeout(100)
    await p.click('#tabs button[data-v="ask"]'); await p.wait_for_timeout(300)
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    box = await exists(p, "#showInclPaused"); ticked = await p.is_checked("#showInclPaused") if box else None
    await p.click("#showSave")
    await until(lambda: any(e["kind"] == "status" and e["item"] == sid for e in st.events))
    live = [e["body"]["step"] for e in st.events if e["kind"] == "comment" and e["item"] == sid and e["body"].get("step")]
    edits = [e for e in st.events if e["kind"] == "item.edit" and e["item"] == sid]
    steps = ((edits[-1]["body"].get("show") if edits else {}) or {}).get("steps") or []
    els = [s.get("target", {}).get("element", "") for s in steps]
    ok = (paused_live == 1 and box and ticked is False and len(live) == 2 and [s["n"] for s in live] == [1, 2]
          and [s["n"] for s in steps] == [1, 2] and not any("tree" in x for x in els) and not any(s.get("paused") for s in steps) and not p.errors)
    return ok, f"live comments after paused click={paused_live}, include box={box} ticked={ticked}, live n={[s['n'] for s in live]}, saved={els}, errors={p.errors[:1]}"


async def s44_show_replay(b):
    """Opens a saved show and presses Replay: Studio switches to the recorded view and plays the cursor, stroke and numbered
    badges over it; Esc closes the replay and returns to where he was."""
    tgt = {"schema": "pointer.v1", "kind": "pointer.studio", "part": "list.items", "element": "button", "text": "x", "view": "list",
           "rect": {"left": 200, "top": 120, "right": 300, "bottom": 150}, "viewport": {"w": 1200, "h": 800}}
    show = {"schema": "show.v1", "started": "2026-11-19T10:00:00.000Z", "duration_ms": 1200, "view": "list", "viewport": {"w": 1200, "h": 800},
            "audience": [], "steps": [{"n": 1, "t_ms": 50, "kind": "point", "target": tgt},
                                      {"n": 2, "t_ms": 200, "kind": "stroke", "target": tgt, "path": [[100, 100], [200, 160], [300, 100]], "dur_ms": 300}],
            "cursor": [[0, 10, 10], [300, 150, 120], [900, 300, 100]]}
    st = Store([ev("item.create", None, {"id": "SH-test", "type": "idea", "title": "A show", "body": "1. Pointed at x\n2. Drew around x",
                                          "status": "open", "source": "show", "show": show})])
    p = await open_page(b, st, {})
    await p.evaluate("select('SH-test')"); await p.wait_for_timeout(200)
    if not await exists(p, "#showReplayBtn"):
        return False, "no Replay button on a show item"
    await p.click("#showReplayBtn"); await p.wait_for_timeout(700)
    on = await exists(p, "#showReplay"); view = await p.evaluate("S.view")
    badges = await p.locator("#showReplay .ptBadge").count()
    pts = await p.evaluate("document.querySelector('#showReplay polyline')?.getAttribute('points') || ''")
    await p.keyboard.press("Escape"); await p.wait_for_timeout(200)
    gone = not await exists(p, "#showReplay"); back = await p.evaluate("S.view")
    ok = on and view == "list" and badges == 2 and pts.count(",") == 3 and gone and back == "map" and not p.errors
    return ok, f"overlay={on}, view={view}, badges={badges}, stroke points={pts!r}, closed by Esc={gone}, back to={back}, errors={p.errors[:1]}"


FAKE_CLOCK = "window.__fake = 0; window.__clockNow = () => window.__fake;"


async def s49_show_pause_clock(b):
    """F-show-pause-clock (20 Nov, staging): pauses a show for five seconds on a fake clock. The timer freezes while paused,
    and paused time is left out of every step's t_ms, the cursor trail and the saved duration; the save box holds it too."""
    st = Store(); p = await open_page(b, st, {})
    await p.evaluate(FAKE_CLOCK)
    await p.click("#showBtn"); await p.wait_for_timeout(150)
    await p.click("#showGo"); await p.wait_for_timeout(300)
    sid = next(e["body"]["id"] for e in st.events if e["kind"] == "item.create")
    await p.evaluate("window.__fake = 1000"); await p.mouse.move(400, 300); await p.wait_for_timeout(200)
    await p.click('#tabs button[data-v="list"]'); await p.wait_for_timeout(150)
    await p.click("#showPause"); await p.wait_for_timeout(100)
    await p.evaluate("window.__fake = 6000"); await p.mouse.move(420, 320); await p.wait_for_timeout(400)
    timer_paused = await p.inner_text("#showTime"); el_paused = (await p.evaluate("NodachShow.state()"))["elapsed_ms"]
    await p.click("#showPause"); await p.wait_for_timeout(100)
    await p.evaluate("window.__fake = 6500"); await p.mouse.move(440, 340); await p.wait_for_timeout(250)
    await p.click('#tabs button[data-v="ask"]'); await p.wait_for_timeout(200)
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    await p.evaluate("window.__fake = 30000"); await p.wait_for_timeout(300)
    timer_stop = await p.inner_text("#showTime")
    await p.click("#showSave")
    await until(lambda: any(e["kind"] == "status" and e["item"] == sid for e in st.events))
    edits = [e for e in st.events if e["kind"] == "item.edit" and e["item"] == sid]
    sh = (edits[-1]["body"].get("show") if edits else {}) or {}
    t_ms = [s["t_ms"] for s in sh.get("steps") or []]
    live = [e["body"]["step"]["t_ms"] for e in st.events if e["kind"] == "comment" and e["item"] == sid and e["body"].get("step")]
    curs = [c[0] for c in sh.get("cursor") or []]
    await p.evaluate("delete window.__clockNow")
    ok = (timer_paused == "0:01" and el_paused == 1000 and t_ms == [1000, 1500] and live == [1000, 1500] and sh.get("duration_ms") == 1500
          and timer_stop == "0:01" and curs and max(curs) <= 1500 and not p.errors)
    return ok, f"timer while paused={timer_paused} (elapsed {el_paused}), step t_ms={t_ms}, live t_ms={live}, duration={sh.get('duration_ms')}, timer in save box={timer_stop}, cursor times={curs}, errors={p.errors[:1]}"


async def s50_show_states(b):
    """Show is an explicit state machine: open, cancel, open, start, pause, stop, keep (back to paused), resume, stop, discard.
    NodachShow.state() reads the state and an ordered trace; an event the state doesn't take is refused and traced."""
    st = Store(); p = await open_page(b, st, {})
    seq = []
    async def s(): seq.append((await p.evaluate("NodachShow.state().state")))
    await s()
    await p.click("#showBtn"); await p.wait_for_timeout(150); await s()
    await p.click("#showCancel"); await p.wait_for_timeout(100); await s()
    await p.click("#showBtn"); await p.wait_for_timeout(150)
    await p.click("#showGo"); await p.wait_for_timeout(300); await s()
    await p.click("#showPause"); await p.wait_for_timeout(100); await s()
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150); await s()
    await p.click("#showResume"); await p.wait_for_timeout(100); await s()
    rec_label = await p.inner_text("#showRec")
    await p.click("#showPause"); await p.wait_for_timeout(100); await s()
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    await p.click("#showDiscard"); await p.wait_for_timeout(400); await s()
    tr = await p.evaluate("NodachShow.state().trace")
    events = [(t["event"], t["to"]) for t in tr if t["ok"]]
    want = [("open", "ready"), ("cancel", "idle"), ("open", "ready"), ("start", "recording"), ("pause", "paused"), ("stop", "stopping"),
            ("keep", "paused"), ("resume", "recording"), ("stop", "stopping"), ("discard", "idle")]
    refused = await p.evaluate("(() => { const m = NodachFSM.machine('t', SHOW_TABLE, 'idle'); const a = m.send('pause'); return [a, m.state, m.trace()[0].ok]; })()")
    edges = await p.evaluate("NodachShow.state().edges.length")
    withdrawn = any(e["kind"] == "status" and e["body"].get("status") == "withdrawn" for e in st.events)
    ok = (seq == ["idle", "ready", "idle", "recording", "paused", "stopping", "paused", "recording", "idle"] and events == want
          and "paused" in rec_label and refused == [False, "idle", False] and edges >= 12 and withdrawn and not p.errors)
    return ok, f"states={seq}, trace={events}, label after keep={rec_label!r}, refused={refused}, edges={edges}, withdrawn={withdrawn}, errors={p.errors[:1]}"


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
    await page.add_init_script(nav_init(full.get("classic", True), full.get("env")))
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


async def s51_show_marks_follow_canvas(b):
    """F-show-replay-drift (20 Nov, staging): points at a node on the canvas and draws on another while showing, then drags
    the map and moves the first node. Live and in the replay, each badge stays on the node it marked (map units and
    node anchor kept), not at the old screen spot."""
    st = Store([]); p = await open_canvas_page(b, st, {})
    async def box(nid):
        return await p.locator(f'.react-flow__node[data-id="{nid}"]').bounding_box()
    def inside(pt, bb, pad=3):
        return pt and bb and bb["x"] - pad <= pt[0] <= bb["x"] + bb["width"] + pad and bb["y"] - pad <= pt[1] <= bb["y"] + bb["height"] + pad
    await p.click("#showBtn"); await p.wait_for_timeout(150)
    await p.click("#showGo"); await p.wait_for_timeout(300)
    sid = next(e["body"]["id"] for e in st.events if e["kind"] == "item.create")
    a = await box("D-001"); await p.mouse.click(a["x"] + 20, a["y"] + 10); await p.wait_for_timeout(150)
    c = await box("D-002"); await stroke(p, c["x"] + 15, c["y"] + 10, ((40, 0), (40, 25), (0, 25)))
    vp = await p.evaluate("NodachCanvas.geo.viewport()")
    await p.evaluate(f"NodachCanvas.geo.setViewport({{x:{vp['x'] + 180}, y:{vp['y'] + 110}, zoom:{vp['zoom']}}})"); await p.wait_for_timeout(250)
    live_badge = await p.evaluate("(() => { const b = document.querySelector('#showLayer .ptBadge'); return b ? [parseFloat(b.style.left) + 9, parseFloat(b.style.top) + 9] : null; })()")
    a_live = await box("D-001")
    await p.keyboard.press("Escape"); await p.wait_for_timeout(150)
    await p.fill("#showTitle", "Canvas marks"); await p.click("#showSave")
    await until(lambda: any(e["kind"] == "status" and e["item"] == sid for e in st.events))
    edits = [e for e in st.events if e["kind"] == "item.edit" and e["item"] == sid]
    steps = ((edits[-1]["body"].get("show") if edits else {}) or {}).get("steps") or []
    f1 = (steps[0].get("target") or {}).get("flow") if steps else None
    f2 = steps[1].get("fpath") if len(steps) > 1 else None
    # drag the map again and move D-001 somewhere else on the board
    vp = await p.evaluate("NodachCanvas.geo.viewport()")
    await p.evaluate(f"NodachCanvas.geo.setViewport({{x:{vp['x'] - 260}, y:{vp['y'] + 40}, zoom:{vp['zoom'] * 1.25}}})")
    n1 = await p.evaluate("NodachCanvas.geo.node('D-001')")
    await st.add("events", ev("canvas.layout", None, {"board": "main", "moves": {"D-001": [round(n1["x"]) + 300, round(n1["y"]) + 160]}}, ts=now_ts()))
    await p.wait_for_timeout(600)
    await p.evaluate(f"select('{sid}')"); await p.wait_for_timeout(200)
    await p.click("#showReplayBtn")
    # The replay plays in real time: a mark appears once the clock passes its step's t_ms. On a slow runner the stroke's
    # t_ms can be past a fixed 1.5 s wait (CI 21 Nov, 'replay 2 None'), so wait for the replay to reach its end instead.
    try:
        await p.wait_for_function("document.querySelector('#showReplay [data-play]')?.textContent.includes('Play again')", timeout=15000)
    except Exception:
        pass
    await p.wait_for_timeout(100)
    rm = {m["n"]: (m["x"], m["y"]) for m in await p.evaluate("NodachShow.replayMarks()")}
    a2, c2 = await box("D-001"), await box("D-002")
    ok = (f1 and f1.get("node") == "D-001" and f2 and steps[1].get("anchor", {}).get("node") == "D-002"
          and inside(live_badge, a_live) and inside(rm.get(1), a2) and inside(rm.get(2), c2) and not p.errors)
    return ok, (f"flow kept={bool(f1)} node={f1 and f1.get('node')}, stroke map path={bool(f2)} anchor={steps[1].get('anchor') if len(steps) > 1 else None}, "
                f"live badge {live_badge} on D-001 {a_live and (round(a_live['x']), round(a_live['y']))}={inside(live_badge, a_live)}, "
                f"replay 1 {rm.get(1)} on moved D-001={inside(rm.get(1), a2)}, replay 2 {rm.get(2)} on D-002={inside(rm.get(2), c2)}, errors={p.errors[:1]}")


async def s45_my_queue(b):
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


def lines_of(st, sid):
    return [e for e in st.events if e["kind"] == "live.line" and e["item"] == sid]


async def s46_live_session_stream(b):
    """Live session (Q-LIVE1): starts a session for one watcher, types a line, points at a tab and says the next line
    with that point, and says "task: ..." which becomes ONE draft change linked to the session (never auto-built). A
    line from the desktop client (voice, desktop point) and Claude's reply land in the same stream, in time order; the
    watcher sees it live; picking a point clicks nothing underneath; End keeps everything and sets the session open."""
    st = Store([ev("comment", "Q-layers", {"text": "hello"}, who="github:watcher1", label="Watcher")])
    p = await open_page(b, st, {})
    await p.click("#liveBtn"); await p.wait_for_timeout(200)
    await p.click("#liveAud summary"); await p.check('#livePanel [data-login="watcher1"]'); await p.click("#liveAud summary")
    await p.click("#liveStart")
    await until(lambda: any(e["kind"] == "item.create" and e["body"].get("source") == "live" for e in st.events))
    sid = next(e["body"]["id"] for e in st.events if e["kind"] == "item.create" and e["body"].get("source") == "live")
    await p.fill("#liveText", "Here is the list view"); await p.press("#liveText", "Enter")
    await p.click("#livePoint"); await p.click('#tabs button[data-v="ask"]'); await p.wait_for_timeout(150)
    view_after_pick = await p.evaluate("S.view")
    await p.fill("#liveText", "this tab needs a count"); await p.click("#liveSend")
    await p.fill("#liveText", "task: add a count badge to Ask"); await p.press("#liveText", "Enter")
    await until(lambda: any(e["kind"] == "item.create" and e["body"].get("type") == "change" for e in st.events))
    # the desktop client posts a voice line with a desktop point; Claude replies with a comment
    await st.add("events", {"_id": "lvdesk1", "ts": now_ts(), "kind": "live.line", "item": sid, "branch": "main",
                            "author": {"id": "u_nowed", "role": "owner", "label": "Nowed"},
                            "body": {"session": sid, "speaker": "Nowed", "text": "and this button in Excel", "ts": now_ts(), "via": "voice",
                                     "point": {"schema": "pointer.v1", "kind": "pointer.desktop", "id": "PE-261119-120000-abcd", "app": "EXCEL.EXE",
                                               "element": {"type": "ButtonControl", "name": "Sort"}}}})
    await st.add("events", ev("comment", sid, {"text": "On it: drafting the badge"}, who="claude", label="Claude", ts=now_ts()))
    await p.wait_for_timeout(300)
    rows = await p.evaluate("[...document.querySelectorAll('#liveStream li')].map(li => li.innerText.replace(/\\s+/g,' '))")
    w = await open_page(b, st, {"me": "github:watcher1", "owner": False})
    await w.evaluate(f"select('{sid}')"); await w.wait_for_timeout(300)
    w_lines = await w.locator("#inspLive li.line").count(); w_reply = await w.locator("#inspLive li.reply").count(); w_live = await exists(w, "#liveBadge")
    lines = lines_of(st, sid)
    pointed = [e for e in lines if e["body"].get("point")]
    drafts = [e for e in st.events if e["kind"] == "item.create" and e["body"].get("type") == "change"]
    task_line = next((e for e in lines if e["body"]["text"].startswith("task:")), None)
    grants = [e["body"] for e in st.events if e["kind"] == "access.grant"]
    await p.click("#liveEnd")
    await until(lambda: any(e["kind"] == "status" and e["item"] == sid for e in st.events))
    status = [e["body"]["status"] for e in st.events if e["kind"] == "status" and e["item"] == sid]
    ended = any(e["kind"] == "item.edit" and e["item"] == sid and (e["body"].get("live") or {}).get("ended") for e in st.events)
    d = drafts[0]["body"] if drafts else {}
    ok = (len(lines) == 4 and lines[0]["body"]["session"] == sid and lines[0]["body"]["speaker"] == "Nowed" and lines[0]["body"]["via"] == "typed"
          and len(pointed) == 2 and pointed[0]["body"]["point"]["part"] == "studio.tabs" and view_after_pick == "map"
          and len(drafts) == 1 and d.get("status") == "draft" and d.get("refs") == [sid] and task_line and d["meta"]["from_line"] == task_line["_id"]
          and d["meta"]["kind"] == "task" and d["title"] == "Task: add a count badge to Ask"
          and grants == [{"login": "watcher1", "items": [sid]}]
          and len(rows) == 5 and "Excel" in rows[3] and "EXCEL.EXE" in rows[3] and "On it" in rows[4] and "→ TK-" in rows[2]
          and w_lines == 4 and w_reply == 1 and w_live and status == ["open"] and ended and not p.errors and not w.errors)
    return ok, (f"lines={len(lines)}, pointed={len(pointed)}, view after pick={view_after_pick}, drafts={[x['body'].get('status') for x in drafts]}, "
                f"grants={grants}, stream rows={len(rows)} last={rows[-1][:40] if rows else ''!r}, watcher lines={w_lines} replies={w_reply} live={w_live}, "
                f"status={status}, ended={ended}, errors={(p.errors + w.errors)[:1]}")


async def s47_live_browser_speech_opt_in(b):
    """Browser speech is off by default: opening Live never starts recognition and the mic stays disabled. Only after
    ticking "Browser speech" does Listen start it; a finished phrase becomes a line marked via browser-speech."""
    st = Store([]); p = await open_page(b, st, {"fakeSpeech": True})
    await p.click("#liveBtn"); await p.wait_for_timeout(150)
    await p.click("#liveStart")
    await until(lambda: any(e["kind"] == "item.create" for e in st.events))
    await p.wait_for_function("!!NodachLive.session()", timeout=8000)   # CI: the session id is set just after the write lands
    sid = await p.evaluate("NodachLive.session()")
    unticked = not await p.is_checked("#liveSpeechOk"); mic_off = await p.locator("#liveMic").is_disabled()
    starts0 = await p.evaluate("window.__speech.starts")
    note = await p.inner_text("#liveSpeechNote")
    await p.check("#liveSpeechOk"); await p.click("#liveMic"); await p.wait_for_timeout(100)
    starts1 = await p.evaluate("window.__speech.starts")
    await p.evaluate("window.__speech.last.say('build: a voice line')")
    await until(lambda: len(lines_of(st, sid)) == 1)
    await p.uncheck("#liveSpeechOk"); await p.wait_for_timeout(100)
    listening = await p.evaluate("NodachLive.listening()")
    ln = lines_of(st, sid)
    ok = (unticked and mic_off and starts0 == 0 and starts1 == 1 and "Google" in note and "stays on your PC" in note
          and len(ln) == 1 and ln[0]["body"]["via"] == "browser-speech" and not listening and not p.errors)
    return ok, f"off by default={unticked}, mic disabled={mic_off}, starts {starts0}->{starts1}, lines={[x['body'] for x in ln]}, listening after untick={listening}, errors={p.errors[:1]}"


async def s48_live_member_lines(b):
    """A stakeholder given one session adds a "build:" line: it's kept as their line but they can't create items, so it
    shows "build?" and no draft is written for them. The owner sees a Draft button; pressing it twice makes one draft.
    A member gets no Start button."""
    sid = "LV-261119-120000-test"
    st = Store([ev("item.create", None, {"id": sid, "type": "idea", "title": "Live session", "status": "live", "source": "live",
                                         "live": {"schema": "live.v1", "started": "2026-11-19T12:00:00.000Z"}}, ts="2026-11-19T12:00:00.000Z")])
    m = await open_page(b, st, {"me": "github:stake1", "owner": False})
    await m.click("#liveBtn"); await m.wait_for_timeout(200)
    no_start = not await exists(m, "#liveStart")
    followed = await m.evaluate("NodachLive.session()")
    await m.fill("#liveText", "Build: a supplier page"); await m.press("#liveText", "Enter")
    await until(lambda: len(lines_of(st, sid)) == 1)
    member_creates = [e for e in st.events if e["kind"] == "item.create" and e["author"]["id"] == "github:stake1"]
    await m.wait_for_timeout(200)
    tag = await m.locator("#liveStream .chip.task").all_inner_texts()
    o = await open_page(b, st, {})
    await o.evaluate(f"select('{sid}')"); await o.wait_for_timeout(200)
    has_btn = await exists(o, "#inspLive [data-live-draft]")
    await o.click("#inspLive [data-live-draft]")
    await until(lambda: any(e["kind"] == "item.create" and e["body"].get("type") == "change" for e in st.events))
    await o.evaluate(f"NodachLive.draft('{sid}', '{lines_of(st, sid)[0]['_id']}')"); await o.wait_for_timeout(400)
    drafts = [e["body"] for e in st.events if e["kind"] == "item.create" and e["body"].get("type") == "change"]
    speaker = lines_of(st, sid)[0]["body"]["speaker"]
    ok = (no_start and followed == sid and not member_creates and tag == ["build?"] and has_btn and len(drafts) == 1
          and drafts[0]["status"] == "draft" and drafts[0]["meta"]["kind"] == "build" and speaker == "Member" and not m.errors and not o.errors)
    return ok, f"no start={no_start}, followed={followed}, member creates={len(member_creates)}, tag={tag}, owner button={has_btn}, drafts={len(drafts)}, speaker={speaker}, errors={(m.errors + o.errors)[:1]}"



SHIM = HERE.parent / "standalone.js"


async def open_hub_page(browser, docs, poll_ms=60000, fast_ms=600, me=None, width=1440, height=900):
    """Studio as the hub serves it (D-171): the real host shim (standalone.js) over a stand-in hub API, so polling,
    sign-in and sign-out are the real code paths. docs: [{doc_id, event}]; append to H["docs"] to "write on the hub"."""
    import urllib.parse
    me = me or {"login": "Morami49", "id": "github:Morami49", "owner": True}
    H = {"docs": list(docs), "posts": [], "gets": 0, "visits": []}
    html = ('<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            '<style>:root{color-scheme:light}body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}</style>'
            f'<script>window.__nodachBuild="test";window.__nodachEnv="production";window.__nodachPollMs={poll_ms};window.__nodachFastPollMs={fast_ms};</script>'
            f'<script>{SHIM.read_text()}</script></head><body>{PAGE.read_text()}</body></html>')

    async def serve(route):
        req = route.request; u = urllib.parse.urlparse(req.url); path = u.path; q = urllib.parse.parse_qs(u.query)
        H["visits"].append(path)
        js = lambda v, status=200: route.fulfill(status=status, body=json.dumps(v), content_type="application/json")
        if path in ("/studio", "/studio/"):
            return await route.fulfill(body=html, content_type="text/html")
        if path in ("/studio/login", "/studio/logout"):
            return await route.fulfill(body=f"<!doctype html><title>{path}</title><p id=where>{path}</p>", content_type="text/html")
        if path == "/health":
            return await route.fulfill(body="ok test", content_type="text/plain")
        if path == "/studio/api/me":
            return await js(me)
        if path == "/studio/api/genesis":
            return await js(GENESIS)
        if path == "/studio/api/events" and req.method == "GET":
            H["gets"] += 1
            after, lim = int(q.get("after", ["0"])[0]), int(q.get("limit", ["500"])[0])
            page = H["docs"][after:after + lim]
            return await js({"events": page, "next": after + len(page), "total": len(H["docs"])})
        if path == "/studio/api/events" and req.method == "POST":
            b = json.loads(req.post_data or "{}"); H["posts"].append(b)
            if not any(d["doc_id"] == b["doc_id"] for d in H["docs"]):
                H["docs"].append({"doc_id": b["doc_id"], "event": b["event"]})
            return await js({"id": b["doc_id"], "imported": 1})
        if path == "/studio/api/private":
            return await js({"error": "no private store"}, 503)
        return await route.fulfill(status=404, body="")
    ctx = await browser.new_context(viewport={"width": width, "height": height})
    await ctx.add_init_script(nav_init(True))   # NAV-2: drives Studio through Classic tabs
    await ctx.route("http://hub.test/**", serve)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto("http://hub.test/studio")
    await page.wait_for_timeout(1200)
    page.errors = errors; page.hub = H
    return page


def hub_doc(doc_id, kind, item, body, who="github:Morami49", label="Morami49", ts=None, via=None):
    e = {"ts": ts or now_ts(), "kind": kind, "item": item, "branch": "main", "body": body,
         "author": {"id": who, "label": label, "role": "owner"}}
    if via:
        e["via"] = via
    return {"doc_id": doc_id, "event": e}


async def s52_live_connector_reply_in_seconds(b):
    """F-live-replies-late (20 Nov, staging): Studio served by the hub, a live session open in the Live panel. A reply
    Claude writes through the connector (via claude-connector) shows in the stream within a few seconds, with no
    reload, although the normal poll is a minute here; closing the panel drops back to the normal poll."""
    sid = "LV-261120-190000-abcd"
    p = await open_hub_page(b, [hub_doc("d1", "item.create", None, {"id": sid, "type": "idea", "title": "Live session", "body": "x", "refs": [],
                                                                    "status": "live", "source": "live", "live": {"schema": "live.v1", "started": now_ts(), "audience": []}})])
    await p.evaluate(f"NodachLive.open('{sid}')"); await p.wait_for_timeout(300)
    fast = (await p.evaluate("NodachLive.state()"))["fast_poll_ms"]
    await p.fill("#liveText", "can you see this"); await p.press("#liveText", "Enter"); await p.wait_for_timeout(300)
    p.hub["docs"].append(hub_doc("hubreply1", "comment", sid, {"text": "Yes, I see your line"}, via="claude-connector"))
    import time
    t0 = time.monotonic(); seen = False
    while time.monotonic() - t0 < 5:
        txt = await p.evaluate("document.getElementById('liveStream')?.innerText || ''")
        if "Yes, I see your line" in txt:
            seen = True; break
        await p.wait_for_timeout(100)
    took = round(time.monotonic() - t0, 1)
    reply_row = await p.locator("#liveStream li.reply").count()
    await p.click("#liveClose"); await p.wait_for_timeout(200)
    after = (await p.evaluate("NodachLive.state()"))["fast_poll_ms"]
    lines = [x for x in p.hub["posts"] if x["event"]["kind"] == "live.line"]
    ok = seen and took < 4 and reply_row >= 1 and fast > 0 and after == 0 and len(lines) == 1 and not p.errors
    return ok, f"reply shown={seen} after {took}s, reply rows={reply_row}, fast poll={fast} ms then {after}, lines posted={len(lines)}, errors={p.errors[:1]}"


async def s53_live_states(b):
    """Live is an explicit state machine too: open (idle), start (live), the session is ended in another place (lost),
    pick another live session (select), End here (end), close. NodachLive.state() gives the state and the trace."""
    old = "LV-261120-170000-aaaa"
    st = Store([ev("item.create", None, {"id": old, "type": "idea", "title": "Live session", "body": "x", "refs": [], "status": "live", "source": "live",
                                         "live": {"schema": "live.v1", "started": "2026-11-20T17:00:00.000Z", "audience": []}}, ts="2026-11-20T17:00:00.000Z")])
    p = await open_page(b, st, {})
    seq = []
    async def s(): seq.append(await p.evaluate("NodachLive.state().state"))
    await s()
    await p.evaluate("NodachLive.open()"); await p.wait_for_timeout(200); await s()      # the old one is live: follows it
    await st.add("events", ev("status", old, {"status": "open"}, ts=now_ts())); await p.wait_for_timeout(300); await s()   # ended elsewhere
    await p.click("#liveStart")
    await until(lambda: sum(e["kind"] == "item.create" and e["body"].get("source") == "live" for e in st.events) == 2)
    await p.wait_for_timeout(200); await s()
    await p.click("#liveEnd")
    await until(lambda: sum(e["kind"] == "status" for e in st.events) == 2)
    await p.wait_for_timeout(200); await s()
    await p.click("#liveClose"); await p.wait_for_timeout(100); await s()
    tr = [(t["event"], t["to"]) for t in await p.evaluate("NodachLive.state().trace") if t["ok"]]
    want = [("open", "idle"), ("select", "live"), ("lost", "idle"), ("start", "live"), ("end", "idle"), ("close", "closed")]
    ok = seq == ["closed", "live", "idle", "live", "idle", "closed"] and tr == want and not p.errors
    return ok, f"states={seq}, trace={tr}, errors={p.errors[:1]}"


async def open_served_studio(browser, me, classic=True):
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
    await ctx.add_init_script(nav_init(classic))
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


# ---------------------------------------------------------------- NAV-2 (D-nav-hover, 101c a): the hover-reveal bar
NAV_STATE = """() => { const h = document.querySelector('#hdr'), r = el => el && el.getBoundingClientRect(), op = sel => +getComputedStyle(document.querySelector(sel)).opacity;
  return {open: h.dataset.open, sections: op('#sections'), word: op('.nb-word'), hint: op('.nb-hint'), bg: getComputedStyle(h).backgroundColor,
    border: getComputedStyle(h).borderBottomWidth, tabs: !!document.querySelector('#tabs').offsetParent,
    mark: r(document.querySelector('.nb-mark')).width > 20, avatar: r(document.querySelector('#avatarBtn')).width > 20,
    markColor: getComputedStyle(document.querySelector('.nb-lockup')).color, wordColor: getComputedStyle(document.querySelector('.nb-word')).color,
    order: ['.nb-mark', '.nb-word', '[data-s=explore]', '[data-s=private]', '[data-s=teams]', '[data-s=settings]', '#goAddr', '#avatarBtn'].map(s => Math.round(r(document.querySelector(s)).left)),
    menu: !document.querySelector('#avatarMenu').hidden}; }"""


TIMER_JS = """() => { const T = window.__navT = {}, h = document.querySelector('#hdr'), bar = document.querySelector('#navBar');
  bar.addEventListener('pointerenter', () => { T.enter ??= performance.now(); }); bar.addEventListener('pointerleave', () => { T.leave ??= performance.now(); });
  new MutationObserver(() => { T[h.dataset.open === 'true' ? 'open' : 'close'] ??= performance.now(); }).observe(h, {attributes: true, attributeFilter: ['data-open']}); }"""


async def nav_open(p):
    box = await p.locator(".nb-mark").bounding_box()
    await p.mouse.move(box["x"] + 5, box["y"] + 5); await p.wait_for_timeout(500)


async def s55_nav_collapsed_expanded(b):
    """NAV-2: Studio opens with a quiet bar (the N mark, three node dots, the avatar; no background or divider). Resting
    the pointer on it opens the full bar after 150 ms (lockup, Explore, Private, Teamspaces, Settings, Search, avatar, in
    that order, the lockup all green, cream in the dark theme); leaving it closes it 400 ms later."""
    st = Store(); p = await open_page(b, st, {"classic": False})
    c = await p.evaluate(NAV_STATE)
    collapsed = (c["open"] == "false" and c["sections"] == 0 and c["word"] == 0 and c["hint"] == 1 and c["mark"] and c["avatar"]
                 and not c["tabs"] and c["bg"] == "rgba(0, 0, 0, 0)" and c["border"] == "0px")
    # timings measured inside the page, so a slow runner can't make them flaky
    await p.evaluate(TIMER_JS)
    box = await p.locator(".nb-mark").bounding_box()
    await p.mouse.move(box["x"] + 5, box["y"] + 5); await p.wait_for_timeout(600)
    o = await p.evaluate(NAV_STATE)
    T = await p.evaluate("window.__navT")
    early = "false" if T.get("open") and T["open"] - T["enter"] >= 140 else "true"
    green = "rgb(14, 74, 61)"
    opened = (early == "false" and o["open"] == "true" and o["sections"] == 1 and o["word"] == 1 and o["hint"] == 0
              and o["order"] == sorted(o["order"]) and o["markColor"] == green and o["wordColor"] == green)
    await p.evaluate("window.scrollTo(0, 500); document.querySelector('#v-map').scrollTop = 400")
    still_clear = (await p.evaluate(NAV_STATE))["bg"] == "rgba(0, 0, 0, 0)"
    await p.mouse.move(700, 700); await p.wait_for_timeout(900)
    T = await p.evaluate("window.__navT")
    lingers = "true" if T.get("close") and T["close"] - T["leave"] >= 390 else "false"
    closed = (await p.evaluate(NAV_STATE))["open"]
    await p.evaluate("document.documentElement.dataset.theme = 'dark'")
    d = await p.evaluate(NAV_STATE); cream = "rgb(243, 227, 204)"
    await p.screenshot(path=str(OUT / "nav2-collapsed.png"))
    ok = collapsed and opened and still_clear and lingers == "true" and closed == "false" and d["markColor"] == cream and d["wordColor"] == cream and not p.errors
    return ok, (f"collapsed={collapsed}, opened early={early}, opened={opened} order={o['order']}, lockup {o['markColor']}/{o['wordColor']}, "
                f"clear on scroll={still_clear}, waited 400 ms to close={lingers}, closed later={closed == 'false'}, dark lockup={d['markColor']}, errors={p.errors[:1]}")


async def s56_sections_reach_views(b):
    """NAV-2: every one of the 15 views is reachable from the new bar. Private holds ActionMap, List, Version tree, My
    queue, Ask, Capture and Pack; Teamspaces holds Teamspaces (BQ-1 step 2), Builders, Money, Hub and Show; Settings holds Runs, Usage (BQ-1 step 3), Developer mode,
    theme, Point, branch and Classic tabs; Search goes to an address or adds a command (Command map); Explore is a
    placeholder; the avatar menu has the account and profiles (inside claude.ai there is no profile list, so the new-window
    entry is disabled there; s60 covers it served by the hub); as-of sits on the canvas."""
    st = Store(); p = await open_page(b, st, {"classic": False})
    want = {"private": ["map", "list", "tree", "queue", "ask", "capture", "pack"], "teams": ["spaces", "builders", "money", "hub"], "settings": ["runs", "usage", "self"]}
    reached, missing = [], []
    for sec, views in want.items():
        await nav_open(p); await p.click(f"#sections [data-s={sec}]"); await p.wait_for_timeout(150)
        for v in views:
            await p.click(f"#subnav [data-sv={v}]"); await p.wait_for_timeout(120)
            ok_v = await p.evaluate(f"S.view === '{v}' && !document.querySelector('#v-{v}').hidden && S.section === '{sec}'")
            (reached if ok_v else missing).append(v)
    show_in_teams = await p.evaluate("['#showBtn', '#liveBtn'].every(s => document.querySelector('#subnav [data-sub=teams] ' + s))")
    settings_ctl = await p.evaluate("['#theme','#pointBtn','#branch','#classicTabs','#conn'].every(s => document.querySelector('#subnav [data-sub=settings] ' + s))")
    dev_named = "Developer mode" in await p.inner_text("#v-self h2")
    await nav_open(p); await p.click("#sections [data-s=explore]"); await p.wait_for_timeout(150)
    explore = "Explore is being designed" in await p.inner_text("#v-explore")
    await nav_open(p); await p.fill("#goAddr", "D-001"); await p.press("#goAddr", "Enter"); await p.wait_for_timeout(300)
    went = await p.evaluate("S.sel") == "D-001"
    await p.fill("#goAddr", "capture idea venue list"); await p.press("#goAddr", "Enter"); await p.wait_for_timeout(500)
    cmd = await p.evaluate("S.view === 'command' && commands().length === 1 && commands()[0].raw === 'capture idea venue list'")
    reached += ["command"] if cmd else []
    await nav_open(p); await p.click("#sections [data-s=private]"); await p.click("#subnav [data-sv=map]"); await p.wait_for_timeout(100)
    asof_on_canvas = await p.evaluate("!!document.querySelector('#v-map #asof') && !!document.querySelector('#asof').offsetParent")
    await p.click("#avatarBtn"); await p.wait_for_timeout(150)
    menu = await p.evaluate("""() => { const m = document.querySelector('#avatarMenu'), w = document.querySelector('#profWindow');
      return !m.hidden && w.disabled && w.title === 'No profile to open' && m.querySelectorAll('#profList li').length >= 1 && !!m.querySelector('#signOut') && m.textContent.includes('Signed in'); }""")
    await p.keyboard.press("Escape"); await p.wait_for_timeout(100)
    ok = (not missing and len(reached) == 15 and show_in_teams and settings_ctl and dev_named and explore and went and cmd and asof_on_canvas and menu and not p.errors)
    return ok, (f"views reached={len(reached)}/15 missing={missing}, Live and Show in Teamspaces={show_in_teams}, Settings controls={settings_ctl}, Developer mode={dev_named}, "
                f"Explore placeholder={explore}, search address={went}, search command={cmd}, as-of on canvas={asof_on_canvas}, avatar menu={menu}, errors={p.errors[:1]}")


async def s57_esc_touch_keyboard(b):
    """NAV-2: keyboard focus opens the bar and Esc closes it (and the avatar menu); on a phone a tap on the bar opens
    and closes it; with reduced motion there is no fade."""
    st = Store(); p = await open_page(b, st, {"classic": False})
    await p.evaluate("document.activeElement?.blur(); document.body.focus()")
    for _ in range(4):
        await p.keyboard.press("Tab"); await p.wait_for_timeout(30)
        if await p.evaluate("document.querySelector('#navBar').contains(document.activeElement)"): break
    focus_opens = await p.evaluate("document.querySelector('#hdr').dataset.open") == "true"
    await p.keyboard.press("Escape"); await p.wait_for_timeout(100)
    esc_closes = await p.evaluate("document.querySelector('#hdr').dataset.open") == "false"
    await nav_open(p); await p.click("#avatarBtn"); await p.wait_for_timeout(100)
    menu_open = await p.evaluate("!document.querySelector('#avatarMenu').hidden")
    await p.keyboard.press("Escape"); await p.wait_for_timeout(100)
    menu_esc = await p.evaluate("document.querySelector('#avatarMenu').hidden && document.querySelector('#hdr').dataset.open === 'false'")
    t = await open_page(b, st, {"classic": False}, width=390, height=800, touch=True)
    await t.tap(".nb-hint", position={"x": 2, "y": 2}, force=True); await t.wait_for_timeout(100)
    tap_opens = await t.evaluate("document.querySelector('#hdr').dataset.open") == "true"
    await t.tap(".nb-lockup"); await t.wait_for_timeout(100)
    tap_closes = await t.evaluate("document.querySelector('#hdr').dataset.open") == "false"
    overflow = await t.evaluate("document.documentElement.scrollWidth > innerWidth")
    await t.screenshot(path=str(OUT / "nav2-phone.png"))
    r = await open_page(b, st, {"classic": False}, reduced=True)
    no_fade = await r.evaluate("getComputedStyle(document.querySelector('#sections')).transitionDuration") in ("0s", "0s, 0s")
    ok = focus_opens and esc_closes and menu_open and menu_esc and tap_opens and tap_closes and not overflow and no_fade and not (p.errors + t.errors + r.errors)
    return ok, (f"focus opens={focus_opens}, Esc closes={esc_closes}, avatar menu Esc={menu_open and menu_esc}, tap opens={tap_opens}, tap closes={tap_closes}, "
                f"phone overflow={overflow}, reduced motion no fade={no_fade}, errors={(p.errors + t.errors + r.errors)[:1]}")


async def s58_classic_tabs(b):
    """NAV-2: the old tab row stays reachable under Settings → Classic tabs until each move is verified: turning it on
    shows all 13 tabs and the old rail (each works), the choice survives a reload, and turning it off brings the bar back."""
    st = Store(); p = await open_page(b, st, {"classic": False})
    await nav_open(p); await p.click("#sections [data-s=settings]"); await p.wait_for_timeout(100)
    await p.check("#classicTabs"); await p.wait_for_timeout(150)
    tabs = await p.evaluate("[...document.querySelectorAll('#tabs button')].map(x => x.dataset.v)")
    works = []
    for v in tabs:
        await p.click(f"#tabs button[data-v={v}]"); await p.wait_for_timeout(80)
        if await p.evaluate(f"S.view === '{v}' && !document.querySelector('#v-{v}').hidden"): works.append(v)
    in_rail = await p.evaluate("['#pointBtn','#showBtn','#liveBtn','#theme','#branch','#asof','#classicTabs'].every(s => document.querySelector('#rail ' + s))")
    await p.reload(); await p.wait_for_timeout(900)
    kept = await p.evaluate("document.querySelector('#hdr').classList.contains('classic') && !!document.querySelector('#tabs').offsetParent")
    await p.uncheck("#classicTabs"); await p.wait_for_timeout(150)
    back = await p.evaluate("!document.querySelector('#tabs').offsetParent && !!document.querySelector('#subnav [data-sub=settings] #pointBtn') && !!document.querySelector('#v-map #asof')")
    ok = len(tabs) == 13 and works == tabs and in_rail and kept and back and not p.errors
    return ok, f"tabs={len(tabs)}, working={len(works)}, old rail={in_rail}, kept after reload={kept}, off restores bar={back}, errors={p.errors[:1]}"


async def s59_dev_mode_proposes(b):
    """NVS-4: in production a look or schema save from Developer mode no longer applies for everyone: it writes a
    proposal (a change item, status proposed, naming the setting and value) and nothing changes. On staging the
    proposal can be applied, which writes the setting version and marks the proposal shipped."""
    st = Store([]); p = await open_page(b, st, {"classic": False})
    await p.evaluate("show('self')"); await p.wait_for_timeout(150)
    await p.fill("#tyKey", "venue"); await p.fill("#tyLabel", "Venue"); await p.click("#tyAdd"); await p.wait_for_timeout(400)
    await p.evaluate("""() => { const i = document.querySelector('[data-tok="--accent"]'); i.value = '#aa0000'; i.dispatchEvent(new Event('input')); }""")
    await p.click("#lkSave"); await p.wait_for_timeout(400)
    props = await p.evaluate("[...F.items.values()].filter(i => i.type === 'change' && i.status === 'proposed' && i.meta?.target).map(i => i.meta.target).sort()")
    untouched = await p.evaluate("!F.items.has('SCHEMA-types') && !F.items.has('SET-ui-theme') && !('venue' in TYPES) && getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() !== '#aa0000'")
    no_apply_here = await p.locator("[data-crapply]").count() == 0
    s = await open_page(b, st, {"classic": False, "env": "staging"})
    await s.evaluate("show('self')"); await s.wait_for_timeout(200)
    n_apply = await s.locator("[data-crapply]").count()
    cid = await s.evaluate("[...F.items.values()].find(i => i.type === 'change' && i.meta?.target === 'SCHEMA-types').id")
    await s.click(f'[data-crapply="{cid}"]'); await s.wait_for_timeout(400)
    applied = await s.evaluate(f"F.items.has('SCHEMA-types') && TYPES.venue === 'Venue' && F.items.get('{cid}').status === 'shipped'")
    ok = props == ["SCHEMA-types", "SET-ui-theme"] and untouched and no_apply_here and n_apply == 2 and applied and not (p.errors + s.errors)
    return ok, f"proposals={props}, nothing applied in production={untouched}, no apply button there={no_apply_here}, apply buttons on staging={n_apply}, applied on staging={applied}, errors={(p.errors + s.errors)[:1]}"

# ---------------------------------------------------------------- B-profile-windows (98b b): one profile per window
PROFILES = [{"id": "P-morami49", "kind": "personal", "label": "Personal", "implicit": True, "revoked": None},
            {"id": "P-work", "kind": "work", "label": "Lift work", "implicit": False, "revoked": None},
            {"id": "P-old", "kind": "venture", "label": "Old venture", "implicit": False, "revoked": "2026-11-20T10:00:00.000Z"}]


async def open_profile_hub(browser, query="", refuse=(), profiles=None, ctx=None):
    """Studio served by a stand-in hub that knows the signed-in person's profiles (/studio/api/profiles) and, like the
    real hub, refuses a write whose x-nodach-profile is in `refuse` with 403 {profile_mismatch} and the hub's words.
    Every window opened from the page shares the stand-in (same context), so new windows are real windows here."""
    import urllib.parse
    H = {"docs": [], "posts": [], "headers": [], "refused": [], "profiles": profiles or PROFILES, "refuse": set(refuse)}
    html = ('<!doctype html><html lang=en><head><meta charset=utf-8><style>:root{color-scheme:light}body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style>'
            '<script>window.__nodachBuild="test";window.__nodachEnv="production";window.__nodachPollMs=60000;</script>'
            f'<script>{SHIM.read_text()}</script></head><body>{PAGE.read_text()}</body></html>')

    async def serve(route):
        req = route.request; u = urllib.parse.urlparse(req.url); path = u.path; q = urllib.parse.parse_qs(u.query)
        js = lambda v, status=200: route.fulfill(status=status, body=json.dumps(v), content_type="application/json")
        if path in ("/studio", "/studio/"):
            return await route.fulfill(body=html, content_type="text/html")
        if path == "/health":
            return await route.fulfill(body="ok test", content_type="text/plain")
        if path == "/studio/api/me":
            return await js({"login": "Morami49", "id": "github:Morami49", "owner": True})
        if path == "/studio/api/profiles":
            return await js({"account": {"id": "acct:github:morami49"}, "profiles": H["profiles"], "connections": []})
        if path == "/studio/api/genesis":
            return await js(GENESIS)
        if path == "/studio/api/events" and req.method == "GET":
            after, lim = int(q.get("after", ["0"])[0]), int(q.get("limit", ["500"])[0])
            page = H["docs"][after:after + lim]
            return await js({"events": page, "next": after + len(page), "total": len(H["docs"])})
        if path.startswith("/studio/api/") and req.method == "POST":
            prof = req.headers.get("x-nodach-profile")
            b = json.loads(req.post_data or "{}")
            H["headers"].append(prof)
            if prof in H["refuse"]:
                H["refused"].append(prof)
                return await js({"error": f"profile {prof} isn't one of yours (signed in as Morami49); this window can only write as your own profiles. Nothing was saved.",
                                 "refused": True, "profile_mismatch": True}, 403)
            if path == "/studio/api/events":
                H["posts"].append(b)
                if not any(d["doc_id"] == b["doc_id"] for d in H["docs"]):
                    H["docs"].append({"doc_id": b["doc_id"], "event": b["event"]})
                return await js({"id": b["doc_id"], "imported": 1})
        if path == "/studio/api/private":
            return await js({"error": "no private store"}, 503)
        return await route.fulfill(status=404, body="")
    if ctx is None:
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        await ctx.add_init_script(nav_init(False))
        await ctx.route("http://hub.test/**", serve)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto("http://hub.test/studio" + query)
    await page.wait_for_timeout(1200)
    page.errors = errors; page.hub = H; page.ctx = ctx
    return page


STRIP = """() => { const el = document.querySelector('#profStrip'); const c = el.hidden ? null : NodachProfile.colours(el.dataset.profile);
  return {shown: !el.hidden && el.getBoundingClientRect().height > 8, text: el.innerText, id: el.dataset.profile || null, bg: getComputedStyle(el).backgroundColor,
    fg: getComputedStyle(el).color, ratio: c && c.ratio, dark: c && c.dark, below: el.getBoundingClientRect().top >= document.querySelector('#navBar').getBoundingClientRect().bottom - 1,
    session: (() => { try { return sessionStorage.getItem('nodach.profile'); } catch (e) { return 'x'; } })(), url: location.search}; }"""


async def s60_profile_new_window(b):
    """B-profile-windows: like Chrome profiles, each profile opens in its own window. The avatar menu lists the person's
    own profiles (revoked ones left out), and "New window" opens Studio at ?profile=<id> in a new window that works as
    that profile, while the first window keeps its own. "Use here" switches the profile of this window only."""
    p = await open_profile_hub(b)
    await nav_open(p); await p.click("#avatarBtn"); await p.wait_for_timeout(150)
    listed = await p.evaluate("[...document.querySelectorAll('#profList [data-prof]')].map(x => x.dataset.prof)")
    first = (await p.evaluate(STRIP))["id"]
    btn_on = await p.evaluate("!document.querySelector('#profWindow').disabled")
    async with p.ctx.expect_page() as info:
        await p.click('[data-profwin="P-work"]')
    w = await info.value
    w.errors = []; w.on("pageerror", lambda e: w.errors.append(str(e)))
    await w.wait_for_load_state(); await w.wait_for_timeout(1300)
    ws = await w.evaluate(STRIP); ps = await p.evaluate(STRIP)
    # switching within the same window
    await nav_open(p); await p.click("#avatarBtn") if await p.evaluate("document.querySelector('#avatarMenu').hidden") else None
    await p.wait_for_timeout(100)
    await p.click('[data-profuse="P-work"]'); await p.wait_for_timeout(200)
    here = await p.evaluate(STRIP)
    await p.reload(); await p.wait_for_timeout(1300)
    kept = (await p.evaluate(STRIP))["id"]
    ok = (listed == ["P-morami49", "P-work"] and first == "P-morami49" and btn_on and "profile=P-work" in w.url and ws["id"] == "P-work"
          and ws["session"] == "P-work" and ps["id"] == "P-morami49" and here["id"] == "P-work" and here["url"] == "?profile=P-work" and kept == "P-work"
          and not (p.errors + w.errors))
    return ok, (f"listed={listed}, default={first}, button on={btn_on}, new window url={w.url[-20:]} works as {ws['id']} (tab keeps {ws['session']}), "
                f"first window still {ps['id']}, use here -> {here['id']} {here['url']}, after reload {kept}, errors={(p.errors + w.errors)[:1]}")


async def s61_profile_strip_shown(b):
    """B-profile-windows: a thin strip under the bar names the window's profile. Its colour is a stable hash of the
    profile id (the same id gets the same colour in every window, two profiles differ), the text is the --ink token
    at 4.5:1 or better, and it follows the dark theme. A profile in the URL that isn't the person's falls back to their
    first profile, with a note."""
    p = await open_profile_hub(b, "?profile=P-work")
    a = await p.evaluate(STRIP)
    q = await open_profile_hub(b, "?profile=P-work", ctx=p.ctx)
    same = (await q.evaluate(STRIP))["bg"] == a["bg"]
    r = await open_profile_hub(b, "?profile=P-morami49", ctx=p.ctx)
    differs = (await r.evaluate(STRIP))["bg"] != a["bg"]
    await p.evaluate("document.documentElement.dataset.theme = 'dark'"); await p.wait_for_timeout(150)
    d = await p.evaluate(STRIP)
    await p.screenshot(path=str(OUT / "profile-strip-dark.png"))
    x = await open_profile_hub(b, "?profile=P-someone-else", ctx=p.ctx)
    fb = await x.evaluate(STRIP); note = "isn't one of yours" in await visible_text(x)
    ok = (a["shown"] and "Lift work" in a["text"] and a["below"] and a["ratio"] >= 4.5 and not a["dark"] and same and differs
          and d["shown"] and d["dark"] and d["ratio"] >= 4.5 and d["bg"] != a["bg"] and fb["id"] == "P-morami49" and note
          and not (p.errors + q.errors + r.errors + x.errors))
    return ok, (f"strip={a['shown']} {a['text']!r} under bar={a['below']} contrast={a['ratio']}, same id same colour={same}, other profile differs={differs}, "
                f"dark={d['dark']} contrast={d['ratio']} {d['bg']}, unknown profile falls back to {fb['id']} with note={note}, errors={(p.errors + x.errors)[:1]}")


async def s62_write_carries_profile(b):
    """B-profile-windows: every write names the window's profile, in the x-nodach-profile header and in the event's
    author.profile, so the hub can check it and the record shows which profile wrote it. Two windows, two profiles."""
    p = await open_profile_hub(b, "?profile=P-work")
    w = await open_profile_hub(b, "?profile=P-morami49", ctx=p.ctx)
    for page, text in ((p, "from the work window"), (w, "from the personal window")):
        await page.evaluate(f"write('comment', 'D-001', {{text: {json.dumps(text)}}})"); await page.wait_for_timeout(300)
    H = p.hub if p.hub["posts"] else w.hub
    posts = p.hub["posts"] + w.hub["posts"]
    by_text = {x["event"]["body"]["text"]: x["event"]["author"].get("profile") for x in posts}
    hdrs = p.hub["headers"] + w.hub["headers"]
    ok = (by_text == {"from the work window": "P-work", "from the personal window": "P-morami49"} and sorted(hdrs) == ["P-morami49", "P-work"]
          and not (p.errors + w.errors))
    return ok, f"author.profile by write={by_text}, headers={hdrs}, errors={(p.errors + w.errors)[:1]}"


async def s63_refused_write_shows_message(b):
    """B-profile-windows: when the hub refuses a write because the window's profile isn't the signed-in account's
    (revoked in another window, or someone else's), the page shows the hub's words, saves nothing, and does not turn
    the whole Studio view-only: switching to an own profile writes again."""
    p = await open_profile_hub(b, "?profile=P-work", refuse={"P-work"})
    await p.evaluate("write('comment', 'D-001', {text: 'should be refused'})"); await p.wait_for_timeout(400)
    txt = await visible_text(p)
    shown = "isn't one of yours" in txt and "Nothing was saved" in txt
    saved = len(p.hub["posts"])
    still = await p.evaluate("S.canWrite !== false")
    flagged = await p.evaluate("document.querySelector('#profStrip').dataset.refused === 'true'")
    await p.evaluate("NodachProfile.use('P-morami49', true)")
    await p.evaluate("write('comment', 'D-001', {text: 'as personal'})"); await p.wait_for_timeout(400)
    ok = shown and saved == 0 and p.hub["refused"] == ["P-work"] and still and flagged and len(p.hub["posts"]) == 1 and not p.errors
    return ok, f"message shown={shown}, saved while refused={saved}, still writable={still}, strip marked={flagged}, then saved as personal={len(p.hub['posts'])}, errors={p.errors[:1]}"


# ---------------------------------------------------------------- BQ-1 step 2: teamspaces
async def open_teams_hub(browser, H, login, owner):
    """Studio served by a stand-in hub shared by several people (one browser context each, so each has its own
    session). H holds the record; every page reads all of it, so what a guest doesn't see is what the page itself
    hides (the real hub also filters; hub-core tests cover that). Like the real hub, the stand-in refuses a role
    change by anyone but the owner with 403 {teamspace_refused} and the rule in words."""
    import urllib.parse
    html = ('<!doctype html><html lang=en><head><meta charset=utf-8><style>:root{color-scheme:light}body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style>'
            '<script>window.__nodachBuild="test";window.__nodachEnv="production";window.__nodachPollMs=60000;</script>'
            f'<script>{SHIM.read_text()}</script></head><body>{PAGE.read_text()}</body></html>')
    me = {"login": login, "id": f"github:{login}", "owner": owner}

    async def serve(route):
        req = route.request; u = urllib.parse.urlparse(req.url); path = u.path; q = urllib.parse.parse_qs(u.query)
        js = lambda v, status=200: route.fulfill(status=status, body=json.dumps(v), content_type="application/json")
        if path in ("/studio", "/studio/"):
            return await route.fulfill(body=html, content_type="text/html")
        if path == "/health":
            return await route.fulfill(body="ok test", content_type="text/plain")
        if path == "/studio/api/me":
            return await js(me)
        if path == "/studio/api/profiles":
            return await js({"account": {"id": f"acct:github:{login.lower()}"}, "profiles": [{"id": f"P-{login.lower()}", "kind": "personal", "label": "Personal"}], "connections": []})
        if path == "/studio/api/genesis":
            return await js(GENESIS)
        if path == "/studio/api/events" and req.method == "GET":
            after, lim = int(q.get("after", ["0"])[0]), int(q.get("limit", ["500"])[0])
            page = H["docs"][after:after + lim]
            shown = [d for d in page if team_visible(H, login, d["event"])] if H.get("filter") else page
            return await js({"events": shown, "next": after + len(page), "total": len(H["docs"])})
        if path == "/studio/api/teamspaces":
            return await js(team_directory(H, login))
        if path == "/studio/api/events" and req.method == "POST":
            b = json.loads(req.post_data or "{}"); e = b.get("event", {})
            if e.get("kind") == "member.role" and login.lower() != H["owner"]:
                H["refused"].append(e.get("kind"))
                return await js({"error": f"only an owner of {e.get('item')} can change roles", "refused": True, "teamspace_refused": True}, 403)
            H["posts"].append(b)
            if not any(d["doc_id"] == b["doc_id"] for d in H["docs"]):
                H["docs"].append({"doc_id": b["doc_id"], "event": e})
            return await js({"id": b["doc_id"], "imported": 1})
        if path == "/studio/api/private":
            return await js({"error": "no private store"}, 503)
        if path == "/studio/api/usage" and "usage" in H:   # BQ-1 step 3: the hub's meters for this login
            H.setdefault("usage_hdr", []).append(req.headers.get("x-nodach-profile"))
            return await js(H["usage"](login) if callable(H["usage"]) else H["usage"])
        if path == "/studio/api/run" and req.method == "POST" and "run" in H:
            H.setdefault("runs", []).append(json.loads(req.post_data or "{}"))
            return await js(H["run"])
        return await route.fulfill(status=404, body="")
    ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    await ctx.add_init_script(nav_init(False))
    await ctx.route("http://hub.test/**", serve)
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto("http://hub.test/studio")
    await page.wait_for_timeout(1200)
    page.errors = errors; page.ctx = ctx
    return page


def team_hub():
    return {"docs": [], "posts": [], "refused": [], "owner": "morami49"}


def _team_fold(H):
    """The stand-in's copy of hub_core::teamspaces, as far as reads go: members by login, nodes' teamspaces, support."""
    from datetime import datetime
    ms = lambda t: int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
    spaces, labels, owners, item_space, support = {}, {}, {}, {}, []
    for d in H["docs"]:
        e = d["event"]; b = e.get("body") or {}; who = (e.get("author") or {}).get("id", "").replace("github:", "").lower()
        k = e.get("kind")
        if k == "teamspace.create":
            spaces[b["id"]] = {who}; owners[b["id"]] = {who}; labels[b["id"]] = b.get("label", b["id"])
        elif k == "member.join" and b.get("teamspace") in spaces:
            spaces[b["teamspace"]].add(who)
        elif k == "item.create" and b.get("teamspace"):
            item_space[b.get("id")] = b["teamspace"]
        elif k == "support.access" and who == H["owner"] and b.get("teamspace") in spaces:
            support.append((b["teamspace"], who, ms(e["ts"])))
    return spaces, labels, owners, item_space, support


def _space_of(e, item_space):
    b = e.get("body") or {}; k = e.get("kind")
    if k == "teamspace.create": return b.get("id")
    if k in ("member.invite", "member.join", "member.role", "member.remove", "support.access"): return b.get("teamspace") or e.get("item")
    if k == "node.share": return b.get("teamspace") or item_space.get(b.get("node") or e.get("item"))
    return item_space.get(e.get("item") or b.get("id"))


def team_visible(H, login, e):
    """114b a in the stand-in: the hub owner reads a teamspace he isn't in only while his support access (24 h) runs;
    a support.access is shown to the teamspace's owners. Other logins get everything (the page filters for them)."""
    import time
    login = login.lower(); who = (e.get("author") or {}).get("id", "").replace("github:", "").lower()
    if who == login: return True
    spaces, _, owners, item_space, support = _team_fold(H)
    ts = _space_of(e, item_space)
    if e.get("kind") == "support.access": return login in owners.get(ts, set())
    if login != H["owner"] or ts is None or ts not in spaces or login in spaces[ts]: return True
    now = time.time() * 1000
    return any(t == ts and w == login and at <= now < at + 24 * 3600 * 1000 for t, w, at in support)


def team_directory(H, login):
    import time
    spaces, labels, _, _, support = _team_fold(H)
    login = login.lower()
    if login != H["owner"]: return {"teamspaces": [], "others": []}
    now = time.time() * 1000
    def until(ts):
        u = [at + 24 * 3600 * 1000 for t, w, at in support if t == ts and at <= now < at + 24 * 3600 * 1000]
        return datetime_iso(max(u)) if u else None
    return {"teamspaces": [], "others": [{"id": t, "label": labels[t], "support_until": until(t)} for t in spaces if login not in spaces[t]]}


def datetime_iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms % 1000):03d}Z"


async def to_spaces(p):
    await nav_open(p); await p.click("#sections [data-s=teams]"); await p.wait_for_timeout(120)
    await p.click("#subnav [data-sv=spaces]"); await p.wait_for_timeout(200)


async def open_space(p, ts):
    """Teamspaces → the teamspace's Open button: its own page (the list of teamspaces steps aside)."""
    await to_spaces(p); await p.click(f"#tsList [data-tsopen='{ts}']"); await p.wait_for_timeout(250)


async def make_space(p, label="Insydr pilot"):
    await to_spaces(p)
    await p.fill("#tsLabel", label); await p.click("#tsCreate"); await p.wait_for_timeout(500)
    return await p.evaluate("S.ts")


async def s64_teamspace_create(b):
    """BQ-1 step 2: Nowed opens Teamspaces from the bar, types a name and clicks Create teamspace. The teamspace is an
    event signed as his window's profile, he is its owner, and a Money rules node (a placeholder, no payments) is made
    inside it. Inside claude.ai, where Studio can't sign teamspace events as him, the button is off and says where to go."""
    H = team_hub(); p = await open_teams_hub(b, H, "Morami49", True)
    ts = await make_space(p)
    kinds = [x["event"]["kind"] for x in H["posts"]]
    create = next((x["event"] for x in H["posts"] if x["event"]["kind"] == "teamspace.create"), {})
    money = next((x["event"] for x in H["posts"] if x["event"]["kind"] == "item.create"), {})
    listed = "Insydr pilot" in await p.inner_text("#tsList")
    members = await p.inner_text("#tsMembers")
    money_shown = "placeholder" in await p.inner_text("#tsMoney") and "No payments" in await p.inner_text("#tsMoney")
    st = Store(); c = await open_page(b, st, {"classic": False})
    await c.evaluate("show('spaces')"); await c.wait_for_timeout(150)
    off = await c.evaluate("document.querySelector('#tsCreate').disabled") and "staging.nodach.com/studio" in await c.inner_text("#tsNote")
    ok = (kinds == ["teamspace.create", "item.create"] and create.get("item") == ts and create["body"].get("label") == "Insydr pilot"
          and create["author"].get("profile") == "P-morami49" and money["body"].get("teamspace") == ts and money["body"].get("meta", {}).get("money_rules")
          and listed and "P-morami49" in members and "owner" in members and money_shown and off and not (p.errors + c.errors))
    return ok, (f"writes={kinds}, teamspace={ts} profile={create.get('author', {}).get('profile')}, money node in it={money.get('body', {}).get('teamspace') == ts}, "
                f"listed={listed}, owner shown={'owner' in members}, money placeholder={money_shown}, off inside claude.ai={off}, errors={(p.errors + c.errors)[:1]}")


async def s65_teamspace_invite(b):
    """BQ-1 step 2: Nowed invites Nassar's profile as a guest. Nassar opens Teamspaces, sees the invitation and clicks
    Join; Nowed then makes him a member with Change role. Nassar is offered no role controls, and when a role change
    of his is forced through anyway, the hub's refusal is shown in words and Studio stays writable for him."""
    H = team_hub(); p = await open_teams_hub(b, H, "Morami49", True)
    ts = await make_space(p)
    await p.fill("#tsInvProf", "P-nassar"); await p.select_option("#tsInvRole", "guest"); await p.click("#tsInvite"); await p.wait_for_timeout(400)
    invited = "invited as guest" in await p.inner_text("#tsMembers")
    n = await open_teams_hub(b, H, "nassar", False)
    await to_spaces(n)
    inv_shown = await n.locator("#tsInvList [data-tsjoin]").count() == 1
    await n.click("#tsInvList [data-tsjoin]"); await n.wait_for_timeout(400)
    joined = await n.evaluate(f"TEAMS.roleOf('nassar', '{ts}')")
    no_controls = await n.locator("[data-tsrole], #tsInvite").count() == 0
    await n.evaluate(f"write('member.role', '{ts}', {{teamspace: '{ts}', profile: 'P-nassar', role: 'owner'}})"); await n.wait_for_timeout(400)
    refused_msg = "only an owner" in await visible_text(n); still = await n.evaluate("S.canWrite !== false")
    p2 = await open_teams_hub(b, H, "Morami49", True)
    await open_space(p2, ts)
    await p2.select_option('[data-tsrolesel="P-nassar"]', "member"); await p2.click('[data-tsrole="P-nassar"]'); await p2.wait_for_timeout(400)
    role_ev = [x["event"] for x in H["posts"] if x["event"]["kind"] == "member.role"]
    now_member = await p2.evaluate(f"TEAMS.roleOf('nassar', '{ts}')")
    ok = (invited and inv_shown and joined == "guest" and no_controls and refused_msg and still and H["refused"] == ["member.role"]
          and len(role_ev) == 1 and role_ev[0]["body"] == {"teamspace": ts, "profile": "P-nassar", "role": "member"} and now_member == "member"
          and not (p.errors + n.errors + p2.errors))
    return ok, (f"invite shown={invited}, invitation for Nassar={inv_shown}, joined as {joined}, no role controls for him={no_controls}, "
                f"forced change refused in words={refused_msg} (still writable={still}), owner's change={role_ev[-1]['body'] if role_ev else None} -> {now_member}, "
                f"errors={(p.errors + n.errors + p2.errors)[:1]}")


async def seeded_space(b, H):
    """Nowed's teamspace with two nodes (Plan, Quote) and Nassar as a guest who has joined; Quote is shared with him."""
    p = await open_teams_hub(b, H, "Morami49", True)
    ts = await make_space(p)
    for title in ("Plan", "Quote"):
        await p.fill("#tsNodeTitle", title); await p.click("#tsAddNode"); await p.wait_for_timeout(350)
    ids = await p.evaluate("Object.fromEntries([...F.items.values()].filter(i => TEAMS.itemSpace.get(i.id) === S.ts).map(i => [i.title, i.id]))")
    await p.fill("#tsInvProf", "P-nassar"); await p.select_option("#tsInvRole", "guest"); await p.click("#tsInvite"); await p.wait_for_timeout(350)
    await p.select_option("#tsShareNode", ids["Quote"]); await p.fill("#tsShareTo", "P-nassar"); await p.select_option("#tsShareLevel", "commenter")
    await p.click("#tsShare"); await p.wait_for_timeout(350)
    n = await open_teams_hub(b, H, "nassar", False)
    await to_spaces(n); await n.click("#tsInvList [data-tsjoin]"); await n.wait_for_timeout(400)
    return p, n, ts, ids


async def s66_guest_cant_see_unshared(b):
    """BQ-1 step 2: Nassar is a guest in Nowed's teamspace. He sees the teamspace, its members and the one node shared
    with him (Quote, as commenter), and nowhere sees the node that wasn't shared (Plan) or the Money rules node: not in
    the teamspace, not on the map, not by its address. Nowed, a member of it, sees both."""
    H = team_hub(); p, n, ts, ids = await seeded_space(b, H)
    n2 = await open_teams_hub(b, H, "nassar", False)
    await open_space(n2, ts)
    nodes = await n2.inner_text("#tsNodes")
    items = await n2.evaluate(f"[{json.dumps(ids['Plan'])}, {json.dumps(ids['Quote'])}].map(id => F.items.has(id))")
    money_hidden = await n2.evaluate("![...F.items.values()].some(i => i.meta?.money_rules)") and "isn't shared with you" in await n2.inner_text("#tsMoney")
    await n2.evaluate(f"show('list')"); await n2.wait_for_timeout(150)
    in_list = "Plan" in await n2.inner_text("#v-list")
    members_seen = await n2.evaluate(f"[...TEAMS.spaces.get('{ts}').members.keys()].sort()")
    no_add = await n2.locator("#tsAddNode").count() == 0
    owner_sees = await p.evaluate(f"[{json.dumps(ids['Plan'])}, {json.dumps(ids['Quote'])}].every(id => F.items.has(id))")
    ok = ("Quote" in nodes and "Plan" not in nodes and items == [False, True] and money_hidden and not in_list
          and members_seen == ["P-morami49", "P-nassar"] and no_add and owner_sees and not (p.errors + n.errors + n2.errors))
    return ok, (f"guest's teamspace nodes={nodes.strip()[:60]!r}, Plan/Quote loaded={items}, money node hidden={money_hidden}, Plan in his list={in_list}, "
                f"members={members_seen}, no add-node for guest={no_add}, owner sees both={owner_sees}, errors={(p.errors + n.errors + n2.errors)[:1]}")


async def s67_teamspace_export(b):
    """BQ-1 step 2: before leaving, Nowed (owner) clicks Export teamspace and gets a JSON download: every node members
    created in it (his and Nassar's), each with its full history (a retracted comment included), files as references
    only (asset id and name, never the contents), the membership events, and a node shared in from outside as a link
    only (no body, no history). Nassar, not an owner, has no Export button."""
    H = team_hub(); p, n, ts, ids = await seeded_space(b, H)
    await p.evaluate(f"write('member.role', '{ts}', {{teamspace: '{ts}', profile: 'P-nassar', role: 'member'}})"); await p.wait_for_timeout(300)
    n2 = await open_teams_hub(b, H, "nassar", False)
    await open_space(n2, ts)
    await n2.fill("#tsNodeTitle", "Nassar's site notes"); await n2.click("#tsAddNode"); await n2.wait_for_timeout(350)
    no_export_for_member = await n2.locator("#tsExport").count() == 0
    plan = ids["Plan"]
    p2 = await open_teams_hub(b, H, "Morami49", True)
    c1 = await p2.evaluate(f"write('comment', {json.dumps(plan)}, {{text: 'first thought'}})"); await p2.wait_for_timeout(250)
    await p2.evaluate(f"write('retract', {json.dumps(plan)}, {{of: {json.dumps(c1)}}})"); await p2.wait_for_timeout(250)
    await p2.evaluate(f"write('comment', {json.dumps(plan)}, {{text: 'Raw material: quote.pdf', raw: {{kind: 'document', asset: 'a77', name: 'quote.pdf', size: 1234, type: 'application/pdf', enc: 'aes-256-gcm/pbkdf2-sha256-310k'}}}})")
    await p2.wait_for_timeout(250)
    await p2.evaluate(f"write('node.share', 'D-001', {{node: 'D-001', teamspace: '{ts}', level: 'viewer'}})"); await p2.wait_for_timeout(300)
    await open_space(p2, ts)
    shared_listed = await p2.locator("#tsShared [data-node='D-001']").count() == 1
    async with p2.expect_download() as info:
        await p2.click("#tsExport")
    dl = await info.value
    path = await dl.path(); data = json.loads(Path(path).read_text())
    titles = sorted(x["title"] for x in data["nodes"])
    plan_n = next(x for x in data["nodes"] if x["id"] == plan)
    hist = [e["kind"] for e in plan_n["history"]]
    files = data["files"]
    shared = data["shared_in"]
    ev_kinds = sorted({e["kind"] for e in data["events"]})
    raw_text = json.dumps(data)
    ok = (dl.suggested_filename.startswith(f"teamspace-{ts}") and data["format"] == "nodach.teamspace-export/1" and data["teamspace"]["id"] == ts
          and "Nassar's site notes" in titles and "Plan" in titles and "Quote" in titles and any(t.startswith("Money rules") for t in titles)
          and hist == ["item.create", "comment", "retract", "comment"]
          and files == [{"node": plan, "event": files[0]["event"], "asset": "a77", "name": "quote.pdf", "type": "application/pdf", "size": 1234, "where": None, "sealed": True}]
          and [s["id"] for s in shared] == ["D-001"] and set(shared[0]) == {"id", "code", "title", "link", "level", "shared_by"} and shared[0]["link"] == "studio/D-001"
          and "D-001" not in [x["id"] for x in data["nodes"]]
          and {"teamspace.create", "member.invite", "member.join", "member.role"} <= set(ev_kinds)
          and data["teamspace"]["members"] == {"P-morami49": "owner", "P-nassar": "member"} and no_export_for_member and shared_listed
          and not (p.errors + n2.errors + p2.errors))
    return ok, (f"file={dl.suggested_filename}, nodes={titles}, Plan history={hist}, files={[(f.get('asset'), f.get('name')) for f in files]}, "
                f"shared in={[(s['id'], s['link']) for s in shared]}, events={ev_kinds}, members={data['teamspace']['members']}, "
                f"member has no export={no_export_for_member}, errors={(p.errors + n2.errors + p2.errors)[:1]}")


async def s68_owner_opens_created_teamspace(b):
    """F-teamspace-open (22 Nov, staging): Nowed, the hub owner, creates a teamspace, then a second one with the same
    name, goes back to all teamspaces and clicks Open on the first. Its own page opens at the top of the screen (the
    list and the create form step aside), says which teamspace it is and that he is its owner, and a Back button
    returns to the list. Before the fix Open only redrew a card below the list, off to the side of where he looked,
    so nothing seemed to happen."""
    H = team_hub(); p = await open_teams_hub(b, H, "Morami49", True)
    first = await make_space(p, "Test space")
    back = await p.locator("#tsBack").count()
    if back: await p.click("#tsBack"); await p.wait_for_timeout(250)
    second = await make_space(p, "Test space")
    if await p.locator("#tsBack").count(): await p.click("#tsBack"); await p.wait_for_timeout(250)
    await to_spaces(p)
    rows = await p.locator("#tsList [data-tsopen]").count()
    await p.click(f"#tsList [data-tsopen='{first}']"); await p.wait_for_timeout(300)
    page = await p.evaluate("""() => { const h = document.querySelector('#tsTitle') || document.querySelector('#tsOne h3');
        const r = h && h.getBoundingClientRect(), list = document.querySelector('#tsCreateCard'), lr = list.getBoundingClientRect();
        return {title: h ? h.innerText : '', top: r ? Math.round(r.top) : null, vh: innerHeight,
                list_on_screen: !!(list.offsetParent && lr.bottom > 0 && lr.top < innerHeight), focused: document.activeElement === h}; }""")
    opened = page["title"].startswith("Test space") and first in page["title"] and "you are owner" in page["title"]
    in_view = page["top"] is not None and 0 <= page["top"] < page["vh"] / 2
    went_back = False
    if await p.locator("#tsBack").count():
        await p.click("#tsBack"); await p.wait_for_timeout(250)
        went_back = await p.locator("#tsCreateCard").is_visible() and await p.locator("#tsTitle").count() == 0
    ids_listed = first in await p.inner_text("#tsList") and second in await p.inner_text("#tsList")
    ok = opened and in_view and not page["list_on_screen"] and page["focused"] and went_back and rows == 2 and ids_listed and first != second and not p.errors
    return ok, (f"opened={opened} ({page['title'][:48]!r}), heading top={page['top']}px of {page['vh']}, list still on screen={page['list_on_screen']}, "
                f"focused={page['focused']}, back to list={went_back}, rows={rows}, ids listed={ids_listed}, errors={p.errors[:1]}")


async def s69_support_access(b):
    """114b a: Nassar has his own teamspace with a node in it. Nowed, the hub owner, isn't in it: Teamspaces lists it
    under Other teamspaces by name only and the node isn't on his record. He clicks Request support access, types why
    and clicks Open with support access: a support.access {teamspace, reason} event is written, the teamspace opens
    read only (no Add node, no Leave) with its node, and says it closes after 24 hours. Nassar, its owner, sees the
    entry with the reason in the teamspace's Support access log."""
    H = team_hub(); H["filter"] = True
    n = await open_teams_hub(b, H, "nassar", False)
    ts = await make_space(n, "Insydr internal")
    await n.fill("#tsNodeTitle", "Margins"); await n.click("#tsAddNode"); await n.wait_for_timeout(350)
    o = await open_teams_hub(b, H, "Morami49", True)
    await to_spaces(o); await o.wait_for_timeout(300)
    listed = ts in await o.inner_text("#tsOthers") and "Insydr internal" in await o.inner_text("#tsOthers")
    hidden_before = await o.evaluate("![...F.items.values()].some(i => i.title === 'Margins')")
    await o.click(f"[data-tssupport='{ts}']"); await o.click(f"[data-tssupgo='{ts}']"); await o.wait_for_timeout(200)
    asked_reason = "Say why" in await o.inner_text("#tsOtherNote")
    await o.fill(f"[data-tssupwhy='{ts}']", "Nassar asked for help with an import"); await o.click(f"[data-tssupgo='{ts}']"); await o.wait_for_timeout(900)
    sup = [x["event"] for x in H["posts"] if x["event"]["kind"] == "support.access"]
    title = await o.inner_text("#tsTitle") if await o.locator("#tsTitle").count() else ""
    nodes = await o.inner_text("#tsNodes") if await o.locator("#tsNodes").count() else ""
    read_only = await o.locator("#tsAddNode, #tsLeave, #tsInvite, #tsExport").count() == 0 and await o.locator("#tsSupportNote").count() == 1
    n2 = await open_teams_hub(b, H, "nassar", False)
    await open_space(n2, ts)
    log = await n2.inner_text("#tsSupportLog") if await n2.locator("#tsSupportLog").count() else ""
    ok = (listed and hidden_before and asked_reason and len(sup) == 1 and sup[0]["body"] == {"teamspace": ts, "reason": "Nassar asked for help with an import"}
          and "support access until" in title and "Margins" in nodes and read_only
          and "morami49" in log and "Nassar asked for help with an import" in log and "open now" in log and not (n.errors + o.errors + n2.errors))
    return ok, (f"listed by name={listed}, node hidden before={hidden_before}, reason required={asked_reason}, events={[e['body'] for e in sup]}, "
                f"title={title[:70]!r}, node readable={'Margins' in nodes}, read only={read_only}, owner's log={log.strip()[:90]!r}, errors={(n.errors + o.errors + n2.errors)[:1]}")

# ---------------------------------------------------------------- BQ-1 step 3: usage meter and free limits
REFUSAL = "Monthly free limit reached; a top-up will be available soon"


def meter(label, used, limit, enforced=True):
    pct = used * 100 // limit if limit else (100 if used else 0)
    return {"label": label, "used": used, "limit": limit, "pct": pct, "enforced": enforced, "refused": enforced and used >= limit}


def usage_view(login):
    """What the hub's /studio/api/usage gives (hub_core::usage::State::view_for): Nowed's Personal profile at 80% of
    AI calls, his Developer profile (exempt), and the Insydr pilot teamspace at its run limit."""
    lim = {"ai_calls": 500, "runs": 2000, "storage_bytes": 1000000000, "node": None, "version": 0}
    rows = [
        {"scope": f"P-{login.lower()}", "kind": "profile", "label": "Personal", "profile_kind": "personal", "active": True, "exempt": False, "notices": [],
         "meters": {"ai_calls": meter("AI calls", 400, 500), "runs": meter("flow runs", 120, 2000), "storage_bytes": meter("stored bytes", 52_000_000, 1_000_000_000, False)}},
        {"scope": "P-dev", "kind": "profile", "label": "Developer", "profile_kind": "developer", "active": False, "exempt": True, "notices": [],
         "meters": {"ai_calls": meter("AI calls", 900, 500), "runs": meter("flow runs", 10, 2000), "storage_bytes": meter("stored bytes", 1000, 1_000_000_000, False)}},
        {"scope": "TS-pilot", "kind": "teamspace", "label": "Insydr pilot", "role": "owner", "exempt": False, "notices": [],
         "meters": {"ai_calls": meter("AI calls", 20, 500), "runs": meter("flow runs", 2000, 2000), "storage_bytes": meter("stored bytes", 900_000_000, 1_000_000_000, False)}},
    ]
    return {"month": "2026-11", "limits": lim, "limits_node": "PLAN-limits", "refusal": REFUSAL, "warn_pct": 80, "meters": rows}


async def to_usage(p):
    await nav_open(p); await p.click("#sections [data-s=settings]"); await p.wait_for_timeout(120)
    await p.click("#subnav [data-sv=usage]"); await p.wait_for_timeout(500)


async def s70_usage_bars(b):
    """BQ-1 step 3: Nowed opens Settings → Usage. He sees one card per meter scope: his Personal profile first (this
    window's), with AI calls at 80% (amber, "notice sent"), his Developer profile marked exempt, and the Insydr pilot
    teamspace with runs at the limit (red, "refused"). The free limits read 500 AI calls, 2,000 runs, 1 GB (Nowed's
    decided limits, with no "placeholder" label), and the hub was asked as this window's profile."""
    H = team_hub(); H["usage"] = usage_view
    p = await open_teams_hub(b, H, "Morami49", True)
    await to_usage(p)
    scopes = await p.evaluate("[...document.querySelectorAll('#useMeters [data-scope]')].map(c => c.dataset.scope)")
    ai = await p.evaluate("document.querySelector('[data-scope=P-morami49] [data-meter=ai_calls] .ubar').className")
    ai_txt = await p.inner_text("[data-scope=P-morami49] [data-meter=ai_calls]")
    runs_cls = await p.evaluate("document.querySelector('[data-scope=TS-pilot] [data-meter=runs] .ubar').className")
    runs_txt = await p.inner_text("[data-scope=TS-pilot] [data-meter=runs]")
    dev = await p.inner_text("[data-scope=P-dev]")
    store_txt = await p.inner_text("[data-scope=TS-pilot] [data-meter=storage_bytes]")
    nums = await p.inner_text("#useNums"); placeholder = await p.locator("#usePlaceholder").count() == 0 and "placeholder" not in (await p.inner_text("#useLimits")).lower()
    hdr = H.get("usage_hdr", [])
    ok = (scopes == ["P-morami49", "P-dev", "TS-pilot"] and "warn" in ai and "400 / 500" in ai_txt and "notice sent" in ai_txt
          and "full" in runs_cls and "refused" in runs_txt and "exempt" in dev and "warn only" not in store_txt and "900.0 MB" in store_txt
          and "500 AI calls" in nums and "2,000 flow runs" in nums and "1 GB" in nums and placeholder and hdr and hdr[-1] == "P-morami49" and not p.errors)
    return ok, (f"cards={scopes}, AI bar={ai!r} {ai_txt.strip()!r}, team runs={runs_cls!r} {runs_txt.strip()!r}, dev exempt={'exempt' in dev}, "
                f"limits={nums!r} no placeholder label={placeholder}, asked as={hdr[-1:] }, errors={p.errors[:1]}")


async def s71_usage_notice_in_queue(b):
    """BQ-1 step 3: the hub raised an urgent notice for Nowed (AI calls at 80% in his Personal profile). It sits at the
    top of his My queue marked urgent, with a Usage button that opens the meters and Mark seen, which writes one status
    and takes it out of the queue. Nassar, whose queue it isn't, never sees it in his."""
    H = team_hub(); H["usage"] = usage_view
    notice = {"id": "USE-2026-11-ai_calls-80-P-morami49", "type": "notice", "title": "Usage: AI calls at 80% of the monthly free limit in profile Personal",
              "body": "400 of 500 AI calls used in 2026-11 (UTC).", "status": "open", "refs": ["FUN-1", "PLAN-limits"],
              "meta": {"queue": ["morami49"], "urgent": True, "lane": "owner", "usage": {"scope": "P-morami49", "meter": "ai_calls", "threshold": 80, "used": 400, "limit": 500, "month": "2026-11"}}}
    H["docs"].append({"doc_id": "Q-older", "event": {"ts": "2026-11-01T09:00:00.000Z", "kind": "item.create", "item": None, "branch": "main",
        "body": {"id": "Q-older", "type": "question", "title": "An older question", "status": "open"}, "author": {"id": "github:Morami49", "label": "Morami49", "role": "owner"}}})
    H["docs"].append({"doc_id": "hub-notice", "event": {"ts": "2026-11-22T10:00:00.000Z", "kind": "item.create", "item": None, "branch": "main", "body": notice,
        "author": {"id": "flow:runner", "label": "Flow runner (for usage meter)", "role": "agent"}, "via": "flow-runner"}})
    p = await open_teams_hub(b, H, "Morami49", True)
    await nav_open(p); await p.click("#sections [data-s=private]"); await p.wait_for_timeout(120)
    await p.click("#subnav [data-sv=queue]"); await p.wait_for_timeout(300)
    first = await p.evaluate("document.querySelector('#queueList li[data-q]')?.dataset.q")
    urgent = await p.locator(f"#queueList li[data-q='{notice['id']}'] [data-urgent]").count() == 1
    await p.click(f"#queueList li[data-q='{notice['id']}'] [data-qusage]"); await p.wait_for_timeout(400)
    to_usage_view = await p.evaluate("S.view") == "usage"
    await p.evaluate("show('queue')"); await p.wait_for_timeout(200)
    await p.click(f"#queueList li[data-q='{notice['id']}'] [data-qdone]"); await p.wait_for_timeout(400)
    st_ev = [x["event"] for x in H["posts"] if x["event"]["kind"] == "status"]
    gone = await p.locator(f"#queueList li[data-q='{notice['id']}']").count() == 0
    n = await open_teams_hub(b, H, "nassar", False)
    await n.evaluate("show('queue')"); await n.wait_for_timeout(250)
    not_his = await n.locator(f"#queueList li[data-q='{notice['id']}']").count() == 0
    ok = (first == notice["id"] and urgent and to_usage_view and len(st_ev) == 1 and st_ev[0]["item"] == notice["id"] and st_ev[0]["body"]["status"] == "done"
          and gone and not_his and not (p.errors + n.errors))
    return ok, (f"top of queue={first}, urgent={urgent}, Usage button opens meters={to_usage_view}, mark seen wrote={[(e['item'], e['body']) for e in st_ev]}, "
                f"left queue={gone}, not in Nassar's queue={not_his}, errors={(p.errors + n.errors)[:1]}")


async def s72_limits_change_is_a_proposal(b):
    """BQ-1 step 3: the limits are a node. Nowed (hub owner) clicks Create the limits node: PLAN-limits is made with the
    default limits (500, 2,000, 1 GB; no placeholder mark) and the card links to it. He then types 800 AI calls and clicks Propose
    new limits: on production that writes a change proposal (status proposed, target PLAN-limits) and the limits
    themselves stay as they were. Nassar, not an owner, gets no create or propose controls."""
    H = team_hub(); H["usage"] = usage_view
    p = await open_teams_hub(b, H, "Morami49", True)
    await to_usage(p)
    await p.click("#useMakeLimits"); await p.wait_for_timeout(500)
    made = next((x["event"] for x in H["posts"] if x["event"]["kind"] == "item.create" and x["event"]["body"].get("id") == "PLAN-limits"), None)
    val = json.loads(made["body"]["value"]) if made else {}
    linked = await p.locator("#useOpenLimits").count() == 1
    await p.fill("#useAi", "800"); await p.click("#usePropose"); await p.wait_for_timeout(500)
    cr = next((x["event"] for x in H["posts"] if x["event"]["kind"] == "item.create" and x["event"]["body"].get("type") == "change"), None)
    edits = [x for x in H["posts"] if x["event"]["kind"] == "item.edit" and x["event"].get("item") == "PLAN-limits"]
    still = await p.inner_text("#useNums")
    await p.click("#useOpenLimits"); await p.wait_for_timeout(300)
    opened = await p.evaluate("S.sel") == "PLAN-limits"
    n = await open_teams_hub(b, H, "nassar", False)
    await to_usage(n)
    no_controls = await n.locator("#useMakeLimits, #usePropose").count() == 0
    ok = (made and val == {"ai_calls": 500, "runs": 2000, "storage_bytes": 1000000000} and "placeholder" not in made["body"].get("body", "").lower() and made["body"].get("type") == "setting"
          and linked and cr and cr["body"]["status"] == "proposed" and cr["body"]["meta"]["target"] == "PLAN-limits"
          and json.loads(cr["body"]["meta"]["value"])["ai_calls"] == 800 and not edits and "500 AI calls" in still and opened and no_controls
          and not (p.errors + n.errors))
    return ok, (f"node made={bool(made)} value={val}, linked={linked}, proposal={cr and (cr['body']['status'], cr['body']['meta']['target'])}, "
                f"direct edits={len(edits)}, limits still={still!r}, opens node={opened}, no controls for Nassar={no_controls}, errors={(p.errors + n.errors)[:1]}")


async def s73_run_refused_at_limit(b):
    """BQ-1 step 3: at the monthly limit Nowed presses Run from here on a step. The hub refuses the run, and the step
    panel says so in the hub's words ("Monthly free limit reached; a top-up will be available soon"); Studio stays
    writable and nothing else is written by the page."""
    H = team_hub(); H["run"] = {"ok": False, "error": REFUSAL, "refused_limit": True, "scope": "P-morami49", "steps": []}
    p = await open_teams_hub(b, H, "Morami49", True)
    sid = "S-fetch1"
    await p.evaluate(f"write('flow.node', null, {{board: BOARD, id: '{sid}', step: 'fetch', title: 'Reed', config: {{url: 'https://www.reed.co.uk/api/1.0/search'}}, x: 0, y: 0}})")
    await p.wait_for_timeout(300)
    await p.evaluate(f"selectStep('{sid}')"); await p.wait_for_selector("#stRun", timeout=10000)
    before = len(H["posts"])
    await p.click("#stRun"); await p.wait_for_timeout(600)
    out = await p.inner_text("#stOut")
    still = await p.evaluate("S.canWrite !== false")
    ok = REFUSAL in out and "stopped" in out and len(H.get("runs", [])) == 1 and H["runs"][0].get("start") == sid and len(H["posts"]) == before and still and not p.errors
    return ok, f"panel says={out.strip()[:90]!r}, runs asked={len(H.get('runs', []))}, writes by the page={len(H['posts']) - before}, writable={still}, errors={p.errors[:1]}"


SCENARIOS = [s01_tired_misclick, s02_undo, s03_stable_codes, s04_past_view_lock, s05_branch_awareness, s06_view_only_client,
             s07_names, s08_long_history, s09_ai_relay, s10_keyboard_list, s11_offline_capture, s12_member_rights,
             s13_two_people_at_once, s14_phone, s15_undo_old_status, s16_overwhelm, s17_disagreement, s18_clock_skew, s19_private_area,
             s20_addresses, s21_money_settings, s22_duplicate_id, s23_same_order_everywhere,
             s24_hub_sync, s25_more_than_1000, s26_hub_not_connected, s27_read_on_open, s28_push_after_clean_runs, s29_runs_timeline, s30_builder_pipeline, s31_content_cycles, s32_private_files_and_views, s33_studio_edits_studio, s34_command_map, s35_history_words, s36_canvas, s37_classic_map_fallback, s38_runnable_steps, s39_scheduled_steps, s40_point_mode,
             s41_multi_point, s42_show_record_save, s43_show_pause, s44_show_replay, s45_my_queue,
             s46_live_session_stream, s47_live_browser_speech_opt_in, s48_live_member_lines,
             s49_show_pause_clock, s50_show_states, s51_show_marks_follow_canvas,
             s52_live_connector_reply_in_seconds, s53_live_states,
             s54_sign_out_switch_account, s55_nav_collapsed_expanded, s56_sections_reach_views, s57_esc_touch_keyboard, s58_classic_tabs,
             s59_dev_mode_proposes, s60_profile_new_window, s61_profile_strip_shown, s62_write_carries_profile, s63_refused_write_shows_message,
             s64_teamspace_create, s65_teamspace_invite, s66_guest_cant_see_unshared, s67_teamspace_export,
             s68_owner_opens_created_teamspace, s69_support_access,
             s70_usage_bars, s71_usage_notice_in_queue, s72_limits_change_is_a_proposal, s73_run_refused_at_limit]


async def main():
    results = []
    async with async_playwright() as pw:
        # STUDIO_CHROME: use an installed Chrome instead of Playwright's (e.g. where its download is blocked)
        b = await pw.chromium.launch(executable_path=os.environ["STUDIO_CHROME"]) if os.environ.get("STUDIO_CHROME") else await pw.chromium.launch()
        only = os.environ.get("STUDIO_ONLY")
        for sc in [x for x in SCENARIOS if not only or x.__name__.split("_")[0] in only.split(",")]:
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
