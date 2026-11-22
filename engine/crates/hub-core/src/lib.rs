//! hub-core (D-089, D-095): one record that survives any single provider.
//!
//! A hub is a node that is always on and serves the record to others. This crate holds everything
//! a hub does that does not depend on where it runs, so Cloudflare, the PC node, Oracle and friends'
//! nodes all behave identically (D-091) and the rules live only in the engine (D-094).
//!
//! - `push`  : accept events in any order; the engine checks signatures, chains and authority.
//! - `pull`  : serve events after a cursor (this node's arrival order), so any node can catch up.
//! - `import`: Studio Web → hub. Each Studio Web event is wrapped as a signed fact by the hub's
//!             importer key, keeping the Studio doc id for dedupe.
//! - `to_studio`: hub → Studio Web (D-095 revised, Q44 "C now"). Writes made on other nodes
//!             (kind `studio.write`) come back as Studio Web events with doc id `hub-<event id>`.
//!             `import` skips those ids, so nothing loops (D-101). Both sides order by (ts, doc id).

use foundation_core::event::{Event, Id, KeyPair, Realm};
use foundation_core::store::{Reject, Store};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};

pub mod mcp;
pub mod oauth;
pub mod session;
pub mod flows;
pub mod private;
pub mod profiles;
pub mod guard;
pub mod access;
pub mod digest;
pub mod authorship;

pub const IMPORT_KIND: &str = "studio.event";
/// A Studio-shaped write made on a node other than Studio Web (PC, phone, an AI via the hub).
pub const WRITE_KIND: &str = "studio.write";

/// The Studio Web doc id a hub write gets. Deterministic, so every node exports the same id.
pub fn studio_doc_id(event_id: &str) -> String {
    format!("hub-{}", &event_id[..event_id.len().min(32)])
}

#[derive(Serialize, Deserialize, Debug, PartialEq, Default)]
pub struct PushResult {
    /// Accepted now, in the order they were stored (persist these, in this order).
    pub added: Vec<Id>,
    /// Already held: harmless, so re-sending is always safe.
    pub duplicates: usize,
    /// Not accepted, with the engine's reason. Missing parents / unauthorised may succeed later.
    pub refused: Vec<(Id, String)>,
}

#[derive(Serialize, Deserialize, Debug, PartialEq)]
pub struct Page {
    pub events: Vec<Event>,
    /// Pass back as `after` to continue. Equal to `total` when caught up.
    pub next: usize,
    pub total: usize,
}

#[derive(Serialize, Deserialize, Debug, PartialEq)]
pub struct Status {
    pub events: usize,
    pub imported_studio_events: usize,
    pub verified: bool,
    pub detail: String,
}

#[derive(Default)]
pub struct Hub {
    store: Store,
    imported: HashSet<String>,
    /// Studio doc ids of this record's own `studio.write` facts: never re-imported (no loops).
    exported: HashSet<String>,
    /// F-48: a write's idempotency key → the Studio doc id it produced, so a repeated write is a no-op.
    idem: HashMap<String, String>,
    /// F-47: item ids this record knows (created in Studio or through the hub), to warn on unknown targets.
    items: HashSet<String>,
    /// A1 d: owner logins (from the hub's settings); empty means everyone signed in is an owner, as before.
    owners: Vec<String>,
}

impl Hub {
    pub fn new() -> Self {
        Self::default()
    }

    /// Rebuild from persisted events (cold start). Persisted order is arrival order, so it always fits.
    pub fn restore(events: Vec<Event>) -> (Self, PushResult) {
        let mut h = Self::new();
        let r = h.push(events);
        (h, r)
    }

    pub fn push(&mut self, events: Vec<Event>) -> PushResult {
        let before = self.store.order.len();
        let mut dup = 0;
        let mut fresh = vec![];
        let mut seen = HashSet::new();
        for e in events {
            if self.store.events.contains_key(&e.id) || !seen.insert(e.id.clone()) {
                dup += 1;
            } else {
                fresh.push(e);
            }
        }
        let (_, refused) = self.store.add_all(fresh);
        let added: Vec<Id> = self.store.order[before..].to_vec();
        for id in added.clone() {
            self.index(&id);
        }
        PushResult { added, duplicates: dup, refused }
    }

    /// Keep the lookup sets in step with one stored fact (used on push, restore, import and write).
    fn index(&mut self, id: &str) {
        let ev = &self.store.events[id];
        let studio = match ev.u.kind.as_str() {
            k if k == WRITE_KIND => {
                let doc = studio_doc_id(id);
                if let Some(key) = ev.u.body.get("idem").and_then(Value::as_str) {
                    self.idem.insert(key.to_string(), doc.clone());
                }
                self.exported.insert(doc);
                Some(&ev.u.body)
            }
            k if k == IMPORT_KIND => {
                if let Some(d) = ev.u.body.get("doc_id").and_then(Value::as_str) {
                    self.imported.insert(d.to_string());
                }
                ev.u.body.get("event")
            }
            _ => None,
        };
        if let Some(se) = studio {
            if se.get("kind").and_then(Value::as_str) == Some("item.create") {
                if let Some(item) = se.pointer("/body/id").and_then(Value::as_str) {
                    self.items.insert(item.to_string());
                }
            }
        }
    }

    /// Whether an item id is known to this record (F-47).
    pub fn knows_item(&self, id: &str) -> bool {
        self.items.contains(id)
    }

    /// The doc id an earlier write with this idempotency key produced, if any (F-48).
    pub fn written_with(&self, key: &str) -> Option<&String> {
        self.idem.get(key)
    }

    pub fn pull(&self, after: usize, limit: usize) -> Page {
        let total = self.store.order.len();
        let start = after.min(total);
        let end = start.saturating_add(limit.max(1)).min(total); // F-46: never overflow, whatever limit is asked
        let events = self.store.order[start..end].iter().map(|id| self.store.events[id].clone()).collect();
        Page { events, next: end, total }
    }

    pub fn get(&self, id: &str) -> Option<&Event> {
        self.store.events.get(id)
    }

    /// Hub → Studio Web: `studio.write` facts after the cursor, as (doc id, Studio event) pairs.
    /// Each Studio event keeps its own ts/kind/item/body/author and gains `origin` (the signer).
    pub fn to_studio(&self, after: usize, limit: usize) -> (Vec<(String, Value)>, usize) {
        let page = self.pull(after, limit);
        let docs = page.events.iter().filter(|e| e.u.kind == WRITE_KIND).map(|e| {
            let mut v = e.u.body.clone();
            if let Some(o) = v.as_object_mut() {
                o.insert("origin".into(), json!(e.u.author));
            }
            (studio_doc_id(&e.id), v)
        }).collect();
        (docs, page.next)
    }

    /// Studio-shaped events after the cursor, as `{doc_id, event}` objects: imported Studio Web events plus
    /// writes made on other nodes. Returns (docs, next cursor, total, facts skipped because they are not
    /// Studio events, e.g. a key's identity fact). Serves the connector's `read_events` and Studio served
    /// by the hub (D-171), so both see exactly the same record.
    pub fn studio_events(&self, after: usize, limit: usize) -> (Vec<Value>, usize, usize, usize) {
        let page = self.pull(after, limit);
        let docs: Vec<Value> = page.events.iter().filter_map(|e| match e.u.kind.as_str() {
            k if k == IMPORT_KIND => Some(json!({"doc_id": e.u.body.get("doc_id"), "event": e.u.body.get("event")})),
            k if k == WRITE_KIND => Some(json!({"doc_id": studio_doc_id(&e.id), "event": e.u.body})),
            _ => None,
        }).collect();
        let skipped = page.events.len() - docs.len();
        (docs, page.next, page.total, skipped)
    }

    /// Accounts, profiles, item ownership and connections (I2), folded from every Studio-shaped event in order.
    pub fn profiles(&self) -> profiles::State {
        let (docs, _, _, _) = self.studio_events(0, usize::MAX);
        let mut st = profiles::State::default();
        for d in &docs {
            if let (Some(id), Some(ev)) = (d.get("doc_id").and_then(Value::as_str), d.get("event")) {
                st.observe(id, ev);
            }
        }
        st
    }

    /// A1 d: set the owner logins (the worker reads them from OWNER_GITHUB_LOGINS).
    pub fn set_owners(&mut self, owners: Vec<String>) { self.owners = owners; }

    /// Who may see and change what, folded from the record's access grants (A1 d).
    pub fn access(&self) -> access::State {
        let (docs, _, _, _) = self.studio_events(0, usize::MAX);
        let evs: Vec<Value> = docs.iter().filter_map(|d| d.get("event").cloned()).collect();
        access::State::fold(&self.owners, &evs)
    }

    /// One page of the shared record as `login` may see it: same cursor and total, members get only what's theirs.
    pub fn studio_events_for(&self, login: &str, after: usize, limit: usize) -> (Vec<Value>, usize, usize, usize) {
        let (docs, next, total, skipped) = self.studio_events(after, limit);
        let acc = self.access();
        if acc.is_owner(login) { return (docs, next, total, skipped); }
        let mine: Vec<Value> = docs.into_iter().filter(|d| d.get("event").map(|e| acc.visible(login, e)).unwrap_or(false)).collect();
        (mine, next, total, skipped)
    }

    /// The resume digest for `login`: active rules, open questions, latest replies (see digest.rs).
    pub fn digest_for(&self, login: &str) -> serde_json::Value {
        let (docs, _, _, _) = self.studio_events_for(login, 0, usize::MAX);
        digest::fold(docs.iter().filter_map(|d| d.get("event")))
    }

    /// Session claims and Studio branches (multi-chat guard), folded from every Studio-shaped event in order.
    pub fn guard(&self) -> guard::State {
        let (docs, _, _, _) = self.studio_events(0, usize::MAX);
        guard::State::fold(docs.iter().filter_map(|d| d.get("event")))
    }

    pub fn has_imported(&self, doc_id: &str) -> bool {
        self.imported.contains(doc_id)
    }

    pub fn status(&self) -> Status {
        let (verified, detail) = match self.store.verify() {
            Ok(n) => (true, format!("{n} events verified")),
            Err(e) => (false, e),
        };
        Status { events: self.store.order.len(), imported_studio_events: self.imported.len(), verified, detail }
    }

    /// Give the importer key its identity (first fact of that key). Idempotent.
    pub fn ensure_importer(&mut self, k: &KeyPair, label: &str, ts: i64) -> Result<Option<Event>, Reject> {
        if self.store.auth.roots.contains_key(&k.public) {
            return Ok(None);
        }
        self.store.fact(k, "identity.genesis", json!({ "label": label }), Realm::Real, None, ts, &[]).map(Some)
    }

    /// Wrap Studio Web events (doc id → event JSON) as signed facts. Already-imported doc ids are skipped,
    /// so running the export twice is harmless. Returns the new facts in order (persist them).
    pub fn import(&mut self, k: &KeyPair, docs: Vec<(String, Value)>, ts: i64) -> Result<Vec<Event>, Reject> {
        let mut out = vec![];
        if let Some(g) = self.ensure_importer(k, "studio-web importer", ts)? {
            out.push(g);
        }
        let mut docs = docs;
        // Studio's own timestamps first, then doc id: a stable order whoever runs the import.
        docs.sort_by(|a, b| {
            let ta = a.1.get("ts").and_then(Value::as_str).unwrap_or("");
            let tb = b.1.get("ts").and_then(Value::as_str).unwrap_or("");
            ta.cmp(tb).then(a.0.cmp(&b.0))
        });
        for (doc_id, ev) in docs {
            // already here, or it is one of our own writes coming back from Studio Web
            if self.imported.contains(&doc_id) || self.exported.contains(&doc_id) {
                continue;
            }
            let e = self.store.fact(k, IMPORT_KIND, json!({ "doc_id": doc_id, "event": ev }), Realm::Real, None, ts, &[])?;
            self.index(&e.id);
            out.push(e);
        }
        Ok(out)
    }

    /// A Studio-shaped write made through this node (e.g. the Claude connector): signed by `k` as a
    /// `studio.write` fact, so it flows to Studio Web via /studio-export and never echoes back (D-101).
    /// Returns the new facts in order (persist them); the last one is the write itself.
    pub fn write(&mut self, k: &KeyPair, label: &str, studio_event: Value, ts: i64) -> Result<Vec<Event>, Reject> {
        let mut out = vec![];
        if let Some(g) = self.ensure_importer(k, label, ts)? {
            out.push(g);
        }
        let e = self.store.fact(k, WRITE_KIND, studio_event, Realm::Real, None, ts, &[])?;
        self.index(&e.id);
        out.push(e);
        Ok(out)
    }
}
