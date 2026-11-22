//! Clanstack hub on Cloudflare (H1, D-089). All rules come from hub-core / the engine (D-094);
//! this file only moves bytes between HTTP, the Durable Object's SQLite storage and hub-core.
//!
//! GET  /health                 → "ok"
//! GET  /status  (Bearer token) → counts + whether the whole chain verifies
//! GET  /pull    (Bearer token) → ?after=N&limit=M: events in this node's arrival order, from cursor N
//! Reads need a token too (F-23): the record is not public. HUB_READ_TOKEN reads; HUB_WRITE_TOKEN reads and writes.
//! POST /push    (Bearer token) → JSON array of signed engine events
//! POST /import  (Bearer token) → JSON array of {doc_id, event} from Studio Web
//! GET  /studio-export (Bearer token) → ?after=N&limit=M: {docs:[{doc_id,event}], next}: writes made on other nodes,
//!                       shaped as Studio Web events (Q44 "C now", D-101). /import skips them, so nothing loops.
//!
//! Claude connector (D-104, D-128): the hub is its own OAuth 2.1 server and MCP server; GitHub is the sign-in.
//! GET  /.well-known/oauth-protected-resource[/mcp]  RFC 9728 metadata
//! GET  /.well-known/oauth-authorization-server      RFC 8414 metadata
//! POST /register   RFC 7591 dynamic client registration (public clients)
//! GET  /authorize  checks Claude's request, then sends the person to GitHub
//! GET  /callback   GitHub returns here; allowed logins get a code and go back to Claude
//! POST /token      code + PKCE → tokens; refresh tokens rotate
//! POST /mcp        MCP JSON-RPC (Bearer token from /token); 401 + WWW-Authenticate starts sign-in
//!
//! Nodach Studio served by the hub (D-171 S1, Q21 A): GitHub sign-in, then a session cookie (hub_core::session).
//! GET  /studio              the Studio page (signed in) or a redirect to /studio/login
//! GET  /studio/login        choose GitHub, Google or an emailed link (Google and email accounts need an owner's invite)
//! GET  /studio/login/github sends the person to GitHub; GitHub returns to the shared /callback with a studio- state
//! GET  /studio/login/google sends the person to Google; Google returns to /studio/callback/google
//! POST /studio/login/email  {email}: emails a 15-minute sign-in link if the address was invited
//! GET  /studio/login/email?token=  shows a Sign in button (mail scanners open links before people do)
//! POST /studio/login/email/confirm   signs in; each link works once
//! GET  /studio/logout       clears the session
//! POST /studio/api/run      {board, start}: run the canvas flow from that step (hub_core::flows; D-174 C2)
//! GET  /studio/api/me       {login, id, owner}
//! GET  /studio/api/genesis  the genesis seed
//! GET  /studio/api/events   ?after=N&limit=M → {events:[{doc_id,event}], next, total} (same as the connector's read_events)
//! POST /studio/api/events   {doc_id, event}: one Studio event, imported with the importer key like Studio Web's sync.
//! GET  /studio/api/private  ?path=data/users/<you>/... → {events:[{doc_id,event}]}: your private items (I1, Q53 A)
//! POST /studio/api/private  {path, doc_id, event}: one private event, sealed with your own key (hub_core::private)
//! POST /studio/api/private/batch  {items:[{path, key, event}]}: up to 200 private events, checked whole (M1 d)
//! GET  /studio/import        the spreadsheet import page (reads .xlsx in the browser, sends batches above)
//! GET  /studio/api/private/export  every private item you own, decrypted, grouped by path (your backup)
//!                           Needs the x-nodach-studio header (cross-site pages cannot send it). Private items never come here.

use foundation_core::event::{Event, KeyPair};
use hub_core::oauth::{self, Config, OAuthState};
use hub_core::{flows, mcp, session, Hub};
use std::collections::HashMap;
use serde::Deserialize;
use std::cell::RefCell;
use worker::*;

/// The Studio page, its host shim and its genesis seed, built into the Worker (one file each in studio/).
const STUDIO_HTML: &str = include_str!("../../studio/index.html");
const STUDIO_SHIM: &str = include_str!("../../studio/standalone.js");
const SITE_HOME: &str = include_str!("../../site/index.html");
const SITE_PRIVACY: &str = include_str!("../../site/privacy.html");
const SITE_TERMS: &str = include_str!("../../site/terms.html");
const LOGIN_PAGE: &str = include_str!("../../studio/login.html");
const STUDIO_IMPORT: &str = include_str!("../../studio/import.html");
/// 6.1 a: personal settings (the vault) changed in the platform, not in code
const STUDIO_VAULT: &str = include_str!("../../studio/vault.html");
const XLSX_LITE: &str = include_str!("../../studio/xlsx-lite.js");
const STUDIO_GENESIS: &str = include_str!("../../studio/tests/genesis.json");
/// React + React Flow for the canvas (D-172), one pinned bundle (studio/vendor/package.json), so no CDN can mix versions.
const CANVAS_LIB: &str = include_str!("../../studio/vendor/canvas-lib.js");
const CANVAS_CSS: &str = include_str!("../../studio/vendor/xyflow.css");

#[event(fetch)]
async fn fetch(req: Request, env: Env, _ctx: Context) -> Result<Response> {
    if req.path() == "/health" {
        return Response::ok(format!("ok {}", mcp::BUILD));
    }
    // One record for now; later one Durable Object per space.
    let ns = env.durable_object("RECORD")?;
    let stub = ns.id_from_name("main")?.get_stub()?;
    stub.fetch_with_request(req).await
}

#[derive(Deserialize)]
struct Doc {
    doc_id: String,
    event: serde_json::Value,
}

#[durable_object]
pub struct Record {
    state: State,
    env: Env,
    hub: RefCell<Option<Hub>>,
    oauth: RefCell<Option<OAuthState>>,
}

fn random_hex(n: usize) -> Result<String> {
    let mut b = vec![0u8; n];
    getrandom::getrandom(&mut b).map_err(|e| Error::RustError(e.to_string()))?;
    Ok(hex::encode(b))
}

fn json_status(v: &serde_json::Value, status: u16) -> Result<Response> {
    let h = Headers::new();
    h.set("content-type", "application/json")?;
    h.set("cache-control", "no-store")?;
    Ok(Response::ok(v.to_string())?.with_status(status).with_headers(h))
}

fn page(title: &str, msg: &str, status: u16) -> Result<Response> {
    let esc = |t: &str| t.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    Ok(Response::from_html(format!(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>{t}</title>\
         <body style='font:16px system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'><h1>{t}</h1><p>{m}</p>",
        t = esc(title), m = esc(msg)))?.with_status(status))
}

/// Adds Set-Cookie headers. A redirect's headers are immutable on Workers, so this copies them into a fresh set.
fn with_cookies(r: Response, cookies: &[String]) -> Result<Response> {
    let h = Headers::new();
    for (k, v) in r.headers().entries() {
        h.append(&k, &v)?;
    }
    for c in cookies {
        h.append("set-cookie", c)?;
    }
    Ok(r.with_headers(h))
}

/// Only plain ids from a browser: letters, digits, '-' and '_', at most 100, and never the hub's own `hub-` ids.
fn valid_doc_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 100 && !id.starts_with("hub-") && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// `env` is "production" or "staging"; the page uses it and the build to show a staging ribbon and a reload banner.
/// M1 d: the spreadsheet import page. The .xlsx file is read in the browser (studio/xlsx-lite.js); rows go to
/// POST /studio/api/private/batch, so nothing but the chosen rows leaves the browser.
fn vault_page() -> Result<Response> {
    let html = format!(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">\
         <title>Nodach Studio · settings</title><style>body{{margin:0 auto;max-width:960px;padding:16px;font:14px system-ui}}\
         table{{border-collapse:collapse;margin:8px 0}}td,th{{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left}}.note{{color:#666;font-size:12px}}\
         button.link{{background:none;border:0;color:#06c;cursor:pointer;padding:0}}</style></head><body>{STUDIO_VAULT}</body></html>");
    let h = Headers::new();
    h.set("content-type", "text/html; charset=utf-8")?;
    h.set("cache-control", "no-store")?;
    h.set("referrer-policy", "no-referrer")?;
    h.set("x-content-type-options", "nosniff")?;
    Ok(Response::ok(html)?.with_headers(h))
}

fn import_page() -> Result<Response> {
    let html = format!(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">\
         <title>Nodach Studio · import</title><style>body{{margin:0 auto;max-width:960px;padding:16px;font:14px system-ui}}\
         table{{border-collapse:collapse;margin:8px 0}}td,th{{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left}}.note{{color:#666;font-size:12px}}</style>\
         <script>{XLSX_LITE}</script></head><body>{STUDIO_IMPORT}</body></html>");
    let h = Headers::new();
    h.set("content-type", "text/html; charset=utf-8")?;
    h.set("cache-control", "no-store")?;
    h.set("x-frame-options", "DENY")?;
    h.set("referrer-policy", "no-referrer")?;
    h.set("x-content-type-options", "nosniff")?;
    Ok(Response::ok(html)?.with_headers(h))
}

fn studio_page(env: &str) -> Result<Response> {
    let env = if env == "staging" { "staging" } else { "production" };
    let build = mcp::BUILD;
    let html = format!(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\
         <style>:root{{color-scheme:light}}body{{margin:0;font:14px system-ui}}img{{max-width:100%}}[hidden]{{display:none!important}}</style>\
         <script>window.__nodachBuild=\"{build}\";window.__nodachEnv=\"{env}\";</script><script>{STUDIO_SHIM}</script></head><body>{STUDIO_HTML}</body></html>");
    let h = Headers::new();
    h.set("content-type", "text/html; charset=utf-8")?;
    h.set("cache-control", "no-store")?;
    h.set("x-frame-options", "DENY")?;
    h.set("referrer-policy", "no-referrer")?;
    h.set("x-content-type-options", "nosniff")?;
    Ok(Response::ok(html)?.with_headers(h))
}

impl Record {
    fn sql(&self) -> SqlStorage {
        self.state.storage().sql()
    }

    fn load(&self) -> Result<()> {
        if self.hub.borrow().is_some() {
            return Ok(());
        }
        let sql = self.sql();
        sql.exec("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, json TEXT NOT NULL)", None)?;
        #[derive(Deserialize)]
        struct Row { json: String }
        let rows: Vec<Row> = sql.exec("SELECT json FROM events ORDER BY seq", None)?.to_array()?;
        let evs: Vec<Event> = rows.into_iter().filter_map(|r| serde_json::from_str(&r.json).ok()).collect();
        let (hub, r) = Hub::restore(evs);
        if !r.refused.is_empty() {
            console_error!("restore refused {} events", r.refused.len());
        }
        let mut hub = hub;
        hub.set_owners(self.owners());
        *self.hub.borrow_mut() = Some(hub);
        Ok(())
    }

    fn persist(&self, evs: &[Event]) -> Result<()> {
        let sql = self.sql();
        for e in evs {
            let json = serde_json::to_string(e).map_err(|e| Error::RustError(e.to_string()))?;
            sql.exec("INSERT OR IGNORE INTO events (id, json) VALUES (?, ?)", vec![e.id.clone().into(), json.into()])?;
        }
        Ok(())
    }

    fn has_token(&self, req: &Request, name: &str) -> bool {
        let want = match self.env.secret(name) { Ok(s) => s.to_string(), Err(_) => return false };
        let got = req.headers().get("authorization").ok().flatten().unwrap_or_default();
        !want.is_empty() && got == format!("Bearer {want}")
    }
    fn authorised(&self, req: &Request) -> bool {
        self.has_token(req, "HUB_WRITE_TOKEN")
    }
    fn can_read(&self, req: &Request) -> bool {
        self.authorised(req) || self.has_token(req, "HUB_READ_TOKEN")
    }

    fn cfg(&self, url: &Url) -> Config {
        let issuer = url.origin().ascii_serialization();
        let allowed = self.env.var("ALLOWED_GITHUB_LOGINS").map(|v| v.to_string()).unwrap_or_default();
        Config { issuer, allowed_logins: allowed.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect() }
    }

    fn load_oauth(&self) -> Result<()> {
        if self.oauth.borrow().is_some() { return Ok(()); }
        let sql = self.sql();
        sql.exec("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)", None)?;
        #[derive(Deserialize)]
        struct Row { v: String }
        let rows: Vec<Row> = sql.exec("SELECT v FROM kv WHERE k = 'oauth'", None)?.to_array()?;
        let st = rows.first().and_then(|r| serde_json::from_str(&r.v).ok()).unwrap_or_default();
        *self.oauth.borrow_mut() = Some(st);
        Ok(())
    }

    fn save_oauth(&self) -> Result<()> {
        let now = Date::now().as_millis() as i64;
        let v = { let mut b = self.oauth.borrow_mut(); let st = b.as_mut().unwrap(); st.purge(now); serde_json::to_string(&*st).map_err(|e| Error::RustError(e.to_string()))? };
        self.sql().exec("INSERT INTO kv (k, v) VALUES ('oauth', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", vec![v.into()])?;
        Ok(())
    }

    /// Q90 A: one row per connector request. Time, route, tool, outcome, duration, who, build. Never contents.
    fn log(&self, started: i64, route: &str, tool: &str, outcome: &str, who: &str) {
        let sql = self.sql();
        let _ = sql.exec("CREATE TABLE IF NOT EXISTS reqlog (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, route TEXT, tool TEXT, outcome TEXT, ms INTEGER, who TEXT, build TEXT)", None);
        let ms = Date::now().as_millis() as i64 - started;
        let _ = sql.exec("INSERT INTO reqlog (ts, route, tool, outcome, ms, who, build) VALUES (?, ?, ?, ?, ?, ?, ?)",
            vec![(started as f64).into(), route.into(), tool.into(), outcome.into(), (ms as f64).into(), who.into(), mcp::BUILD.into()]);
        // keep the newest 5000 rows
        let _ = sql.exec("DELETE FROM reqlog WHERE seq <= (SELECT MAX(seq) FROM reqlog) - 5000", None);
    }

    fn read_log(&self, limit: usize) -> Result<serde_json::Value> {
        let sql = self.sql();
        sql.exec("CREATE TABLE IF NOT EXISTS reqlog (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, route TEXT, tool TEXT, outcome TEXT, ms INTEGER, who TEXT, build TEXT)", None)?;
        #[derive(Deserialize, serde::Serialize)]
        struct Row { ts: f64, route: String, tool: String, outcome: String, ms: f64, who: String, build: String }
        let rows: Vec<Row> = sql.exec("SELECT ts, route, tool, outcome, ms, who, build FROM reqlog ORDER BY seq DESC LIMIT ?", vec![(limit as f64).into()])?.to_array()?;
        let rows: Vec<serde_json::Value> = rows.into_iter().map(|r| serde_json::json!({
            "time": mcp::iso(r.ts as i64), "route": r.route, "tool": r.tool, "outcome": r.outcome, "ms": r.ms as i64, "who": r.who, "build": r.build})).collect();
        Ok(serde_json::json!({ "entries": rows,
            "note": "ms counts only time spent waiting on the network or storage: Cloudflare freezes the clock while code computes (a timing-attack defence), so ms 0 means no waiting, not no work. 'not offered' means a client asked for a method this hub does not have; it is a probe, not a failure." }))
    }

    /// I1: the Private tier (A-spec-identity envelope). PRIVATE_SEED gives the key-encryption key; each owner has a
    /// random data key per version, stored only wrapped. If the secret was rotated, PRIVATE_SEED_PREVIOUS opens the
    /// old wrapping once and the key is re-wrapped with the current secret; rows are never re-encrypted.
    fn private_ready(&self) -> bool {
        self.secret_opt("PRIVATE_SEED").is_some()
    }

    fn private_tables(&self) -> Result<SqlStorage> {
        let sql = self.sql();
        sql.exec("CREATE TABLE IF NOT EXISTS private_keys (owner TEXT NOT NULL, version INTEGER NOT NULL, wrapped TEXT NOT NULL, PRIMARY KEY (owner, version))", None)?;
        sql.exec("CREATE TABLE IF NOT EXISTS private (seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, key_version INTEGER NOT NULL, path TEXT NOT NULL, doc_id TEXT NOT NULL, blob TEXT NOT NULL, UNIQUE(owner, path, doc_id))", None)?;
        Ok(sql)
    }

    fn nonce() -> Result<[u8; hub_core::private::NONCE_LEN]> {
        let mut n = [0u8; hub_core::private::NONCE_LEN];
        getrandom::getrandom(&mut n).map_err(|e| Error::RustError(e.to_string()))?;
        Ok(n)
    }

    /// The owner's data keys by version. `create` makes version 1 if the owner has none yet.
    fn owner_deks(&self, owner: &str, create: bool) -> Result<Vec<(u32, [u8; 32])>> {
        Ok(self.owner_keys(owner, create)?.0)
    }

    /// The owner's openable data keys by version, and how many versions can't be opened (19 Nov: a deploy replaced
    /// PRIVATE_SEED, so older keys stay stored but sealed). Those are never deleted: the old seed may yet be found.
    /// With `create`, a fresh version is added above every stored one when the newest can't be opened, so writes go on.
    fn owner_keys(&self, owner: &str, create: bool) -> Result<(Vec<(u32, [u8; 32])>, usize)> {
        use hub_core::private::{kek, unwrap_dek, wrap_dek};
        // SD1 a: PRIVATE_SEED_NEXT is the seed Nowed also keeps offline. When set, it wraps every key: new ones at once,
        // older ones the next time they're opened (the watch job's /reliability call opens them all).
        let cur = kek(&self.secret_opt("PRIVATE_SEED").ok_or_else(|| Error::RustError("PRIVATE_SEED is not set".into()))?);
        let next = self.secret_opt("PRIVATE_SEED_NEXT").filter(|s| s.trim().chars().count() >= 32).map(|s| kek(&s));
        let prev = self.secret_opt("PRIVATE_SEED_PREVIOUS").map(|s| kek(&s));
        let wrap_with = next.unwrap_or(cur);
        let o = owner.to_ascii_lowercase();
        let sql = self.private_tables()?;
        #[derive(Deserialize)]
        struct K { version: f64, wrapped: String }
        let rows: Vec<K> = sql.exec("SELECT version, wrapped FROM private_keys WHERE owner = ? ORDER BY version", vec![o.clone().into()])?.to_array()?;
        let (mut out, mut sealed, mut top) = (vec![], 0usize, 0u32);
        for r in rows {
            let v = r.version as u32;
            top = top.max(v);
            let on_target = unwrap_dek(&wrap_with, &o, v, &r.wrapped);
            let opened = on_target.map(|d| (d, false)).or_else(|| {
                [Some(cur), prev].into_iter().flatten().find_map(|k| unwrap_dek(&k, &o, v, &r.wrapped)).map(|d| (d, true))
            });
            match opened {
                Some((d, rewrap)) => {
                    if rewrap {   // rotation: re-wrap with the seed that wraps from now on
                        let w = wrap_dek(&wrap_with, &Self::nonce()?, &o, v, &d);
                        sql.exec("UPDATE private_keys SET wrapped = ? WHERE owner = ? AND version = ?", vec![w.into(), o.clone().into(), (v as f64).into()])?;
                    }
                    out.push((v, d));
                }
                None => sealed += 1,
            }
        }
        let newest_open = out.last().map(|(v, _)| *v == top).unwrap_or(false);
        if create && !newest_open {
            let v = top + 1;
            let mut d = [0u8; 32];
            getrandom::getrandom(&mut d).map_err(|e| Error::RustError(e.to_string()))?;
            let w = wrap_dek(&wrap_with, &Self::nonce()?, &o, v, &d);
            sql.exec("INSERT OR IGNORE INTO private_keys (owner, version, wrapped) VALUES (?, ?, ?)", vec![o.into(), (v as f64).into(), w.into()])?;
            out.push((v, d));
        }
        Ok((out, sealed))
    }

    /// Health of the stores a deploy can break (P1 a). Counts and flags only.
    fn reliability(&self) -> Result<serde_json::Value> {
        let seeds: serde_json::Map<String, serde_json::Value> = ["IMPORTER_SEED", "CONNECTOR_SEED", "PRIVATE_SEED", "PRIVATE_SEED_NEXT", "CONNECTOR_SEED_NEXT", "IMPORTER_SEED_NEXT"].iter()
            .map(|n| (n.to_string(), serde_json::Value::Bool(self.secret_opt(n).is_some()))).collect();
        let (mut rows, mut sealed, mut owners, mut sealed_keys) = (0u64, 0u64, 0u64, 0u64);
        if self.private_ready() {
            let sql = self.private_tables()?;
            #[derive(Deserialize)]
            struct O { owner: String, n: f64 }
            let os: Vec<O> = sql.exec("SELECT owner, COUNT(*) AS n FROM private GROUP BY owner", None)?.to_array()?;
            for o in os {
                owners += 1;
                rows += o.n as u64;
                let (open, bad) = self.owner_keys(&o.owner, false)?;
                sealed_keys += bad as u64;
                let open: std::collections::HashSet<u32> = open.into_iter().map(|(v, _)| v).collect();
                #[derive(Deserialize)]
                struct V { key_version: f64, n: f64 }
                let vs: Vec<V> = sql.exec("SELECT key_version, COUNT(*) AS n FROM private WHERE owner = ? GROUP BY key_version", vec![o.owner.clone().into()])?.to_array()?;
                sealed += vs.iter().filter(|v| !open.contains(&(v.key_version as u32))).map(|v| v.n as u64).sum::<u64>();
            }
        }
        let st = self.hub.borrow().as_ref().map(|h| serde_json::to_value(h.status()).unwrap_or_default()).unwrap_or_default();
        Ok(serde_json::json!({"build": mcp::BUILD, "seeds": seeds, "private": {"ready": self.private_ready(), "owners": owners,
            "rows": rows, "sealed_rows": sealed, "sealed_keys": sealed_keys}, "record": st}))
    }

    /// Where the owner's sealed rows sit (path, how many), so a lost key shows *what* is sealed, never the content.
    fn sealed_index(&self, owner: &str) -> Result<Vec<serde_json::Value>> {
        let open: std::collections::HashSet<u32> = self.owner_deks(owner, false)?.into_iter().map(|(v, _)| v).collect();
        let sql = self.private_tables()?;
        #[derive(Deserialize)]
        struct R { path: String, key_version: f64, n: f64 }
        let rows: Vec<R> = sql.exec("SELECT path, key_version, COUNT(*) AS n FROM private WHERE owner = ? GROUP BY path, key_version ORDER BY path",
            vec![owner.to_ascii_lowercase().into()])?.to_array()?;
        Ok(rows.into_iter().filter(|r| !open.contains(&(r.key_version as u32)))
            .map(|r| serde_json::json!({"path": r.path, "key_version": r.key_version as u32, "count": r.n as u64})).collect())
    }

    /// Store one private event. Ok(false) if that doc id is already there (a repeat is harmless).
    fn private_put(&self, owner: &str, path: &str, doc_id: &str, event: &serde_json::Value) -> Result<bool> {
        let o = owner.to_ascii_lowercase();
        let sql = self.private_tables()?;
        let keys = self.owner_deks(owner, true)?;
        #[derive(Deserialize)]
        struct E { key_version: f64 }
        let before: Vec<E> = sql.exec("SELECT key_version FROM private WHERE owner = ? AND path = ? AND doc_id = ?",
            vec![o.clone().into(), path.into(), doc_id.into()])?.to_array()?;
        if let Some(e) = before.first() {
            if keys.iter().any(|(v, _)| *v == e.key_version as u32) { return Ok(false); }   // a readable repeat
            // the old copy is sealed under a lost key: park it (kept whole, in case the old seed turns up), then store anew
            sql.exec("CREATE TABLE IF NOT EXISTS private_sealed (seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, key_version INTEGER, path TEXT, doc_id TEXT, blob TEXT)", None)?;
            sql.exec("INSERT INTO private_sealed (owner, key_version, path, doc_id, blob) SELECT owner, key_version, path, doc_id, blob FROM private WHERE owner = ? AND path = ? AND doc_id = ?",
                vec![o.clone().into(), path.into(), doc_id.into()])?;
            sql.exec("DELETE FROM private WHERE owner = ? AND path = ? AND doc_id = ?", vec![o.clone().into(), path.into(), doc_id.into()])?;
        }
        let (v, dek) = *keys.last().expect("created above");
        let plain = serde_json::to_vec(event).map_err(|e| Error::RustError(e.to_string()))?;
        let blob = hub_core::private::seal(&dek, &Self::nonce()?, &o, v, path, doc_id, &plain);
        sql.exec("INSERT OR IGNORE INTO private (owner, key_version, path, doc_id, blob) VALUES (?, ?, ?, ?, ?)",
            vec![o.into(), (v as f64).into(), path.into(), doc_id.into(), blob.into()])?;
        Ok(true)
    }

    /// An owner's private events, oldest first, as {doc_id, event, path}. `path` None means every path.
    /// Rows that fail to open (tampering, a lost key) are counted, never returned.
    fn private_list(&self, owner: &str, path: Option<&str>) -> Result<(Vec<serde_json::Value>, usize)> {
        let deks: std::collections::HashMap<u32, [u8; 32]> = self.owner_deks(owner, false)?.into_iter().collect();
        let sql = self.private_tables()?;
        #[derive(Deserialize)]
        struct Row { key_version: f64, path: String, doc_id: String, blob: String }
        let o = owner.to_ascii_lowercase();
        let rows: Vec<Row> = match path {
            Some(p) => sql.exec("SELECT key_version, path, doc_id, blob FROM private WHERE owner = ? AND path = ? ORDER BY seq", vec![o.clone().into(), p.into()])?,
            None => sql.exec("SELECT key_version, path, doc_id, blob FROM private WHERE owner = ? ORDER BY seq", vec![o.clone().into()])?,
        }.to_array()?;
        let (mut out, mut unreadable) = (vec![], 0);
        for r in rows {
            let v = r.key_version as u32;
            let ev = deks.get(&v).and_then(|d| hub_core::private::open(d, &o, v, &r.path, &r.doc_id, &r.blob))
                .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
            match ev {
                Some(ev) => out.push(serde_json::json!({"doc_id": r.doc_id, "event": ev, "path": r.path})),
                None => unreadable += 1,
            }
        }
        Ok((out, unreadable))
    }

    fn secret_opt(&self, name: &str) -> Option<String> {
        self.env.secret(name).ok().map(|s| s.to_string()).filter(|s| !s.is_empty())
    }

    async fn github_login(&self, code: &str, redirect_uri: &str) -> Result<String> {
        let (id, secret) = match (self.secret_opt("GH_OAUTH_CLIENT_ID"), self.secret_opt("GH_OAUTH_CLIENT_SECRET")) {
            (Some(i), Some(s)) => (i, s),
            _ => return Err(Error::RustError("GitHub sign-in is not set up yet".into())),
        };
        let h = Headers::new();
        h.set("accept", "application/json")?;
        h.set("content-type", "application/x-www-form-urlencoded")?;
        h.set("user-agent", "clanstack-hub")?;
        let body = format!("client_id={}&client_secret={}&code={}&redirect_uri={}", oauth::enc(&id), oauth::enc(&secret), oauth::enc(code), oauth::enc(redirect_uri));
        let mut init = RequestInit::new();
        init.with_method(Method::Post).with_headers(h).with_body(Some(worker::wasm_bindgen::JsValue::from_str(&body)));
        let mut r = Fetch::Request(Request::new_with_init("https://github.com/login/oauth/access_token", &init)?).send().await?;
        let tok: serde_json::Value = r.json().await?;
        let at = tok.get("access_token").and_then(|v| v.as_str()).ok_or_else(|| Error::RustError("GitHub refused the sign-in".into()))?.to_string();
        let h = Headers::new();
        h.set("authorization", &format!("Bearer {at}"))?;
        h.set("accept", "application/vnd.github+json")?;
        h.set("user-agent", "clanstack-hub")?;
        let mut init = RequestInit::new();
        init.with_method(Method::Get).with_headers(h);
        let mut r = Fetch::Request(Request::new_with_init("https://api.github.com/user", &init)?).send().await?;
        let user: serde_json::Value = r.json().await?;
        user.get("login").and_then(|v| v.as_str()).map(String::from).ok_or_else(|| Error::RustError("GitHub returned no login".into()))
    }

    fn runner_key(&self) -> Result<KeyPair> {
        let s = self.env.secret(self.active_seed("CONNECTOR_SEED"))?.to_string();
        Ok(KeyPair::from_seed(flows::runner_seed(&s)))
    }

    /// The hub's owners (A1 d): OWNER_GITHUB_LOGINS, else the first allowed login.
    fn owners(&self) -> Vec<String> {
        let read = |k: &str| self.env.var(k).map(|v| v.to_string()).unwrap_or_default();
        let list = |v: String| v.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect::<Vec<_>>();
        let o = list(read("OWNER_GITHUB_LOGINS"));
        if !o.is_empty() { return o; }
        list(read("ALLOWED_GITHUB_LOGINS")).into_iter().take(1).collect()
    }

    /// V1 d: a value from `login`'s vault (data/users/<login>/vault/vars, encrypted with their own key); scheduled
    /// runs use the first owner's vault. The latest `vault.set` for the name wins; `vault.unset` removes it.
    fn vault_get(&self, login: &str, name: &str) -> std::result::Result<String, String> {
        let owner = if login == "schedule" { self.owners().into_iter().next().unwrap_or_default() } else { login.to_string() };
        if owner.is_empty() || !self.private_ready() { return Err(format!("no vault available for {name}")); }
        let path = format!("data/users/{owner}/vault/vars");
        let mut rows = vec![];
        for p in [path.clone(), format!("data/users/github:{owner}/vault/vars")] {
            rows.extend(self.private_list(&owner, Some(&p)).map_err(|e| e.to_string())?.0);
        }
        let mut val: Option<String> = None;
        for r in rows {
            let ev = &r["event"];
            // name and value may sit at the top of the event or in its body (the connector's write_private shape)
            let field = |k: &str| ev.get(k).or_else(|| ev.pointer(&format!("/body/{k}"))).and_then(|v| v.as_str()).map(str::to_string);
            if field("name").as_deref() != Some(name) { continue; }
            match ev.get("kind").and_then(|v| v.as_str()) {
                Some("vault.set") => val = field("value"),
                Some("vault.unset") => val = None,
                _ => {}
            }
        }
        val.filter(|v| !v.is_empty()).ok_or_else(|| format!("{name} isn't in your vault yet (Private tier path {path}, a vault.set event with name {name})"))
    }

    fn node_secret(&self, name: &str) -> std::result::Result<String, String> {
        let var = flows::secret_var(name).ok_or_else(|| format!("{name:?} isn't a usable secret name"))?;
        self.secret_opt(&var).ok_or_else(|| format!("the hub has no secret {var} yet (add it in GitHub → Settings → Secrets, then deploy)"))
    }

    /// Writes one Studio event as the flow runner, persists it, and returns its doc id.
    fn runner_write(&self, kind: &str, item: Option<&str>, body: serde_json::Value, login: &str, now: i64) -> std::result::Result<String, String> {
        let key = self.runner_key().map_err(|e| e.to_string())?;
        let ev = serde_json::json!({"ts": mcp::iso(now), "kind": kind, "item": item, "branch": "main", "body": body,
            "author": {"id": "flow:runner", "label": format!("Flow runner (for {login})"), "role": "agent"}, "via": "flow-runner"});
        let made = self.hub.borrow_mut().as_mut().unwrap().write(&key, "flow runner", ev, now).map_err(|e| format!("{e:?}"))?;
        self.persist(&made).map_err(|e| e.to_string())?;
        Ok(hub_core::studio_doc_id(&made.last().map(|e| e.id.clone()).unwrap_or_default()))
    }

    async fn step_fetch(&self, cfg: &serde_json::Value) -> std::result::Result<Vec<serde_json::Value>, String> {
        let mut url = cfg.get("url").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        if !flows::url_allowed(&url) {
            return Err("the address must start with https:// and can't be this hub".into());
        }
        let h = Headers::new();
        let e = |x: worker::Error| x.to_string();
        h.set("accept", "application/json").map_err(e)?;
        h.set("user-agent", "nodach-hub").map_err(e)?;
        let auths: Vec<serde_json::Value> = match cfg.get("auth") { Some(serde_json::Value::Array(a)) => a.clone(), Some(o) if o.is_object() => vec![o.clone()], _ => vec![] };
        for a in auths {
            let secret = self.node_secret(a.get("secret").and_then(|v| v.as_str()).unwrap_or(""))?;
            match a.get("type").and_then(|v| v.as_str()).unwrap_or("bearer") {
                "basic" => h.set("authorization", &format!("Basic {}", flows::base64(format!("{secret}:").as_bytes()))).map_err(e)?,
                "bearer" => h.set("authorization", &format!("Bearer {secret}")).map_err(e)?,
                "header" => h.set(a.get("header").and_then(|v| v.as_str()).unwrap_or("x-api-key"), &secret).map_err(e)?,
                "query" => {
                    let p = a.get("param").and_then(|v| v.as_str()).unwrap_or("key");
                    url.push(if url.contains('?') { '&' } else { '?' });
                    url.push_str(&format!("{}={}", oauth::enc(p), oauth::enc(&secret)));
                }
                t => return Err(format!("unknown sign-in type {t:?}")),
            }
        }
        let mut init = RequestInit::new();
        init.with_method(Method::Get).with_headers(h);
        let req = Request::new_with_init(&url, &init).map_err(e)?;
        let mut r = Fetch::Request(req).send().await.map_err(e)?;
        let status = r.status_code();
        if !(200..300).contains(&status) {
            let text = r.text().await.unwrap_or_default();
            let why = serde_json::from_str::<serde_json::Value>(&text).ok()
                .and_then(|v| ["/message", "/error/message", "/error", "/detail"].iter()
                    .find_map(|p| v.pointer(p).and_then(|m| m.as_str()).map(str::to_string)))
                .unwrap_or_else(|| text.split_whitespace().collect::<Vec<_>>().join(" "));
            let why: String = why.chars().take(160).collect();
            return Err(if why.is_empty() { format!("the site answered {status}") } else { format!("the site answered {status}: {why}") });
        }
        let body: serde_json::Value = r.json().await.map_err(|_| "the site didn't answer with JSON".to_string())?;
        Ok(flows::as_items(body, cfg.get("list").and_then(|v| v.as_str()).unwrap_or("")))
    }

    async fn step_ai(&self, cfg: &serde_json::Value, items: Vec<serde_json::Value>, login: &str) -> std::result::Result<Vec<serde_json::Value>, String> {
        let key = self.node_secret(cfg.get("secret").and_then(|v| v.as_str()).unwrap_or("GEMINI"))?;
        let model = cfg.get("model").and_then(|v| v.as_str()).unwrap_or("gemini-flash-latest");
        if !model.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'.') {
            return Err("model names are letters, digits, '-' and '.'".into());
        }
        let prompt = cfg.get("prompt").and_then(|v| v.as_str()).unwrap_or("");
        let set = cfg.get("set").and_then(|v| v.as_str()).unwrap_or("ai").to_string();
        let max = cfg.get("max_items").and_then(|v| v.as_u64()).unwrap_or(10).min(20) as usize;
        // Private values for the prompt only (flows::with_vars), e.g. the owner's home area kept out of the record.
        let mut vars = serde_json::Map::new();
        if let Some(o) = cfg.get("vars").and_then(|v| v.as_object()) {
            for (k, sec) in o.iter().take(5) {
                // V1 d: personal values live in the run owner's vault (their Private tier), never in GitHub secrets
                let name = sec.as_str().unwrap_or("");
                let val = self.vault_get(login, name)?;
                vars.insert(k.clone(), serde_json::json!(val));
            }
        }
        // Busy models (429/500/503) are retried after a pause, then the lighter model is tried (F-75): Gemini's
        // free tier answers "high demand" often enough that one refusal shouldn't stop a whole flow.
        let mut models = vec![model.to_string()];
        if model != "gemini-flash-lite-latest" { models.push("gemini-flash-lite-latest".into()); }
        let mut out = vec![];
        let mut used: Option<String> = None;
        for (i, mut it) in items.into_iter().enumerate() {
            if i < max {
                let body = serde_json::json!({"contents": [{"parts": [{"text": flows::render(prompt, &flows::with_vars(&it, &vars))}]}]}).to_string();
                let mut last = String::new();
                let mut text = None;
                'models: for m in models.iter().skip_while(|m| used.as_ref().is_some_and(|u| u != *m)) {
                    for attempt in 0..2 {
                        if attempt > 0 { worker::Delay::from(std::time::Duration::from_millis(1500)).await; }
                        match self.ai_call(m, &key, &body).await {
                            Ok(t) => { text = Some(t); used = Some(m.clone()); break 'models; }
                            Err((status, msg)) => {
                                last = format!("the AI ({m}) answered {status}: {msg}");
                                if !matches!(status, 429 | 500 | 502 | 503 | 504) { return Err(last); }
                            }
                        }
                    }
                }
                let text = text.ok_or(format!("{last} (tried again and with the lighter model; try later)"))?;
                if let Some(m) = it.as_object_mut() { m.insert(set.clone(), serde_json::json!(text)); }
            }
            out.push(it);
        }
        Ok(out)
    }

    async fn ai_call(&self, model: &str, key: &str, body: &str) -> std::result::Result<String, (u16, String)> {
        let url = format!("https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent");
        let e = |x: worker::Error| (0u16, x.to_string());
        let h = Headers::new();
        h.set("content-type", "application/json").map_err(e)?;
        h.set("x-goog-api-key", key).map_err(e)?;
        let mut init = RequestInit::new();
        init.with_method(Method::Post).with_headers(h).with_body(Some(worker::wasm_bindgen::JsValue::from_str(body)));
        let mut r = Fetch::Request(Request::new_with_init(&url, &init).map_err(e)?).send().await.map_err(e)?;
        let status = r.status_code();
        let v: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
        if !(200..300).contains(&status) {
            return Err((status, v.pointer("/error/message").and_then(|m| m.as_str()).unwrap_or("").to_string()));
        }
        Ok(v.pointer("/candidates/0/content/parts/0/text").and_then(|t| t.as_str()).unwrap_or("").trim().to_string())
    }

    async fn step_post(&self, cfg: &serde_json::Value, items: Vec<serde_json::Value>, login: &str, now: i64) -> std::result::Result<Vec<serde_json::Value>, String> {
        let item = cfg.get("item").and_then(|v| v.as_str()).unwrap_or("LOBBY-income").trim().to_string();
        if item.is_empty() || item.len() > 80 || !item.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
            return Err("post to an item id made of letters, digits, '-' and '_'".into());
        }
        let text = cfg.get("text").and_then(|v| v.as_str()).unwrap_or("{{title}}");
        let max = cfg.get("max_items").and_then(|v| v.as_u64()).unwrap_or(30).min(50) as usize;
        let known = self.hub.borrow().as_ref().unwrap().knows_item(&item);
        if !known {
            let title = cfg.get("item_title").and_then(|v| v.as_str()).unwrap_or("Income lobby");
            self.runner_write("item.create", None, serde_json::json!({"id": item, "type": "asset", "title": title,
                "body": "Where flows post what they find (D-174). Reply on each line: apply / skip / ?", "status": "open", "refs": ["D-174"]}), login, now)?;
        }
        // "Only new" (S3): with `once` set (e.g. {{jobId}}), an item whose key was already posted here is skipped,
        // read from this item's own comments, so the record shows exactly what was posted when.
        let once = cfg.get("once").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        let mut seen = if once.is_empty() { Default::default() } else { flows::posted_keys(&self.all_events(), &item) };
        let mut posted = vec![];
        for it in items.into_iter() {
            if posted.len() >= max { break; }
            let mut body = serde_json::json!({"text": flows::render(text, &it), "source": "flow"});
            if !once.is_empty() {
                let k = flows::render(&once, &it);
                if k.is_empty() || !seen.insert(k.clone()) { continue; }
                body["key"] = serde_json::json!(k);
            }
            self.runner_write("comment", Some(&item), body, login, now)?;
            posted.push(it);
        }
        Ok(posted)
    }

    /// Runs the canvas flow on `board` from `start`, as the hub reads it from its own record.
    fn all_events(&self) -> Vec<serde_json::Value> {
        let hub = self.hub.borrow();
        let (docs, _, _, _) = hub.as_ref().unwrap().studio_events(0, usize::MAX);
        docs.into_iter().filter_map(|d| d.get("event").cloned()).collect()
    }

    /// The hub's hourly wake-up (S3): Durable Object alarms need no cron, route or account. Set if missing.
    async fn ensure_alarm(&self) -> Result<()> {
        let st = self.state.storage();
        if st.get_alarm().await?.is_none() {
            let now = Date::now().as_millis() as i64;
            st.set_alarm(3_600_000 - now.rem_euclid(3_600_000) + 60_000).await?;   // a minute past the next hour
        }
        Ok(())
    }

    async fn run_flow(&self, board: &str, start: &str, login: &str, now: i64, slot: Option<&str>) -> Result<serde_json::Value> {
        let evs = self.all_events();
        let flow = flows::fold(&evs, board);
        let order = match flows::plan(&flow, start) {
            Ok(o) => o,
            Err(e) => return Ok(serde_json::json!({"ok": false, "error": e, "steps": []})),
        };
        let mut outs: HashMap<String, Vec<serde_json::Value>> = HashMap::new();
        let (mut steps, mut failed) = (vec![], false);
        for id in &order {
            let node = flow.nodes[id].clone();
            if failed {
                steps.push(serde_json::json!({"id": id, "step": node.step, "skipped": true}));
                continue;
            }
            let input: Vec<serde_json::Value> = flows::inputs_of(&flow, id).iter()
                .flat_map(|i| outs.get(i).cloned().unwrap_or_default()).take(flows::MAX_ITEMS).collect();
            let got = match node.step.as_str() {
                "fetch" => self.step_fetch(&node.config).await,
                "filter" => flows::filter(input, &node.config),
                "calc" => flows::calc(input, &node.config),
                "ai" => self.step_ai(&node.config, input, login).await,
                "post" => self.step_post(&node.config, input, login, now).await,
                other => Err(format!("unknown step {other}")),
            };
            match got {
                Ok(items) => {
                    steps.push(serde_json::json!({"id": id, "step": node.step, "ok": true, "count": items.len(), "sample": flows::sample(&items, 3)}));
                    outs.insert(id.clone(), items);
                }
                Err(e) => { failed = true; steps.push(serde_json::json!({"id": id, "step": node.step, "ok": false, "error": e})); }
            }
        }
        let mut summary = serde_json::json!({"board": board, "start": start, "ok": !failed, "by": login, "steps": steps});
        if let Some(sl) = slot { summary["slot"] = serde_json::json!(sl); }
        let _ = self.runner_write("flow.run", None, summary.clone(), login, now);
        Ok(summary)
    }

    /// SD1 a: a root seed is 64 hex characters (as made before 19 Nov) or any passphrase of 32+ characters made in
    /// Nowed's password manager, hashed to 32 bytes. Existing hex seeds keep giving exactly the same keys.
    fn seed_bytes(&self, name: &str) -> Result<[u8; 32]> {
        let s = self.env.secret(name)?.to_string();
        let t = s.trim();
        if let Some(b) = hex::decode(t).ok().and_then(|v| <[u8; 32]>::try_from(v).ok()) { return Ok(b); }
        if t.chars().count() < 32 { return Err(Error::RustError(format!("{name} must be 64 hex characters or a passphrase of 32+ characters"))); }
        use sha2::Digest;
        let mut h = sha2::Sha256::new();
        h.update(b"nodach-seed-v1\0"); h.update(name.as_bytes()); h.update(b"\0"); h.update(t.as_bytes());
        Ok(h.finalize().into())
    }

    /// ES1 3a, the two-key window: when `<NAME>_NEXT` holds a 32+ character passphrase (kept offline in Nowed's
    /// Bitwarden), it signs and mints from now on, while the old seed still verifies what it made. Sessions last 30
    /// days, so after 30 days nothing depends on the old one and nobody was ever signed out.
    fn active_seed(&self, base: &'static str) -> &'static str {
        let next: &'static str = match base { "CONNECTOR_SEED" => "CONNECTOR_SEED_NEXT", "IMPORTER_SEED" => "IMPORTER_SEED_NEXT", _ => return base };
        if self.secret_opt(next).map(|s| s.trim().chars().count() >= 32).unwrap_or(false) { next } else { base }
    }

    fn importer_key(&self) -> Result<KeyPair> {
        Ok(KeyPair::from_seed(self.seed_bytes(self.active_seed("IMPORTER_SEED"))?))
    }

    fn connector_key(&self) -> Result<KeyPair> {
        Ok(KeyPair::from_seed(self.seed_bytes(self.active_seed("CONNECTOR_SEED"))?))
    }

    /// The key new sessions and sign-in links are minted with.
    fn session_key(&self) -> Option<[u8; 32]> {
        self.secret_opt(self.active_seed("CONNECTOR_SEED")).map(|s| session::session_key(&s))
    }

    /// Every key a session or link may have been minted with: the active one first, then the old one.
    fn session_keys(&self) -> Vec<[u8; 32]> {
        let mut ks: Vec<[u8; 32]> = self.session_key().into_iter().collect();
        if let Some(old) = self.secret_opt("CONNECTOR_SEED").map(|s| session::session_key(&s)) { if !ks.contains(&old) { ks.push(old); } }
        ks
    }

    fn verify_link_any(&self, token: &str, now: i64) -> Option<String> {
        self.session_keys().iter().find_map(|k| session::verify_link(k, token, now))
    }

    fn signed_in(&self, req: &Request, cfg: &Config, now: i64) -> Option<String> {
        let header = req.headers().get("cookie").ok().flatten()?;
        let c = session::cookie_value(&header, session::SESSION_COOKIE)?;
        let allowed = self.sign_in_allowed(cfg);
        self.session_keys().iter().find_map(|k| session::verify(k, &c, now, &allowed))
    }

    /// Marks an emailed sign-in link as used; false if it already was. Only a hash of the token is kept, until it expires.
    fn use_link_once(&self, token: &str, now: i64) -> Result<bool> {
        let sql = self.state.storage().sql();
        sql.exec("CREATE TABLE IF NOT EXISTS used_links (h TEXT PRIMARY KEY, exp INTEGER NOT NULL)", None)?;
        sql.exec("DELETE FROM used_links WHERE exp < ?", vec![(now as f64).into()])?;
        let h = hub_core::private::checksum(&serde_json::json!(token));
        #[derive(Deserialize)]
        struct N { n: f64 }
        let seen: Vec<N> = sql.exec("SELECT COUNT(*) AS n FROM used_links WHERE h = ?", vec![h.clone().into()])?.to_array()?;
        if seen.first().map(|r| r.n > 0.0).unwrap_or(false) { return Ok(false); }
        sql.exec("INSERT INTO used_links (h, exp) VALUES (?, ?)", vec![h.into(), ((now + session::LINK_TTL_MS) as f64).into()])?;
        Ok(true)
    }

    /// Who may sign in to Studio: the GitHub allow-list plus email accounts an owner invited (18 Nov).
    fn sign_in_allowed(&self, cfg: &Config) -> Vec<String> {
        let _ = self.load();
        let mut list = cfg.allowed_logins.clone();
        if let Some(h) = self.hub.borrow().as_ref() { list.extend(h.access().invited_logins()); }
        list
    }

    /// The login an email sign-in gets (F-two-accounts-one-person): the account the email is linked to with
    /// account.link, written in that account's casing from the allow-list, else the email's own `e-<hash16>` login.
    /// `hash` is the full SHA-256 when known (Google, the email form); the emailed link carries only `e_login`.
    fn email_sign_in_login(&self, cfg: &Config, hash: Option<&str>, e_login: &str) -> String {
        let _ = self.load();
        let linked = self.hub.borrow().as_ref().and_then(|h| {
            let p = h.profiles();
            match hash { Some(x) => p.linked_login_for_email_hash(x), None => Some(p.sign_in_login_for_email_login(e_login)) }
        });
        match linked {
            Some(l) if !l.eq_ignore_ascii_case(e_login) => cfg.allowed_logins.iter().find(|a| a.eq_ignore_ascii_case(&l)).cloned().unwrap_or(l),
            _ => e_login.to_string(),
        }
    }

    /// Ends a sign-in: mint the session for `login` if they may sign in, then go to Studio.
    fn finish_sign_in(&self, cfg: &Config, login: &str, now: i64, method: &str) -> Result<Option<(Response, String)>> {
        if !session::allowed(login, &self.sign_in_allowed(cfg)) {
            let msg = if method == "github" { format!("{login} is not allowed to use this Studio yet.") }
                      else { "That email address hasn't been invited to this Studio yet. Ask the person who invited you which address they used.".to_string() };
            let r = page("Not allowed", &msg, 403)?;
            return Ok(Some((with_cookies(r, &[session::state_set_cookie(None)])?, login.to_string())));
        }
        let Some(value) = self.session_key().and_then(|k| session::mint(&k, login, now)) else {
            return Ok(Some((page("Not set up yet", "The hub has no signing seed yet; deploy once more.", 503)?, "-".into())));
        };
        let r = Response::redirect(Url::parse(&format!("{}/studio", cfg.issuer))?)?;
        Ok(Some((with_cookies(r, &[session::session_set_cookie(Some(&value)), session::state_set_cookie(None)])?, login.to_string())))
    }

    /// Google sign-in: exchange the code, then read the verified email from Google's userinfo endpoint.
    async fn google_email(&self, code: &str, redirect_uri: &str) -> Result<String> {
        let (id, secret) = match (self.secret_opt("GOOGLE_OAUTH_CLIENT_ID"), self.secret_opt("GOOGLE_OAUTH_CLIENT_SECRET")) {
            (Some(i), Some(s)) => (i, s),
            _ => return Err(Error::RustError("Google sign-in is not set up yet".into())),
        };
        let h = Headers::new();
        h.set("content-type", "application/x-www-form-urlencoded")?;
        let body = format!("code={}&client_id={}&client_secret={}&redirect_uri={}&grant_type=authorization_code",
            oauth::enc(code), oauth::enc(&id), oauth::enc(&secret), oauth::enc(redirect_uri));
        let mut init = RequestInit::new();
        init.with_method(Method::Post).with_headers(h).with_body(Some(worker::wasm_bindgen::JsValue::from_str(&body)));
        let mut r = Fetch::Request(Request::new_with_init("https://oauth2.googleapis.com/token", &init)?).send().await?;
        let tok: serde_json::Value = r.json().await?;
        let at = tok.get("access_token").and_then(|v| v.as_str()).ok_or_else(|| Error::RustError("Google refused the sign-in".into()))?.to_string();
        let h = Headers::new();
        h.set("authorization", &format!("Bearer {at}"))?;
        let mut init = RequestInit::new();
        init.with_method(Method::Get).with_headers(h);
        let mut r = Fetch::Request(Request::new_with_init("https://openidconnect.googleapis.com/v1/userinfo", &init)?).send().await?;
        let user: serde_json::Value = r.json().await?;
        if user.get("email_verified").and_then(|v| v.as_bool()) != Some(true) {
            return Err(Error::RustError("Google says that email address isn't verified".into()));
        }
        user.get("email").and_then(|v| v.as_str()).map(String::from).ok_or_else(|| Error::RustError("Google returned no email".into()))
    }

    /// Sends the one-time sign-in link through Resend (RESEND_API_KEY secret, MAIL_FROM var).
    async fn send_sign_in_link(&self, to: &str, link: &str) -> std::result::Result<(), String> {
        let text = format!("Here's your link to sign in to Nodach Studio. It works once, for 15 minutes.\n\n{link}\n\nIf you didn't ask for this, you can ignore this email.");
        self.send_mail(to, "Your Nodach Studio sign-in link", &text).await
    }

    /// Plain-text email through Resend (RESEND_API_KEY secret, MAIL_FROM var).
    async fn send_mail(&self, to: &str, subject: &str, text: &str) -> std::result::Result<(), String> {
        let key = self.secret_opt("RESEND_API_KEY").ok_or("email isn't set up yet (RESEND_API_KEY)")?;
        let from = self.env.var("MAIL_FROM").map(|v| v.to_string()).unwrap_or_default();
        if from.is_empty() { return Err("email isn't set up yet (MAIL_FROM)".into()); }
        let body = serde_json::json!({"from": from, "to": [to], "subject": subject, "text": text}).to_string();
        let h = Headers::new();
        h.set("authorization", &format!("Bearer {key}")).map_err(|e| e.to_string())?;
        h.set("content-type", "application/json").map_err(|e| e.to_string())?;
        let mut init = RequestInit::new();
        init.with_method(Method::Post).with_headers(h).with_body(Some(worker::wasm_bindgen::JsValue::from_str(&body)));
        let req = Request::new_with_init("https://api.resend.com/emails", &init).map_err(|e| e.to_string())?;
        let r = Fetch::Request(req).send().await.map_err(|e| e.to_string())?;
        if r.status_code() >= 300 { return Err(format!("the mail service answered {}", r.status_code())); }
        Ok(())
    }

    /// Nodach Studio routes; None = not a Studio route (the shared /callback falls through for the connector).
    async fn studio(&self, req: &mut Request, url: &Url) -> Result<Option<(Response, String)>> {
        let path = req.path();
        let cfg = self.cfg(url);
        let now = Date::now().as_millis() as i64;
        if path == "/callback" {
            let q = oauth::parse_form(url.query().unwrap_or(""));
            let Some(state) = q.get("state").filter(|s| s.starts_with(session::STATE_PREFIX)) else { return Ok(None) };
            let cookie = req.headers().get("cookie").ok().flatten().and_then(|h| session::cookie_value(&h, session::STATE_COOKIE));
            if !session::state_matches(state, cookie.as_deref()) {
                return Ok(Some((page("Sign-in expired", "Open /studio and sign in again.", 400)?, "-".into())));
            }
            let Some(code) = q.get("code") else {
                return Ok(Some((page("Sign-in cancelled", "GitHub didn't return a sign-in code. Open /studio to try again.", 400)?, "-".into())));
            };
            let login = match self.github_login(code, &format!("{}/callback", cfg.issuer)).await {
                Ok(l) => l,
                Err(e) => return Ok(Some((page("Sign-in failed", &e.to_string(), 502)?, "-".into()))),
            };
            // a GitHub account named like an email account can't take that account over
            if login.to_ascii_lowercase().starts_with(session::EMAIL_LOGIN_PREFIX) && !session::allowed(&login, &cfg.allowed_logins) {
                return Ok(Some((page("Not allowed", "That GitHub account can't sign in here.", 403)?, "-".into())));
            }
            return self.finish_sign_in(&cfg, &login, now, "github");
        }
        if path == "/studio/callback/google" {
            let q = oauth::parse_form(url.query().unwrap_or(""));
            let cookie = req.headers().get("cookie").ok().flatten().and_then(|h| session::cookie_value(&h, session::STATE_COOKIE));
            if !q.get("state").is_some_and(|s| session::state_matches(s, cookie.as_deref())) {
                return Ok(Some((page("Sign-in expired", "Open /studio and sign in again.", 400)?, "-".into())));
            }
            let Some(code) = q.get("code") else {
                return Ok(Some((page("Sign-in cancelled", "Google didn't return a sign-in code. Open /studio to try again.", 400)?, "-".into())));
            };
            let email = match self.google_email(code, &format!("{}/studio/callback/google", cfg.issuer)).await {
                Ok(e) => e,
                Err(e) => return Ok(Some((page("Sign-in failed", &e.to_string(), 502)?, "-".into()))),
            };
            let login = self.email_sign_in_login(&cfg, Some(&session::email_hash(&email)), &session::email_login(&email));
            return self.finish_sign_in(&cfg, &login, now, "google");
        }
        let method = req.method();
        match (method.clone(), path.as_str()) {
            // DN1 d: nodach.com is the public front page; studio./staging. open Studio at their root
            (Method::Get, "/" | "/privacy" | "/terms") => {
                let host = url.host_str().unwrap_or("").to_ascii_lowercase();
                if path == "/" && (host.starts_with("studio.") || host.starts_with("staging.")) {
                    return Ok(Some((Response::redirect(Url::parse(&format!("{}/studio", cfg.issuer))?)?, "-".into())));
                }
                let body = match path.as_str() { "/privacy" => SITE_PRIVACY, "/terms" => SITE_TERMS, _ => SITE_HOME };
                let h = Headers::new();
                h.set("content-type", "text/html; charset=utf-8")?;
                h.set("cache-control", "public, max-age=300")?;
                h.set("x-content-type-options", "nosniff")?;
                h.set("x-frame-options", "DENY")?;
                Ok(Some((Response::ok(body)?.with_headers(h), "-".into())))
            }
            (Method::Get, "/studio/login") => {
                let h = Headers::new();
                h.set("content-type", "text/html; charset=utf-8")?;
                h.set("cache-control", "no-store")?;
                h.set("x-frame-options", "DENY")?;
                Ok(Some((Response::ok(LOGIN_PAGE)?.with_headers(h), "-".into())))
            }
            (Method::Get, "/studio/login/google") => {
                let Some(gid) = self.secret_opt("GOOGLE_OAUTH_CLIENT_ID") else {
                    return Ok(Some((page("Not set up yet", "Google sign-in isn't configured on this hub yet.", 503)?, "-".into())));
                };
                let state = format!("{}{}", session::STATE_PREFIX, random_hex(16)?);
                let to = format!("https://accounts.google.com/o/oauth2/v2/auth?client_id={}&redirect_uri={}&response_type=code&scope=openid%20email&state={}&prompt=select_account",
                    oauth::enc(&gid), oauth::enc(&format!("{}/studio/callback/google", cfg.issuer)), state);
                let r = Response::redirect(Url::parse(&to)?)?;
                Ok(Some((with_cookies(r, &[session::state_set_cookie(Some(&state))])?, "-".into())))
            }
            (Method::Post, "/studio/login/email") => {
                // Always the same answer, invited or not, so the form can't be used to test addresses.
                let form = oauth::parse_form(&req.text().await.unwrap_or_default());
                let email = form.get("email").cloned().unwrap_or_default();
                let done = page("Check your email", "If that address has been invited, a sign-in link is on its way. It works for 15 minutes.", 200)?;
                if !session::plausible_email(&email) {
                    return Ok(Some((page("That doesn't look like an email address", "Go back and check it.", 400)?, "-".into())));
                }
                let login = session::email_login(&email);
                // a linked email may sign in even if its e- account was never invited (it opens the linked account)
                let resolved = self.email_sign_in_login(&cfg, Some(&session::email_hash(&email)), &login);
                if session::allowed(&resolved, &self.sign_in_allowed(&cfg)) {
                    if let Some(token) = self.session_key().and_then(|k| session::mint_link(&k, &login, now)) {
                        let link = format!("{}/studio/login/email?token={}", cfg.issuer, oauth::enc(&token));
                        let outcome = match self.send_sign_in_link(email.trim(), &link).await { Ok(()) => "sent", Err(e) => { console_error!("sign-in link: {e}"); "failed" } };
                        self.log(now, "/studio/login/email", "email link", outcome, &login);
                    }
                }
                Ok(Some((done, "-".into())))
            }
            (Method::Get, "/studio/login/email") => {
                // Mail scanners (e.g. Microsoft Safe Links) open links before people do, so opening the link only
                // shows a button; signing in happens on the POST, and each link works once.
                let token = url.query_pairs().find(|(k, _)| k == "token").map(|(_, v)| v.to_string()).unwrap_or_default();
                if self.verify_link_any(&token, now).is_none() {
                    return Ok(Some((page("Link expired", "Sign-in links work once, for 15 minutes. Open /studio/login to get a new one.", 400)?, "-".into())));
                }
                let html = format!("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\"><title>Sign in · Nodach Studio</title>\
                    <style>body{{font:16px system-ui;max-width:26rem;margin:3rem auto;padding:0 1rem}}button{{width:100%;padding:.8rem;border-radius:8px;border:1px solid #222;background:#222;color:#fff;font:inherit;cursor:pointer}}</style></head>\
                    <body><h1>Sign in to Nodach Studio</h1><form method=post action=\"/studio/login/email/confirm\"><input type=hidden name=token value=\"{}\"><button type=submit>Sign in</button></form></body></html>",
                    token.replace('&', "&amp;").replace('"', "&quot;").replace('<', "&lt;"));
                let h = Headers::new();
                h.set("content-type", "text/html; charset=utf-8")?;
                h.set("cache-control", "no-store")?;
                h.set("referrer-policy", "no-referrer")?;
                h.set("x-frame-options", "DENY")?;
                Ok(Some((Response::ok(html)?.with_headers(h), "-".into())))
            }
            (Method::Post, "/studio/login/email/confirm") => {
                let form = oauth::parse_form(&req.text().await.unwrap_or_default());
                let token = form.get("token").cloned().unwrap_or_default();
                let Some(login) = self.verify_link_any(&token, now) else {
                    return Ok(Some((page("Link expired", "Sign-in links work once, for 15 minutes. Open /studio/login to get a new one.", 400)?, "-".into())));
                };
                if !self.use_link_once(&token, now)? {
                    return Ok(Some((page("Link already used", "Each sign-in link works once. Open /studio/login to get a new one.", 400)?, "-".into())));
                }
                let login = self.email_sign_in_login(&cfg, None, &login);
                self.finish_sign_in(&cfg, &login, now, "email")
            }
            (Method::Get, "/studio/login/github") => {
                let Some(gh_id) = self.secret_opt("GH_OAUTH_CLIENT_ID") else {
                    return Ok(Some((page("Not set up yet", "The hub's GitHub sign-in isn't configured yet (D-128).", 503)?, "-".into())));
                };
                let state = format!("{}{}", session::STATE_PREFIX, random_hex(16)?);
                let to = format!("https://github.com/login/oauth/authorize?client_id={}&redirect_uri={}&state={}&scope=read:user&allow_signup=false",
                    oauth::enc(&gh_id), oauth::enc(&format!("{}/callback", cfg.issuer)), state);
                let r = Response::redirect(Url::parse(&to)?)?;
                Ok(Some((with_cookies(r, &[session::state_set_cookie(Some(&state))])?, "-".into())))
            }
            // Open-source libraries, no record data: served without sign-in so the page can load them before anything else.
            (Method::Get, "/studio/vendor/canvas-lib.js" | "/studio/vendor/xyflow.css") => {
                let (body, ct) = if path.ends_with(".js") { (CANVAS_LIB, "text/javascript; charset=utf-8") } else { (CANVAS_CSS, "text/css; charset=utf-8") };
                let h = Headers::new();
                h.set("content-type", ct)?;
                h.set("cache-control", "public, max-age=3600")?;
                h.set("x-content-type-options", "nosniff")?;
                Ok(Some((Response::ok(body)?.with_headers(h), "-".into())))
            }
            (Method::Get, "/studio/logout") => {
                let r = page("Signed out", "You are signed out of Nodach Studio on this browser.", 200)?;
                Ok(Some((with_cookies(r, &[session::session_set_cookie(None)])?, "-".into())))
            }
            (_, p) if p == "/studio" || p == "/studio/" || p == "/studio/import" || p == "/studio/vault" || p.starts_with("/studio/api/") => {
                let login = self.signed_in(req, &cfg, now);
                let who = login.clone().unwrap_or_else(|| "-".into());
                let env = self.env.var("NODACH_ENV").map(|v| v.to_string()).unwrap_or_default();
                let r = match (method, p, login) {
                    (Method::Get, "/studio" | "/studio/", None) => Response::redirect(Url::parse(&format!("{}/studio/login", cfg.issuer))?)?,
                    (Method::Get, "/studio" | "/studio/", Some(_)) => studio_page(&env)?,
                    (Method::Get, "/studio/import", None) => Response::redirect(Url::parse(&format!("{}/studio/login", cfg.issuer))?)?,
                    (Method::Get, "/studio/import", Some(_)) => import_page()?,
                    (Method::Get, "/studio/vault", None) => Response::redirect(Url::parse(&format!("{}/studio/login", cfg.issuer))?)?,
                    (Method::Get, "/studio/vault", Some(_)) => vault_page()?,
                    (Method::Post, "/studio/api/private/batch", Some(l)) => {
                        // M1 d: the import page's batches; same whole-batch checks and checksums as the connector's tool
                        if req.headers().get(session::CSRF_HEADER).ok().flatten().as_deref() != Some("1") {
                            json_status(&serde_json::json!({"error": "missing x-nodach-studio header"}), 403)?
                        } else if !self.private_ready() {
                            json_status(&serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), 503)?
                        } else {
                            let args: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                            match hub_core::private::plan_batch_via(&l, &args, "studio-import") {
                                Err(errs) => json_status(&serde_json::json!({"error": "batch rejected; nothing was stored", "reasons": errs}), 400)?,
                                Ok((items, whole)) => {
                                    let mut rows = vec![];
                                    let mut added_n = 0;
                                    for it in &items {
                                        let added = self.private_put(&l, &it.path, &it.doc_id, &it.event)?;
                                        if added { added_n += 1; }
                                        rows.push(serde_json::json!({"key": it.key, "doc_id": it.doc_id, "checksum": it.checksum, "repeat": !added}));
                                    }
                                    self.log(now, "/studio/api/private/batch", "private batch", &format!("ok {added_n}/{}", items.len()), &l);
                                    json_status(&serde_json::json!({"count": items.len(), "new": added_n, "repeats": items.len() - added_n,
                                        "batch_checksum": whole, "items": rows}), 200)?
                                }
                            }
                        }
                    }
                    (_, _, None) => json_status(&serde_json::json!({"error": "sign in at /studio/login"}), 401)?,
                    (Method::Get, "/studio/api/me", Some(l)) => {
                        // A1 d: owners see everything; a member sees the spaces they were given
                        let acc = self.hub.borrow().as_ref().unwrap().access();
                        let email_account = l.to_ascii_lowercase().starts_with(session::EMAIL_LOGIN_PREFIX);
                        json_status(&serde_json::json!({"login": l, "id": if email_account { format!("email:{l}") } else { format!("github:{l}") },
                            "label": acc.label_of(&l), "owner": acc.is_owner(&l), "spaces": acc.grants_of(&l)}), 200)?
                    }
                    (Method::Get, "/studio/api/profiles", Some(l)) => {
                        // I2: your account, profiles, connections (refs only) and findings; never someone else's
                        let v = self.hub.borrow().as_ref().unwrap().profiles().view_for(&l);
                        json_status(&v, 200)?
                    }
                    (Method::Get, "/studio/api/genesis", Some(l)) if !self.hub.borrow().as_ref().unwrap().access().is_owner(&l) => {
                        json_status(&serde_json::json!({"label": "genesis", "items": []}), 200)?
                    }
                    (Method::Get, "/studio/api/genesis", Some(_)) => {
                        let h = Headers::new();
                        h.set("content-type", "application/json")?;
                        h.set("cache-control", "no-store")?;
                        Response::ok(STUDIO_GENESIS)?.with_headers(h)
                    }
                    (Method::Post, "/studio/api/link-sign-in", Some(l)) => {
                        // F-two-accounts-one-person: "also sign in as me with this email". Owner only; the address is
                        // hashed here and dropped; the record gets account.link {identity: email:sha256:<hash>}.
                        if req.headers().get(session::CSRF_HEADER).ok().flatten().as_deref() != Some("1") {
                            json_status(&serde_json::json!({"error": "missing x-nodach-studio header"}), 403)?
                        } else {
                            let body: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                            let email = body.get("email").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let unlink = body.get("unlink").and_then(|v| v.as_bool()).unwrap_or(false);
                            let key = self.connector_key()?;
                            let before = self.hub.borrow().as_ref().unwrap().status().events;
                            let res = hub_core::mcp::link_sign_in(self.hub.borrow_mut().as_mut().unwrap(), &l, &key, &email, unlink, "studio-settings", now);
                            let added = self.hub.borrow().as_ref().unwrap().pull(before, usize::MAX).events;
                            self.persist(&added)?;
                            self.log(now, "/studio/api/link-sign-in", if unlink { "unlink sign-in" } else { "link sign-in" }, if res.is_ok() { "ok" } else { "refused" }, &l);
                            match res {
                                Ok(v) => json_status(&v, 200)?,
                                Err(e) => json_status(&serde_json::json!({"error": e, "refused": true}), 403)?,
                            }
                        }
                    }
                    (Method::Get, "/studio/api/events", Some(l)) => {
                        let q = |k: &str| url.query_pairs().find(|(a, _)| a == k).and_then(|(_, v)| v.parse::<usize>().ok());
                        let (events, next, total, _) = self.hub.borrow().as_ref().unwrap().studio_events_for(&l, q("after").unwrap_or(0), q("limit").unwrap_or(500).clamp(1, 2000));
                        json_status(&serde_json::json!({"events": events, "next": next, "total": total}), 200)?
                    }
                    (Method::Post, "/studio/api/run", Some(l)) => {
                        if !self.hub.borrow().as_ref().unwrap().access().is_owner(&l) {
                            json_status(&serde_json::json!({"error": "running flows is for owners for now"}), 403)?
                        } else if req.headers().get(session::CSRF_HEADER).ok().flatten().as_deref() != Some("1") {
                            json_status(&serde_json::json!({"error": "missing x-nodach-studio header"}), 403)?
                        } else {
                            let body: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                            let board = body.get("board").and_then(|v| v.as_str()).unwrap_or("main").to_string();
                            let start = body.get("start").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let res = self.run_flow(&board, &start, &l, now, None).await?;
                            self.log(now, "/studio/api/run", "run", if res.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) { "ok" } else { "failed" }, &l);
                            json_status(&res, 200)?
                        }
                    }
                    (Method::Post, "/studio/api/events", Some(l)) => {
                        if req.headers().get(session::CSRF_HEADER).ok().flatten().as_deref() != Some("1") {
                            json_status(&serde_json::json!({"error": "missing x-nodach-studio header"}), 403)?
                        } else {
                            let body: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                            let doc_id = body.get("doc_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let ev = body.get("event").cloned().unwrap_or(serde_json::Value::Null);
                            let ok_ev = ev.get("kind").and_then(|v| v.as_str()).is_some() && ev.get("ts").and_then(|v| v.as_str()).is_some();
                            let kind = ev.get("kind").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let subject = hub_core::access::subject(&ev);
                            let allowed = self.hub.borrow().as_ref().unwrap().access().check_write(&l, &kind, subject.as_deref());
                            if !valid_doc_id(&doc_id) || !ok_ev {
                                json_status(&serde_json::json!({"error": "need {doc_id, event:{kind, ts, ...}}"}), 400)?
                            } else if let Err(e) = allowed {
                                json_status(&serde_json::json!({"error": e, "refused": true}), 403)?
                            } else if kind == "account.link" && !ev.pointer("/author/id").and_then(|v| v.as_str()).is_some_and(|a| a.eq_ignore_ascii_case(&format!("github:{l}"))) {
                                // a sign-in link must be written by the account it links to (F-two-accounts-one-person)
                                json_status(&serde_json::json!({"error": "account.link must be written by the signed-in account itself", "refused": true}), 403)?
                            } else {
                                let importer = self.importer_key()?;
                                let made = self.hub.borrow_mut().as_mut().unwrap().import(&importer, vec![(doc_id.clone(), ev)], now)
                                    .map_err(|e| Error::RustError(format!("{e:?}")))?;
                                self.persist(&made)?;
                                let n = made.iter().filter(|e| e.u.kind == hub_core::IMPORT_KIND).count();
                                self.log(now, "/studio/api/events", "write", "ok", &l);
                                json_status(&serde_json::json!({"id": doc_id, "imported": n}), 200)?
                            }
                        }
                    }
                    (Method::Get, "/studio/api/private/export", Some(l)) => {
                        if !self.private_ready() {
                            json_status(&serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), 503)?
                        } else {
                            let (rows, unreadable) = self.private_list(&l, None)?;
                            self.log(now, "/studio/api/private/export", "export", "ok", &l);
                            json_status(&serde_json::json!({"owner": l, "exported_at": mcp::iso(now), "events": rows, "unreadable": unreadable}), 200)?
                        }
                    }
                    (Method::Get, "/studio/api/private", Some(l)) => {
                        let path = url.query_pairs().find(|(a, _)| a == "path").map(|(_, v)| v.to_string()).unwrap_or_default();
                        if !hub_core::private::path_owned_by(&path, &l) {
                            json_status(&serde_json::json!({"error": "that private path isn't yours"}), 403)?
                        } else if !self.private_ready() {
                            json_status(&serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), 503)?
                        } else {
                            let (rows, unreadable) = self.private_list(&l, Some(&path))?;
                            { let sealed = if unreadable > 0 { self.sealed_index(&l).unwrap_or_default() } else { vec![] };
                              json_status(&serde_json::json!({"events": rows, "unreadable": unreadable, "sealed_index": sealed}), 200)? }
                        }
                    }
                    (Method::Post, "/studio/api/private", Some(l)) => {
                        if req.headers().get(session::CSRF_HEADER).ok().flatten().as_deref() != Some("1") {
                            json_status(&serde_json::json!({"error": "missing x-nodach-studio header"}), 403)?
                        } else {
                            let body: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                            let path = body.get("path").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let doc_id = body.get("doc_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                            let ev = body.get("event").cloned().unwrap_or(serde_json::Value::Null);
                            let size = serde_json::to_vec(&ev).map(|v| v.len()).unwrap_or(usize::MAX);
                            let ok_ev = ev.get("kind").and_then(|v| v.as_str()).is_some() && ev.get("ts").and_then(|v| v.as_str()).is_some();
                            if !hub_core::private::path_owned_by(&path, &l) {
                                json_status(&serde_json::json!({"error": "that private path isn't yours"}), 403)?
                            } else if !valid_doc_id(&doc_id) || !ok_ev {
                                json_status(&serde_json::json!({"error": "need {path, doc_id, event:{kind, ts, ...}}"}), 400)?
                            } else if size > hub_core::private::MAX_EVENT_BYTES {
                                json_status(&serde_json::json!({"error": "private event too large (64 KB max); use an encrypted file instead"}), 413)?
                            } else if !self.private_ready() {
                                json_status(&serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), 503)?
                            } else {
                                let added = self.private_put(&l, &path, &doc_id, &ev)?;
                                self.log(now, "/studio/api/private", "private write", if added { "ok" } else { "repeat" }, &l);
                                json_status(&serde_json::json!({"id": doc_id, "stored": added}), 200)?
                            }
                        }
                    }
                    _ => Response::error("not found", 404)?,
                };
                Ok(Some((r, who)))
            }
            _ => Ok(None),
        }
    }

    /// Connector routes; None = not a connector route.
    async fn connector(&self, req: &mut Request, url: &Url) -> Result<Option<Response>> {
        let cfg = self.cfg(url);
        let now = Date::now().as_millis() as i64;
        let path = req.path();
        let resp = match (req.method(), path.as_str()) {
            (Method::Get, "/.well-known/oauth-protected-resource" | "/.well-known/oauth-protected-resource/mcp") => json_status(&cfg.protected_resource_metadata(), 200)?,
            (Method::Get, "/.well-known/oauth-authorization-server" | "/.well-known/oauth-authorization-server/mcp") => json_status(&cfg.authorization_server_metadata(), 200)?,
            (Method::Post, "/register") => {
                self.load_oauth()?;
                let body: serde_json::Value = req.json().await.unwrap_or(serde_json::Value::Null);
                let r = self.oauth.borrow_mut().as_mut().unwrap().register(&body, random_hex(16)?, now);
                match r { Ok(v) => { self.save_oauth()?; json_status(&v, 201)? } Err(e) => json_status(&e.body(), e.status)? }
            }
            (Method::Get, "/authorize") => {
                self.load_oauth()?;
                let Some(gh_id) = self.secret_opt("GH_OAUTH_CLIENT_ID") else {
                    return Ok(Some(page("Not set up yet", "The hub's GitHub sign-in isn't configured yet (D-128).", 503)?));
                };
                let params = oauth::parse_form(url.query().unwrap_or(""));
                let upstream = random_hex(16)?;
                let r = self.oauth.borrow_mut().as_mut().unwrap().authorize(&cfg, &params, upstream.clone(), now);
                match r {
                    Ok(()) => {
                        self.save_oauth()?;
                        let to = format!("https://github.com/login/oauth/authorize?client_id={}&redirect_uri={}&state={}&scope=read:user&allow_signup=false",
                            oauth::enc(&gh_id), oauth::enc(&format!("{}/callback", cfg.issuer)), upstream);
                        Response::redirect(Url::parse(&to)?)?
                    }
                    Err(e) => page("Can't start sign-in", &e.description, e.status)?,
                }
            }
            (Method::Get, "/callback") => {
                self.load_oauth()?;
                let q = oauth::parse_form(url.query().unwrap_or(""));
                let (Some(code), Some(state)) = (q.get("code"), q.get("state")) else {
                    return Ok(Some(page("Sign-in cancelled", "GitHub didn't return a sign-in code. Start again from Claude.", 400)?));
                };
                let login = match self.github_login(code, &format!("{}/callback", cfg.issuer)).await {
                    Ok(l) => l,
                    Err(e) => return Ok(Some(page("Sign-in failed", &e.to_string(), 502)?)),
                };
                let our_code = random_hex(32)?;
                let r = self.oauth.borrow_mut().as_mut().unwrap().complete(&cfg, state, &login, &our_code, now);
                self.save_oauth()?;
                match r {
                    Ok(to) => Response::redirect(Url::parse(&to)?)?,
                    Err(e) => page("Not allowed", &e.description, e.status)?,
                }
            }
            (Method::Post, "/token") => {
                self.load_oauth()?;
                let params = oauth::parse_form(&req.text().await.unwrap_or_default());
                let r = self.oauth.borrow_mut().as_mut().unwrap().token(&params, &random_hex(32)?, &random_hex(32)?, now);
                self.save_oauth()?;
                match r { Ok(v) => json_status(&v, 200)?, Err(e) => json_status(&e.body(), e.status)? }
            }
            (Method::Post, "/mcp") => {
                self.load_oauth()?;
                let bearer = req.headers().get("authorization").ok().flatten();
                let mut login = self.oauth.borrow().as_ref().unwrap().authenticate(bearer.as_deref(), now);
                // F-46 follow-up: the deploy's smoke test drives /mcp with HUB_WRITE_TOKEN (already full-access via /push)
                if login.is_none() && self.authorised(req) { login = Some("service:deploy-check".into()); }
                let Some(login) = login else {
                    self.log(now, "/mcp", "-", "401 sign-in needed", "-");
                    let h = Headers::new();
                    h.set("www-authenticate", &cfg.www_authenticate())?;
                    h.set("content-type", "application/json")?;
                    return Ok(Some(Response::ok(r#"{"error":"invalid_token"}"#)?.with_status(401).with_headers(h)));
                };
                let msg: serde_json::Value = match req.json().await {
                    Ok(v) => v,
                    Err(_) => return Ok(Some(json_status(&serde_json::json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}), 400)?)),
                };
                let key = self.connector_key()?;
                let before = self.hub.borrow().as_ref().unwrap().status().events;
                let msgs: Vec<serde_json::Value> = match &msg { serde_json::Value::Array(b) => b.clone(), m => vec![m.clone()] };
                let mut replies: Vec<serde_json::Value> = vec![];
                for m in &msgs {
                    let method = m.get("method").and_then(|v| v.as_str()).unwrap_or("-").to_string();
                    let tool = if method == "tools/call" { m.pointer("/params/name").and_then(|v| v.as_str()).unwrap_or("-").to_string() } else { method.clone() };
                    let r = if tool == "hub_log" && m.get("id").is_some() {
                        let limit = m.pointer("/params/arguments/limit").and_then(|v| v.as_u64()).unwrap_or(50).clamp(1, 200) as usize;
                        // logged before reading, so the call that asks for the log appears in it
                        self.log(now, "/mcp", &tool, "ok", &login);
                        let v = self.read_log(limit)?;
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": v.to_string()}], "structuredContent": v, "isError": false}}))
                    } else if tool == "read_private" && m.get("id").is_some() {
                        // I1 + Q53 A: the connector is a grantee of its signed-in owner's Private tier (Sealed stays unreadable)
                        let on = self.env.var("CONNECTOR_PRIVATE_READ").map(|v| v.to_string()).unwrap_or_default() == "on";
                        let path = m.pointer("/params/arguments/path").and_then(|v| v.as_str()).map(str::to_string);
                        let v = if !on {
                            mcp::tool_result(serde_json::json!({"error": "private reading is switched off for the connector on this hub (CONNECTOR_PRIVATE_READ)"}), true)
                        } else if !self.private_ready() {
                            mcp::tool_result(serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), true)
                        } else if path.as_deref().map(|p| !hub_core::private::path_owned_by(p, &login)).unwrap_or(false) {
                            mcp::tool_result(serde_json::json!({"error": "that private path isn't yours"}), true)
                        } else {
                            let (rows, unreadable) = self.private_list(&login, path.as_deref())?;
                            let limit = m.pointer("/params/arguments/limit").and_then(|v| v.as_u64()).unwrap_or(100).clamp(1, 200) as usize;
                            let after = m.pointer("/params/arguments/after").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                            let total = rows.len();
                            let page: Vec<serde_json::Value> = rows.into_iter().skip(after).take(limit).collect();
                            let next = (after + page.len()).min(total);
                            let sealed = if unreadable > 0 { self.sealed_index(&login).unwrap_or_default() } else { vec![] };
                            mcp::tool_result(serde_json::json!({"events": page, "next": next, "total": total, "unreadable": unreadable, "sealed_index": sealed,
                                "note": "Your Private tier: stored on the hub encrypted, apart from the shared record. Sealed items (passphrase only you know) are not readable here by design. Treat this as personal data: don't copy it into shared items."}), false)
                        };
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": v}))
                    } else if tool == "write_private" && m.get("id").is_some() {
                        // the personal side of a conversation's trail goes here, never into the shared record
                        let on = self.env.var("CONNECTOR_PRIVATE_WRITE").map(|v| v.to_string()).unwrap_or_default() == "on";
                        let args = m.pointer("/params/arguments").cloned().unwrap_or(serde_json::Value::Null);
                        let path = args.get("path").and_then(|v| v.as_str()).unwrap_or("").to_string();
                        let ev = args.get("event").cloned().unwrap_or(serde_json::Value::Null);
                        let ok_ev = ev.get("kind").and_then(|v| v.as_str()).is_some() && ev.get("ts").and_then(|v| v.as_str()).is_some();
                        let size = serde_json::to_vec(&ev).map(|v| v.len()).unwrap_or(usize::MAX);
                        let key = args.get("idempotency_key").and_then(|v| v.as_str()).map(str::to_string);
                        let doc_id = match &key {
                            Some(k) => hub_core::private::doc_id_for(&login, &path, k),
                            None => { let mut b = [0u8; 16]; getrandom::getrandom(&mut b).map_err(|e| Error::RustError(e.to_string()))?; format!("cp-{}", hex::encode(b)) }
                        };
                        let v = if !on {
                            mcp::tool_result(serde_json::json!({"error": "private writing is switched off for the connector on this hub (CONNECTOR_PRIVATE_WRITE)"}), true)
                        } else if !self.private_ready() {
                            mcp::tool_result(serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), true)
                        } else if !hub_core::private::path_owned_by(&path, &login) {
                            mcp::tool_result(serde_json::json!({"error": "that private path isn't yours (use data/users/<your login>/...)"}), true)
                        } else if !ok_ev {
                            mcp::tool_result(serde_json::json!({"error": "event needs kind and ts"}), true)
                        } else if size > hub_core::private::MAX_EVENT_BYTES {
                            mcp::tool_result(serde_json::json!({"error": "private event too large (64 KB max)"}), true)
                        } else {
                            let mut ev = ev;
                            if let Some(o) = ev.as_object_mut() { o.insert("via".into(), "claude-connector".into()); }
                            let added = self.private_put(&login, &path, &doc_id, &ev)?;
                            self.log(now, "/mcp", "write_private", if added { "ok" } else { "repeat" }, &login);
                            mcp::tool_result(serde_json::json!({"written": doc_id, "repeat": !added}), false)
                        };
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": v}))
                    } else if tool == "write_private_batch" && m.get("id").is_some() {
                        // I-next' d: bulk private import (e.g. the career sheet), checked whole, with per-row checksums
                        let on = self.env.var("CONNECTOR_PRIVATE_WRITE").map(|v| v.to_string()).unwrap_or_default() == "on";
                        let args = m.pointer("/params/arguments").cloned().unwrap_or(serde_json::Value::Null);
                        let v = if !on {
                            mcp::tool_result(serde_json::json!({"error": "private writing is switched off for the connector on this hub (CONNECTOR_PRIVATE_WRITE)"}), true)
                        } else if !self.private_ready() {
                            mcp::tool_result(serde_json::json!({"error": "the private store isn't set up on this hub yet (PRIVATE_SEED)"}), true)
                        } else {
                            match hub_core::private::plan_batch(&login, &args) {
                                Err(errs) => mcp::tool_result(serde_json::json!({"error": "batch rejected; nothing was stored", "reasons": errs}), true),
                                Ok((items, whole)) => {
                                    let mut rows = vec![];
                                    let mut added_n = 0;
                                    for it in &items {
                                        let added = self.private_put(&login, &it.path, &it.doc_id, &it.event)?;
                                        if added { added_n += 1; }
                                        rows.push(serde_json::json!({"key": it.key, "doc_id": it.doc_id, "checksum": it.checksum, "repeat": !added}));
                                    }
                                    self.log(now, "/mcp", "write_private_batch", &format!("ok {added_n}/{}", items.len()), &login);
                                    mcp::tool_result(serde_json::json!({"count": items.len(), "new": added_n, "repeats": items.len() - added_n,
                                        "batch_checksum": whole, "items": rows}), false)
                                }
                            }
                        };
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": v}))
                    } else if tool == "send_invite" && m.get("id").is_some() {
                        // An owner invites someone: fingerprint and grants go on the record, a welcome email goes to them,
                        // and the address itself is dropped once the email is sent.
                        let args = m.pointer("/params/arguments").cloned().unwrap_or_default();
                        let email = args.get("email").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                        let label: String = args.get("label").and_then(|v| v.as_str()).unwrap_or("").chars().take(80).collect();
                        let note: String = args.get("note").and_then(|v| v.as_str()).unwrap_or("").chars().take(600).collect();
                        let prefixes: Vec<String> = args.get("prefixes").and_then(|v| v.as_array()).map(|a| a.iter().filter_map(|x| x.as_str()).map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).take(10).collect()).unwrap_or_default();
                        let send = args.get("send_email").and_then(|v| v.as_bool()).unwrap_or(true);
                        let is_owner = self.hub.borrow().as_ref().unwrap().access().is_owner(&login);
                        let v = if !is_owner {
                            mcp::tool_result(serde_json::json!({"error": "only an owner can invite"}), true)
                        } else if !session::plausible_email(&email) || label.trim().is_empty() {
                            mcp::tool_result(serde_json::json!({"error": "need a valid email and a label"}), true)
                        } else {
                            let hash = session::email_hash(&email);
                            let invitee = session::email_login(&email);
                            let call = |kind: &str, body: serde_json::Value, idem: String| serde_json::json!({"jsonrpc": "2.0", "id": 0, "method": "tools/call",
                                "params": {"name": "write_event", "arguments": {"kind": kind, "item": null, "body": body, "idempotency_key": idem}}});
                            {
                                let mut b = self.hub.borrow_mut();
                                let h = b.as_mut().unwrap();
                                mcp::handle(h, &login, &key, &call("access.invite", serde_json::json!({"email_sha256": hash, "label": label}), format!("invite/{}/{}", &hash[..16], label)), now);
                                if !prefixes.is_empty() {
                                    mcp::handle(h, &login, &key, &call("access.grant", serde_json::json!({"login": invitee, "prefixes": prefixes}), format!("grant/{}/{}", &hash[..16], prefixes.join(","))), now);
                                }
                            }
                            let emailed = if send {
                                let login_url = format!("{}/studio/login", cfg.issuer);
                                let mut text = format!("You've been invited to Nodach Studio.\n\n");
                                if !note.trim().is_empty() { text.push_str(&format!("{}\n\n", note.trim())); }
                                text.push_str(&format!("To sign in, open {login_url}, then either choose \"Continue with Google\" (if this address is a Google account) or type this address under \"Email me a link\" and click the link we send you.\n\nPrivacy notice: https://nodach.com/privacy\nTerms: https://nodach.com/terms\n"));
                                match self.send_mail(&email, "You're invited to Nodach Studio", &text).await { Ok(()) => "sent".to_string(), Err(e) => format!("not sent: {e}") }
                            } else { "not requested".to_string() };
                            self.log(now, "/mcp", "send_invite", &emailed, &invitee);
                            mcp::tool_result(serde_json::json!({"invited": invitee, "label": label, "spaces": prefixes, "email": emailed,
                                "stored": "the address's SHA-256 fingerprint only"}), false)
                        };
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": v}))
                    } else if tool == "import_studio_events" && m.get("id").is_some() {
                        // D-129 / Q99 A: Studio Web's events keep their own authors; the importer key vouches for the copy
                        let args = m.pointer("/params/arguments").cloned().unwrap_or_default();
                        let forged = |docs: &Vec<(String, serde_json::Value)>| docs.iter().any(|(_, ev)| ev.get("kind").and_then(|k| k.as_str()) == Some("account.link")
                            && !ev.pointer("/author/id").and_then(|v| v.as_str()).is_some_and(|a| a.eq_ignore_ascii_case(&format!("github:{login}"))));
                        let v = match mcp::import_docs(&args) {
                            Err(e) => mcp::tool_result(serde_json::json!({"error": e}), true),
                            // a sign-in link must be written by the account it links to (F-two-accounts-one-person)
                            Ok(docs) if forged(&docs) => mcp::tool_result(serde_json::json!({"error": "account.link events can only be imported by the account that wrote them", "refused": true}), true),
                            Ok(docs) => {
                                let sent = docs.len();
                                let importer = self.importer_key()?;
                                let r = self.hub.borrow_mut().as_mut().unwrap().import(&importer, docs, now);
                                match r {
                                    Ok(made) => {
                                        let n = made.iter().filter(|e| e.u.kind == hub_core::IMPORT_KIND).count();
                                        mcp::tool_result(serde_json::json!({"imported": n, "already_there": sent - n}), false)
                                    }
                                    Err(e) => mcp::tool_result(serde_json::json!({"error": format!("{e:?}")}), true),
                                }
                            }
                        };
                        Some(serde_json::json!({"jsonrpc": "2.0", "id": m["id"], "result": v}))
                    } else {
                        let mut b = self.hub.borrow_mut();
                        mcp::handle(b.as_mut().unwrap(), &login, &key, m, now)
                    };
                    let outcome = match &r {
                        None => "notification".to_string(),
                        // a client probing for a method this hub does not offer (e.g. server/discover) is not a failure
                        Some(v) if v.pointer("/error/code") == Some(&serde_json::json!(-32601)) => "not offered".to_string(),
                        Some(v) if v.get("error").is_some() => format!("error {}", v.pointer("/error/code").cloned().unwrap_or_default()),
                        Some(v) if v.pointer("/result/isError") == Some(&serde_json::Value::Bool(true)) => "tool error".to_string(),
                        Some(_) => "ok".to_string(),
                    };
                    if tool != "hub_log" { self.log(now, "/mcp", &tool, &outcome, &login); }
                    if let Some(v) = r { replies.push(v); }
                }
                let added = self.hub.borrow().as_ref().unwrap().pull(before, usize::MAX).events;
                self.persist(&added)?;
                match (&msg, replies.len()) {
                    (_, 0) => Response::empty()?.with_status(202),
                    (serde_json::Value::Array(_), _) => json_status(&serde_json::Value::Array(replies), 200)?,
                    _ => json_status(&replies[0], 200)?,
                }
            }
            (_, "/mcp") => {
                let h = Headers::new();
                h.set("allow", "POST")?;
                Response::error("use POST", 405)?.with_headers(h)
            }
            _ => return Ok(None),
        };
        Ok(Some(resp))
    }
}

impl DurableObject for Record {
    fn new(state: State, env: Env) -> Self {
        Self { state, env, hub: RefCell::new(None), oauth: RefCell::new(None) }
    }

    /// Hourly: run every scheduled start step that is due this UK hour and hasn't run in this slot yet.
    async fn alarm(&self) -> Result<Response> {
        self.load()?;
        let now = Date::now().as_millis() as i64;
        // next wake-up first, so a failed run can never stop the schedule
        self.state.storage().set_alarm(3_600_000 - now.rem_euclid(3_600_000) + 60_000).await?;
        let due = flows::due(&self.all_events(), now);
        for (board, start, slot) in &due {
            let res = self.run_flow(board, start, "schedule", now, Some(slot)).await?;
            let ok = res.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
            self.log(now, "/alarm", "schedule", if ok { "ok" } else { "failed" }, "schedule");
        }
        Response::ok(format!("ran {}", due.len()))
    }

    async fn fetch(&self, mut req: Request) -> Result<Response> {
        self.load()?;
        if let Err(e) = self.ensure_alarm().await { console_error!("alarm: {e}"); }
        let url = req.url()?;
        let started = Date::now().as_millis() as i64;
        let path = req.path();
        let method = req.method().to_string();
        if let Some((r, who)) = self.studio(&mut req, &url).await? {
            // writes log themselves (with the tool name); everything else here gets one line with the login
            if !((path == "/studio/api/events" || path == "/studio/api/run") && method == "POST") {
                self.log(started, &path, &method, &format!("{}", r.status_code()), &who);
            }
            return Ok(r);
        }
        if let Some(r) = self.connector(&mut req, &url).await? {
            // F-58: sign-in and discovery steps are logged too (route, method, status; never codes, tokens or bodies),
            // so a connection that fails before /mcp still leaves a trace. /mcp logs each message itself.
            if path != "/mcp" {
                self.log(started, &path, &method, &format!("{}", r.status_code()), "-");
            }
            return Ok(r);
        }
        let q = |k: &str| url.query_pairs().find(|(a, _)| a == k).and_then(|(_, v)| v.parse::<usize>().ok());
        match (req.method(), req.path().as_str()) {
            (Method::Get, "/status" | "/pull" | "/studio-export" | "/reliability") if !self.can_read(&req) => Response::error("unauthorised", 401),
            // P1 a (INC-261118-seed): what the watch job compares with its baseline. Counts and flags only, never content.
            (Method::Get, "/reliability") => Response::from_json(&self.reliability()?),
            (Method::Get, "/status") => Response::from_json(&self.hub.borrow().as_ref().unwrap().status()),
            (Method::Get, "/pull") => {
                let page = self.hub.borrow().as_ref().unwrap().pull(q("after").unwrap_or(0), q("limit").unwrap_or(500).min(2000));
                Response::from_json(&page)
            }
            (Method::Get, "/studio-export") => {
                let (docs, next) = self.hub.borrow().as_ref().unwrap().to_studio(q("after").unwrap_or(0), q("limit").unwrap_or(500).min(2000));
                let docs: Vec<serde_json::Value> = docs.into_iter().map(|(d, e)| serde_json::json!({"doc_id": d, "event": e})).collect();
                Response::from_json(&serde_json::json!({"docs": docs, "next": next}))
            }
            (Method::Post, "/push") => {
                if !self.authorised(&req) { return Response::error("unauthorised", 401); }
                let evs: Vec<Event> = req.json().await?;
                let r = self.hub.borrow_mut().as_mut().unwrap().push(evs);
                let added: Vec<Event> = { let b = self.hub.borrow(); let h = b.as_ref().unwrap(); r.added.iter().filter_map(|id| h.get(id).cloned()).collect() };
                self.persist(&added)?;
                Response::from_json(&r)
            }
            (Method::Post, "/import") => {
                if !self.authorised(&req) { return Response::error("unauthorised", 401); }
                let seed = self.seed_bytes(self.active_seed("IMPORTER_SEED"))?;
                let docs: Vec<Doc> = req.json().await?;
                let now = Date::now().as_millis() as i64;
                let made = self.hub.borrow_mut().as_mut().unwrap()
                    .import(&KeyPair::from_seed(seed), docs.into_iter().map(|d| (d.doc_id, d.event)).collect(), now)
                    .map_err(|e| Error::RustError(e.to_string()))?;
                self.persist(&made)?;
                Response::from_json(&serde_json::json!({ "imported": made.len() }))
            }
            _ => Response::error("not found", 404),
        }
    }
}
