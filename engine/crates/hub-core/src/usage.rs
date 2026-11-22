//! Usage meter and free limits (FUN-1 v2, BQ-1 step 3, 22 Nov 2026).
//!
//! Nodach is free to use, with generous monthly limits on machine capacity. One meter per profile and per teamspace
//! counts, per calendar month (UTC):
//!   - AI calls: the flow AI step (one per item answered) and the live responder (one per answer);
//!   - flow runs: manual and scheduled;
//!   - stored bytes: the scope's Studio events, the encrypted files they reference (declared size) and, for a
//!     Personal profile, the account's Private tier rows. Storage is what is held now, shown in the current month.
//!
//! Nothing here is a counter that can be overwritten: AI calls and runs are folded from the runner's own `flow.run`
//! facts (`body.usage = {scope, runs, ai_calls}`) and the live responder's answers, so the meter at any past point is
//! the record up to that point (D-nodach-trail).
//!
//! Which meter pays (the scope):
//!   - work on a node in a teamspace is the teamspace's;
//!   - a run started from a Studio window is the window's profile's;
//!   - otherwise the node's home profile: its latest `profile.assign` link, else the profile its `item.create` was
//!     written as, else its author's Personal profile.
//!
//! Limits are a node, `PLAN-limits` (type setting, `value` = {ai_calls, runs, storage_bytes, placeholder}), so a
//! change is an ordinary versioned edit (in Studio a Developer-mode proposal first, NVS-4). Only a hub owner's edits
//! count; anyone else's are kept, ignored and reported as findings. With no node yet the placeholder defaults apply
//! (500 AI calls, 2,000 runs, 1 GB): placeholders for Nowed to set.
//!
//! At 80% of a limit an urgent notice item goes to the scope owners' queue (once per scope, meter and month); at
//! 100% another one, and AI calls and runs are refused with `REFUSAL` (logged as "refused: limit"). Storage is
//! warn-only. The hub owner's Developer profile is exempt (no counting limits, no notices).
//!
//! Rollback: revert the PR. The `usage` fields and notice items stay on the record (append-only) and mean nothing.
use crate::{profiles, teamspaces};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};

pub const LIMITS_ID: &str = "PLAN-limits";
pub const REFUSAL: &str = "Monthly free limit reached; a top-up will be available soon";
/// The request-log outcome for a refusal.
pub const REFUSED_OUTCOME: &str = "refused: limit";
pub const WARN_PCT: u64 = 80;
/// Authors whose `usage` the meter trusts: the hub's own flow runner and live responder.
pub const RUNNER_ID: &str = "flow:runner";
pub const RESPONDER_ID: &str = "agent:live-responder";
pub const NOTICE_PREFIX: &str = "USE-";
/// PLACEHOLDER defaults (FUN-1 v2): Nowed sets the real numbers in the PLAN-limits node.
pub const DEFAULT_AI_CALLS: u64 = 500;
pub const DEFAULT_RUNS: u64 = 2_000;
pub const DEFAULT_STORAGE_BYTES: u64 = 1_000_000_000;

#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Meter { AiCalls, Runs, StorageBytes }

impl Meter {
    pub const ALL: [Meter; 3] = [Meter::AiCalls, Meter::Runs, Meter::StorageBytes];
    pub fn key(self) -> &'static str {
        match self { Meter::AiCalls => "ai_calls", Meter::Runs => "runs", Meter::StorageBytes => "storage_bytes" }
    }
    pub fn label(self) -> &'static str {
        match self { Meter::AiCalls => "AI calls", Meter::Runs => "flow runs", Meter::StorageBytes => "stored bytes" }
    }
    /// Storage is warn-only; AI calls and runs are refused at 100%.
    pub fn enforced(self) -> bool { self != Meter::StorageBytes }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Limits {
    pub ai_calls: u64,
    pub runs: u64,
    pub storage_bytes: u64,
    /// true while the numbers are the placeholder defaults (or the node still says so)
    pub placeholder: bool,
    /// the limits node's id, once it exists on the record
    pub node: Option<String>,
    /// how many accepted versions of the limits there have been (0: defaults)
    pub version: usize,
    pub updated: Option<String>,
    pub doc_id: Option<String>,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { ai_calls: DEFAULT_AI_CALLS, runs: DEFAULT_RUNS, storage_bytes: DEFAULT_STORAGE_BYTES, placeholder: true,
                 node: None, version: 0, updated: None, doc_id: None }
    }
}

impl Limits {
    pub fn of(&self, m: Meter) -> u64 {
        match m { Meter::AiCalls => self.ai_calls, Meter::Runs => self.runs, Meter::StorageBytes => self.storage_bytes }
    }
    /// The value a new limits node starts with (the placeholders, marked as such).
    pub fn default_value() -> Value {
        json!({"ai_calls": DEFAULT_AI_CALLS, "runs": DEFAULT_RUNS, "storage_bytes": DEFAULT_STORAGE_BYTES, "placeholder": true})
    }
}

#[derive(Serialize, Clone, Copy, Default, Debug, PartialEq)]
pub struct Counts { pub ai_calls: u64, pub runs: u64, pub storage_bytes: u64 }

impl Counts {
    pub fn get(&self, m: Meter) -> u64 {
        match m { Meter::AiCalls => self.ai_calls, Meter::Runs => self.runs, Meter::StorageBytes => self.storage_bytes }
    }
    fn add(&mut self, m: Meter, n: u64) {
        let f = match m { Meter::AiCalls => &mut self.ai_calls, Meter::Runs => &mut self.runs, Meter::StorageBytes => &mut self.storage_bytes };
        *f = f.saturating_add(n);
    }
}

#[derive(Default, Clone, Debug)]
pub struct State {
    /// `YYYY-MM` (UTC) the AI-call and run counts are for
    pub month: String,
    pub limits: Limits,
    /// scope (profile id or teamspace id) → this month's counts
    pub counts: BTreeMap<String, Counts>,
    /// limits edits that didn't count (not a hub owner, unreadable value)
    pub findings: Vec<profiles::Finding>,
    teams: teamspaces::State,
    profiles: profiles::State,
    hub_owners: Vec<String>,
    item_home: HashMap<String, String>,
    notices: HashSet<String>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).map(str::trim).unwrap_or("")
}

fn lc(x: &str) -> String { x.trim().to_ascii_lowercase() }

fn author_login(ev: &Value) -> Option<String> {
    ev.pointer("/author/id").and_then(Value::as_str).and_then(|a| a.strip_prefix("github:")).map(lc).filter(|l| !l.is_empty())
}

fn author_id(ev: &Value) -> &str {
    ev.pointer("/author/id").and_then(Value::as_str).unwrap_or("")
}

/// `YYYY-MM` of a Studio timestamp (`2026-11-22T…`); "" if it isn't one.
pub fn month_of(ts: &str) -> &str {
    let t = ts.trim();
    if t.len() >= 7 && t.as_bytes()[4] == b'-' { &t[..7] } else { "" }
}

/// `YYYY-MM` (UTC) of a time in milliseconds.
pub fn month_ms(ms: i64) -> String {
    month_of(&crate::mcp::iso(ms)).to_string()
}

/// Percent used, rounded down; a zero limit counts as full once anything is used.
pub fn pct(used: u64, limit: u64) -> u64 {
    if limit == 0 { return if used > 0 { 100 } else { 0 }; }
    ((used as u128) * 100 / (limit as u128)).min(u64::MAX as u128) as u64
}

/// The `usage` a runner fact carries.
pub fn usage_body(scope: &str, runs: u64, ai_calls: u64) -> Value {
    json!({"scope": scope, "runs": runs, "ai_calls": ai_calls})
}

/// Is this event a usage notice queued for `login`? (Lets the account a notice is for read it, A1 d.)
pub fn queued_for(ev: &Value, login: &str) -> bool {
    s(ev, "kind") == "item.create" && author_id(ev) == RUNNER_ID
        && ev.pointer("/body/id").and_then(Value::as_str).is_some_and(|i| i.starts_with(NOTICE_PREFIX))
        && ev.pointer("/body/meta/queue").and_then(Value::as_array)
            .is_some_and(|q| q.iter().filter_map(Value::as_str).any(|l| lc(l) == lc(login)))
}

/// The limits a node value gives: a JSON string (as Studio saves settings) or an object. Fields left out keep `prev`.
fn parse_limits(v: &Value, prev: &Limits) -> Result<Limits, String> {
    let obj = match v {
        Value::String(t) => serde_json::from_str::<Value>(t).map_err(|_| "the limits value isn't JSON".to_string())?,
        o => o.clone(),
    };
    if !obj.is_object() { return Err("the limits value must be {ai_calls, runs, storage_bytes}".into()); }
    let num = |k: &str, d: u64| -> Result<u64, String> {
        match obj.get(k) {
            None | Some(Value::Null) => Ok(d),
            Some(x) => x.as_u64().ok_or(format!("{k} must be a whole number, 0 or more")),
        }
    };
    Ok(Limits {
        ai_calls: num("ai_calls", prev.ai_calls)?,
        runs: num("runs", prev.runs)?,
        storage_bytes: num("storage_bytes", prev.storage_bytes)?,
        placeholder: obj.get("placeholder").and_then(Value::as_bool).unwrap_or(false),
        ..prev.clone()
    })
}

fn safe_id(x: &str) -> String {
    x.chars().map(|c| if c.is_ascii_alphanumeric() || "-_.:".contains(c) { c } else { '-' }).take(80).collect()
}

/// The id of the notice for one scope, meter, threshold and month (the same on every node, so it is raised once).
pub fn notice_id(month: &str, m: Meter, threshold: u64, scope: &str) -> String {
    safe_id(&format!("{NOTICE_PREFIX}{month}-{}-{threshold}-{scope}", m.key()))
}

impl State {
    /// Fold Studio-shaped events, in record order, into this month's meters. `private_bytes` is each login's Private
    /// tier size (the hub's store; it never enters the record). `hub_owners` empty means everyone is an owner (A1 d).
    pub fn fold<'a>(profiles: profiles::State, teams: teamspaces::State, hub_owners: &[String],
                    docs: impl IntoIterator<Item = (&'a str, &'a Value)>, month: &str, private_bytes: &BTreeMap<String, u64>) -> Self {
        let docs: Vec<(&str, &Value)> = docs.into_iter().collect();
        let mut st = State { month: month.to_string(), teams, profiles, hub_owners: hub_owners.iter().map(|o| lc(o)).filter(|o| !o.is_empty()).collect(), ..State::default() };
        // pass 1: where each node lives, notices already raised, the limits node's versions
        for (doc_id, ev) in &docs {
            let kind = s(ev, "kind");
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            if kind == "item.create" {
                let id = s(&b, "id");
                if id.is_empty() { continue; }
                if id.starts_with(NOTICE_PREFIX) && author_id(ev) == RUNNER_ID { st.notices.insert(id.to_string()); }
                if !st.item_home.contains_key(id) {
                    let prof = ev.pointer("/author/profile").and_then(Value::as_str).map(str::trim).filter(|p| !p.is_empty()).map(str::to_string);
                    if let Some(p) = prof.or_else(|| author_login(ev).map(|l| profiles::personal_id(&l))) {
                        st.item_home.insert(id.to_string(), p);
                    }
                }
            }
            let subject = if kind == "item.create" { s(&b, "id") } else { s(ev, "item") };
            if subject == LIMITS_ID && matches!(kind, "item.create" | "item.edit") {
                let owner = author_login(ev).is_some_and(|l| st.hub_owners.is_empty() || st.hub_owners.contains(&l));
                if kind == "item.create" && st.limits.node.is_none() && owner { st.limits.node = Some(LIMITS_ID.to_string()); }
                let Some(v) = b.get("value") else { continue };
                if !owner {
                    st.findings.push(profiles::Finding { doc_id: doc_id.to_string(), kind: kind.into(), reason: "only a hub owner changes the free limits".into() });
                    continue;
                }
                match parse_limits(v, &st.limits) {
                    Ok(mut l) => {
                        l.node = Some(LIMITS_ID.to_string());
                        l.version += 1;
                        l.updated = Some(s(ev, "ts").to_string());
                        l.doc_id = Some(doc_id.to_string());
                        st.limits = l;
                    }
                    Err(reason) => st.findings.push(profiles::Finding { doc_id: doc_id.to_string(), kind: kind.into(), reason }),
                }
            }
        }
        for (item, a) in &st.profiles.assigned { st.item_home.insert(item.clone(), a.profile.clone()); }
        // pass 2: count
        for (_, ev) in &docs {
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            // storage: every event, plus the declared size of an encrypted file it references
            let mut bytes = serde_json::to_string(ev).map(|x| x.len() as u64).unwrap_or(0);
            if b.pointer("/raw/asset").and_then(Value::as_str).is_some() {
                bytes = bytes.saturating_add(b.pointer("/raw/size").and_then(Value::as_u64).unwrap_or(0));
            }
            if let Some(sc) = st.scope_of_event(ev) { st.counts.entry(sc).or_default().add(Meter::StorageBytes, bytes); }
            if month_of(s(ev, "ts")) != st.month { continue; }
            let kind = s(ev, "kind");
            let who = author_id(ev);
            let charged = if who == RUNNER_ID && kind == "flow.run" {
                match b.get("usage").filter(|u| u.is_object()) {
                    Some(u) => Some((s(u, "scope").to_string(), u.get("runs").and_then(Value::as_u64).unwrap_or(1), u.get("ai_calls").and_then(Value::as_u64).unwrap_or(0))),
                    None => {
                        // before this step: one run on its board's meter; AI calls from its AI steps (at most 20 items each)
                        let ai = b.get("steps").and_then(Value::as_array).map(|a| a.iter()
                            .filter(|x| s(x, "step") == "ai" && x.get("ok").and_then(Value::as_bool) == Some(true))
                            .map(|x| x.get("count").and_then(Value::as_u64).unwrap_or(0).min(20)).sum()).unwrap_or(0);
                        st.scope_of(s(&b, "board")).map(|sc| (sc, 1, ai))
                    }
                }
            } else if who == RESPONDER_ID && kind == "comment" {
                match b.get("usage").filter(|u| u.is_object()) {
                    Some(u) => Some((s(u, "scope").to_string(), 0, u.get("ai_calls").and_then(Value::as_u64).unwrap_or(1))),
                    None => st.scope_of(s(ev, "item")).map(|sc| (sc, 0, 1)),
                }
            } else { None };
            if let Some((sc, runs, ai)) = charged.filter(|(sc, _, _)| !sc.is_empty()) {
                let c = st.counts.entry(sc).or_default();
                c.add(Meter::Runs, runs);
                c.add(Meter::AiCalls, ai);
            }
        }
        for (login, n) in private_bytes {
            st.counts.entry(profiles::personal_id(login)).or_default().add(Meter::StorageBytes, *n);
        }
        st
    }

    /// The meter a node's work goes to: its teamspace, else its home profile.
    pub fn scope_of(&self, item: &str) -> Option<String> {
        let item = item.trim();
        if item.is_empty() { return None; }
        self.teams.item_space.get(item).or_else(|| self.item_home.get(item)).cloned()
    }

    fn scope_of_event(&self, ev: &Value) -> Option<String> {
        if let Some(sc) = crate::access::subject(ev).and_then(|i| self.scope_of(&i)) { return Some(sc); }
        if crate::teamspaces::KINDS.contains(&s(ev, "kind")) {
            let ts = crate::teamspaces::teamspace_of(ev);
            if self.teams.teamspaces.contains_key(&ts) { return Some(ts); }
        }
        let prof = ev.pointer("/author/profile").and_then(Value::as_str).map(str::trim).filter(|p| !p.is_empty()).map(str::to_string);
        prof.or_else(|| author_login(ev).map(|l| profiles::personal_id(&l)))
    }

    /// The meter a flow run on `board` goes to: the board's teamspace; else the window's profile when started from a
    /// Studio window; else the board's home profile; else the Personal profile of whoever started it.
    pub fn scope_for_run(&self, board: &str, window_profile: Option<&str>, login: &str) -> String {
        if let Some(ts) = self.teams.item_space.get(board.trim()) { return ts.clone(); }
        if let Some(p) = window_profile.map(str::trim).filter(|p| !p.is_empty()) { return p.to_string(); }
        self.scope_of(board).unwrap_or_else(|| profiles::personal_id(login))
    }

    /// The hub owner's Developer profile is exempt.
    pub fn exempt(&self, scope: &str) -> bool {
        self.profiles.profiles.get(scope).is_some_and(|p| {
            p.kind == "developer" && p.revoked.is_none()
                && p.account.strip_prefix("acct:github:").is_some_and(|l| self.hub_owners.is_empty() || self.hub_owners.contains(&lc(l)))
        })
    }

    pub fn used(&self, scope: &str, m: Meter) -> u64 {
        self.counts.get(scope).map(|c| c.get(m)).unwrap_or(0)
    }

    /// May `scope` use `extra` more of `m` this month? Err carries the words to show (REFUSAL).
    pub fn allow(&self, scope: &str, m: Meter, extra: u64) -> Result<(), String> {
        if !m.enforced() || self.exempt(scope) { return Ok(()); }
        if self.used(scope, m).saturating_add(extra) > self.limits.of(m) { return Err(REFUSAL.to_string()); }
        Ok(())
    }

    /// How many more of `m` the scope may use this month (None: no limit applies to it).
    pub fn left(&self, scope: &str, m: Meter) -> Option<u64> {
        if !m.enforced() || self.exempt(scope) { return None; }
        Some(self.limits.of(m).saturating_sub(self.used(scope, m)))
    }

    fn is_team(&self, scope: &str) -> bool { self.teams.teamspaces.contains_key(scope) }

    /// The GitHub logins whose queue a scope's notices go to: a teamspace's owners, a profile's account.
    pub fn owners_of(&self, scope: &str) -> Vec<String> {
        if self.is_team(scope) { return self.teams.owner_logins(scope); }
        let acct = self.profiles.profiles.get(scope).map(|p| p.account.clone());
        match acct {
            Some(a) => a.strip_prefix("acct:github:").map(|l| vec![l.to_string()]).unwrap_or_default(),
            None => scope.strip_prefix("P-").filter(|l| crate::session::valid_login(l)).map(|l| vec![lc(l)]).unwrap_or_default(),
        }
    }

    fn label_of(&self, scope: &str) -> String {
        if let Some(t) = self.teams.teamspaces.get(scope) { return format!("teamspace {}", t.label); }
        match self.profiles.profiles.get(scope) {
            Some(p) => format!("profile {} ({})", p.label, scope),
            None => format!("profile {scope}"),
        }
    }

    /// Notices to raise now (Studio `item.create` bodies): at 80% and at 100% of a limit, once per scope, meter and
    /// month; never for an exempt scope. A scope that jumps past 100% gets only the 100% notice.
    pub fn due_notices(&self) -> Vec<Value> {
        let mut out = vec![];
        for (scope, c) in &self.counts {
            if self.exempt(scope) { continue; }
            for m in Meter::ALL {
                let (used, limit) = (c.get(m), self.limits.of(m));
                let p = pct(used, limit);
                let t = if p >= 100 { 100 } else if p >= WARN_PCT { WARN_PCT } else { continue };
                let id = notice_id(&self.month, m, t, scope);
                if self.notices.contains(&id) || (t == WARN_PCT && self.notices.contains(&notice_id(&self.month, m, 100, scope))) { continue; }
                out.push(self.notice(scope, m, t, used, limit, id));
            }
        }
        out
    }

    fn notice(&self, scope: &str, m: Meter, t: u64, used: u64, limit: u64, id: String) -> Value {
        let what = self.label_of(scope);
        let title = if t >= 100 { format!("Usage: {} at the monthly free limit in {what}", m.label()) }
                    else { format!("Usage: {} at {t}% of the monthly free limit in {what}", m.label()) };
        let then = match (m.enforced(), t >= 100) {
            (true, true) => format!("{REFUSAL}. {} are refused for {what} until the month ends (UTC).", capital(m.label())),
            (true, false) => format!("At 100%, {} are refused until the month ends: \"{REFUSAL}\".", m.label()),
            (false, _) => "Storage is warn-only: nothing is refused.".to_string(),
        };
        let body = format!("{used} of {limit} {} used in {} (UTC). {then} The limits are the node {LIMITS_ID}{}.",
            m.label(), self.month, if self.limits.placeholder { " (placeholder numbers for Nowed to set)" } else { "" });
        let mut v = json!({"id": id, "type": "notice", "title": title, "body": body, "status": "open", "refs": ["FUN-1", LIMITS_ID],
            "meta": {"queue": self.owners_of(scope), "urgent": true, "lane": "owner",
                     "usage": {"scope": scope, "meter": m.key(), "threshold": t, "used": used, "limit": limit, "month": self.month}}});
        if self.is_team(scope) { v["teamspace"] = json!(scope); }
        v
    }

    fn row(&self, scope: &str) -> Value {
        let c = self.counts.get(scope).copied().unwrap_or_default();
        let exempt = self.exempt(scope);
        let meters: serde_json::Map<String, Value> = Meter::ALL.iter().map(|m| {
            let (used, limit) = (c.get(*m), self.limits.of(*m));
            (m.key().to_string(), json!({"label": m.label(), "used": used, "limit": limit, "pct": pct(used, limit), "enforced": m.enforced(),
                "refused": m.enforced() && !exempt && used >= limit}))
        }).collect();
        let notices: Vec<&String> = self.notices.iter().filter(|n| n.starts_with(&format!("{NOTICE_PREFIX}{}-", self.month)) && n.ends_with(&format!("-{scope}"))).collect();
        json!({"scope": scope, "exempt": exempt, "meters": meters, "notices": notices})
    }

    /// The meters `login` sees: each of their active profiles (the window's first) and each teamspace they are a
    /// member or owner of, with this month's limits and the limits node.
    pub fn view_for(&self, login: &str, window_profile: Option<&str>) -> Value {
        let pv = self.profiles.view_for(login);
        let mut profs: Vec<Value> = pv.get("profiles").and_then(Value::as_array).cloned().unwrap_or_default().into_iter()
            .filter(|p| p.get("revoked").map(Value::is_null).unwrap_or(true)).collect();
        let active = window_profile.map(str::trim).filter(|p| !p.is_empty()).map(str::to_string).unwrap_or_else(|| profiles::personal_id(login));
        profs.sort_by_key(|p| s(p, "id") != active);
        let mut rows: Vec<Value> = profs.iter().map(|p| {
            let id = s(p, "id");
            let mut r = self.row(id);
            r["kind"] = json!("profile");
            r["label"] = json!(s(p, "label"));
            r["profile_kind"] = json!(s(p, "kind"));
            r["active"] = json!(id == active);
            r
        }).collect();
        for t in self.teams.teamspaces.values() {
            let Some(role) = self.teams.role_of(login, &t.id) else { continue };
            if role == teamspaces::Role::Guest { continue; }
            let mut r = self.row(&t.id);
            r["kind"] = json!("teamspace");
            r["label"] = json!(t.label);
            r["role"] = json!(role);
            rows.push(r);
        }
        json!({"month": self.month, "limits": self.limits, "limits_node": LIMITS_ID, "refusal": REFUSAL, "warn_pct": WARN_PCT,
               "default_limits": Limits::default_value(), "meters": rows})
    }
}

fn capital(x: &str) -> String {
    let mut c = x.chars();
    match c.next() { Some(f) => f.to_uppercase().collect::<String>() + c.as_str(), None => String::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(author: &str, kind: &str, item: Option<&str>, body: Value, ts: &str) -> Value {
        json!({"ts": ts, "kind": kind, "item": item, "body": body, "author": {"id": format!("github:{author}"), "label": author, "role": if author == "Morami49" { "owner" } else { "member" }}})
    }
    fn runner(kind: &str, item: Option<&str>, body: Value, ts: &str) -> Value {
        json!({"ts": ts, "kind": kind, "item": item, "body": body, "author": {"id": RUNNER_ID, "label": "Flow runner", "role": "agent"}, "via": "flow-runner"})
    }
    fn run(scope: &str, ai: u64, ts: &str) -> Value { runner("flow.run", None, json!({"board": "main", "ok": true, "usage": usage_body(scope, 1, ai)}), ts) }
    const NOV: &str = "2026-11-10T10:00:00.000Z";
    const OCT: &str = "2026-10-30T10:00:00.000Z";

    /// Morami49 (hub owner) has a Developer profile; TS-a is his teamspace with nassar as member and gus as guest.
    fn base() -> Vec<Value> {
        vec![
            ev("Morami49", "profile.create", None, json!({"id": "P-dev", "kind": "developer", "label": "Developer"}), "2026-11-01T00:00:00.000Z"),
            ev("Morami49", "teamspace.create", Some("TS-a"), json!({"id": "TS-a", "label": "Insydr pilot"}), "2026-11-01T00:00:01.000Z"),
            ev("Morami49", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-nassar", "role": "member"}), "2026-11-01T00:00:02.000Z"),
            ev("nassar", "member.join", Some("TS-a"), json!({"teamspace": "TS-a"}), "2026-11-01T00:00:03.000Z"),
            ev("Morami49", "item.create", None, json!({"id": "B-team", "type": "idea", "title": "board", "teamspace": "TS-a"}), "2026-11-01T00:00:04.000Z"),
        ]
    }

    fn fold_with(extra: Vec<Value>, private: &[(&str, u64)]) -> State {
        let mut evs = base();
        evs.extend(extra);
        let ids: Vec<String> = (0..evs.len()).map(|i| format!("d{i}")).collect();
        let pairs: Vec<(&str, &Value)> = ids.iter().map(String::as_str).zip(evs.iter()).collect();
        let owners = vec!["Morami49".to_string()];
        let prof = profiles::State::fold(pairs.clone());
        let teams = teamspaces::State::fold(prof.clone(), &owners, pairs.clone());
        let pb: BTreeMap<String, u64> = private.iter().map(|(l, n)| (l.to_string(), *n)).collect();
        State::fold(prof, teams, &owners, pairs, "2026-11", &pb)
    }

    #[test]
    fn counts_ai_calls_and_runs_per_scope_for_this_month_only() {
        let st = fold_with(vec![
            run("P-morami49", 3, NOV), run("P-morami49", 0, NOV), run("TS-a", 2, NOV),
            run("P-morami49", 9, OCT),   // last month
            // the live responder: one AI call per answer, on the session's meter (here the teamspace board)
            json!({"ts": NOV, "kind": "comment", "item": "B-team", "body": {"text": "ok"}, "author": {"id": RESPONDER_ID, "role": "agent"}}),
            // anyone else claiming usage counts for nothing
            ev("mal", "flow.run", None, json!({"usage": usage_body("P-morami49", 500, 500)}), NOV),
        ], &[]);
        assert_eq!((st.used("P-morami49", Meter::AiCalls), st.used("P-morami49", Meter::Runs)), (3, 2));
        assert_eq!((st.used("TS-a", Meter::AiCalls), st.used("TS-a", Meter::Runs)), (3, 1));
        // a run from before this step (no usage field) goes to its board's meter
        let legacy = runner("flow.run", None, json!({"board": "B-team", "ok": true, "steps": [{"step": "ai", "ok": true, "count": 4}, {"step": "post", "ok": true, "count": 4}]}), NOV);
        let st = fold_with(vec![legacy], &[]);
        assert_eq!((st.used("TS-a", Meter::Runs), st.used("TS-a", Meter::AiCalls)), (1, 4));
    }

    #[test]
    fn limits_are_a_versioned_node_with_placeholder_defaults() {
        let st = fold_with(vec![], &[]);
        assert_eq!((st.limits.ai_calls, st.limits.runs, st.limits.storage_bytes), (500, 2_000, 1_000_000_000));
        assert!(st.limits.placeholder && st.limits.node.is_none() && st.limits.version == 0);
        let create = ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "type": "setting", "title": "Free limits", "value": Limits::default_value().to_string()}), NOV);
        let edit = ev("Morami49", "item.edit", Some(LIMITS_ID), json!({"value": "{\"ai_calls\":800,\"runs\":3000,\"storage_bytes\":2000000000}"}), NOV);
        let forged = ev("nassar", "item.edit", Some(LIMITS_ID), json!({"value": "{\"ai_calls\":999999}"}), NOV);
        let st = fold_with(vec![create.clone()], &[]);
        assert_eq!((st.limits.node.as_deref(), st.limits.version, st.limits.placeholder), (Some(LIMITS_ID), 1, true));
        let st = fold_with(vec![create, edit, forged], &[]);
        assert_eq!((st.limits.ai_calls, st.limits.runs, st.limits.storage_bytes, st.limits.version, st.limits.placeholder), (800, 3000, 2_000_000_000, 2, false));
        assert_eq!(st.findings.len(), 1, "a non-owner's edit is kept, ignored and reported");
        assert!(st.findings[0].reason.contains("only a hub owner"));
    }

    #[test]
    fn at_the_limit_ai_calls_and_runs_are_refused_and_storage_is_only_warned() {
        let lim = ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "value": "{\"ai_calls\":5,\"runs\":2,\"storage_bytes\":10}"}), NOV);
        let st = fold_with(vec![lim, run("P-morami49", 4, NOV)], &[]);
        assert_eq!(st.allow("P-morami49", Meter::AiCalls, 1), Ok(()));
        assert_eq!(st.allow("P-morami49", Meter::AiCalls, 2), Err(REFUSAL.to_string()));
        assert_eq!(st.left("P-morami49", Meter::AiCalls), Some(1));
        assert_eq!(st.allow("P-morami49", Meter::Runs, 1), Ok(()));
        let st2 = fold_with(vec![ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "value": "{\"ai_calls\":5,\"runs\":2,\"storage_bytes\":10}"}), NOV),
                                  run("P-morami49", 0, NOV), run("P-morami49", 0, NOV)], &[]);
        assert_eq!(st2.allow("P-morami49", Meter::Runs, 1).unwrap_err(), "Monthly free limit reached; a top-up will be available soon");
        assert!(st2.used("P-morami49", Meter::StorageBytes) > 10, "well over the storage limit");
        assert_eq!(st2.allow("P-morami49", Meter::StorageBytes, 1_000), Ok(()), "storage is warn-only");
        assert_eq!(st2.left("P-morami49", Meter::StorageBytes), None);
    }

    #[test]
    fn the_hub_owners_developer_profile_is_exempt() {
        let lim = ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "value": "{\"ai_calls\":1,\"runs\":1}"}), NOV);
        let friend_dev = ev("Morami49", "profile.create", None, json!({"id": "P-f-dev", "kind": "developer", "label": "Dev", "for": "github:friend"}), NOV);
        let st = fold_with(vec![lim, friend_dev, run("P-dev", 7, NOV), run("P-dev", 0, NOV), run("P-morami49", 1, NOV), run("P-f-dev", 1, NOV)], &[]);
        assert!(st.exempt("P-dev"));
        assert_eq!(st.allow("P-dev", Meter::Runs, 1), Ok(()));
        assert_eq!(st.left("P-dev", Meter::AiCalls), None);
        assert!(!st.exempt("P-morami49"), "his Personal profile counts like anyone's");
        assert!(st.allow("P-morami49", Meter::Runs, 1).is_err());
        assert!(!st.exempt("P-f-dev"), "someone else's Developer profile isn't exempt");
        assert!(st.due_notices().iter().all(|n| n.pointer("/meta/usage/scope") != Some(&json!("P-dev"))), "no notices for the exempt profile");
    }

    #[test]
    fn notices_at_80_and_100_percent_go_to_the_owners_queue_once() {
        let lim = ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "value": "{\"ai_calls\":10,\"runs\":100}"}), NOV);
        let st = fold_with(vec![lim.clone(), run("P-morami49", 8, NOV), run("TS-a", 10, NOV)], &[]);
        let due = st.due_notices();
        let me = due.iter().find(|n| n.pointer("/meta/usage/scope") == Some(&json!("P-morami49")) && n.pointer("/meta/usage/meter") == Some(&json!("ai_calls"))).expect("80% notice");
        assert_eq!(me["id"], json!("USE-2026-11-ai_calls-80-P-morami49"));
        assert_eq!(me.pointer("/meta/urgent"), Some(&json!(true)));
        assert_eq!(me.pointer("/meta/queue"), Some(&json!(["morami49"])));
        assert!(me["body"].as_str().unwrap().contains("8 of 10 AI calls"));
        assert!(me.get("teamspace").is_none());
        let team = due.iter().find(|n| n.pointer("/meta/usage/scope") == Some(&json!("TS-a")) && n.pointer("/meta/usage/meter") == Some(&json!("ai_calls"))).expect("100% notice");
        assert_eq!(team["id"], json!("USE-2026-11-ai_calls-100-TS-a"), "jumping past 100% raises only the 100% notice");
        assert_eq!(team["teamspace"], json!("TS-a"));
        assert_eq!(team.pointer("/meta/queue"), Some(&json!(["morami49"])), "the teamspace's owners, not its members");
        assert!(team["body"].as_str().unwrap().contains(REFUSAL));
        // raised notices are on the record, so they aren't raised again
        let raised: Vec<Value> = due.iter().map(|n| runner("item.create", None, n.clone(), NOV)).collect();
        let mut evs = vec![lim, run("P-morami49", 8, NOV), run("TS-a", 10, NOV)];
        evs.extend(raised.clone());
        let st = fold_with(evs, &[]);
        assert!(st.due_notices().is_empty(), "{:?}", st.due_notices());
        // the account it is for can read it
        assert!(queued_for(&raised[0], "Morami49"));
        assert!(!queued_for(&raised[0], "nassar"));
        assert!(!queued_for(&ev("mal", "item.create", None, raised[0]["body"].clone(), NOV), "Morami49"), "only the runner's notices");
    }

    #[test]
    fn storage_is_events_plus_files_plus_private_rows() {
        let note = ev("nassar", "item.create", None, json!({"id": "N-1", "type": "idea", "title": "x"}), NOV);
        let file = ev("nassar", "comment", Some("N-1"), json!({"text": "Raw material", "raw": {"asset": "a1", "name": "q.pdf", "size": 5000}}), NOV);
        let st = fold_with(vec![note.clone(), file.clone()], &[("nassar", 700)]);
        let ev_bytes = (serde_json::to_string(&note).unwrap().len() + serde_json::to_string(&file).unwrap().len()) as u64;
        // P-nassar also wrote member.join into TS-a: that is the teamspace's
        assert_eq!(st.used("P-nassar", Meter::StorageBytes), ev_bytes + 5000 + 700);
        assert!(st.used("TS-a", Meter::StorageBytes) > 0);
        // at 80% of the storage limit a warn-only notice
        let lim = ev("Morami49", "item.create", None, json!({"id": LIMITS_ID, "value": format!("{{\"storage_bytes\":{}}}", (ev_bytes + 5700) * 10 / 9)}), NOV);
        let st = fold_with(vec![note, file, lim], &[("nassar", 700)]);
        let n = st.due_notices().into_iter().find(|n| n.pointer("/meta/usage/scope") == Some(&json!("P-nassar"))).expect("storage notice");
        assert_eq!(n.pointer("/meta/usage/meter"), Some(&json!("storage_bytes")));
        assert!(n["body"].as_str().unwrap().contains("warn-only"));
        assert_eq!(n.pointer("/meta/queue"), Some(&json!(["nassar"])));
    }

    #[test]
    fn a_run_goes_to_the_teamspace_then_the_window_then_the_boards_home() {
        let mine = json!({"ts": NOV, "kind": "item.create", "item": null, "body": {"id": "B-dev", "type": "idea"}, "author": {"id": "github:Morami49", "profile": "P-dev"}});
        let st = fold_with(vec![mine, ev("Morami49", "item.create", None, json!({"id": "B-home"}), NOV),
            ev("Morami49", "profile.assign", None, json!({"profile": "P-dev", "items": ["B-moved"]}), NOV),
            ev("Morami49", "item.create", None, json!({"id": "B-moved"}), NOV)], &[]);
        assert_eq!(st.scope_for_run("B-team", Some("P-morami49"), "Morami49"), "TS-a");
        assert_eq!(st.scope_for_run("B-home", Some("P-dev"), "Morami49"), "P-dev");
        assert_eq!(st.scope_for_run("B-dev", None, "schedule"), "P-dev", "written as the Developer profile");
        assert_eq!(st.scope_for_run("B-home", None, "schedule"), "P-morami49");
        assert_eq!(st.scope_for_run("B-moved", None, "schedule"), "P-dev", "a profile.assign link wins");
        assert_eq!(st.scope_for_run("main", None, "Morami49"), "P-morami49");
    }

    #[test]
    fn the_view_lists_your_profiles_and_teamspaces() {
        let st = fold_with(vec![run("TS-a", 1, NOV)], &[]);
        let v = st.view_for("nassar", None);
        let scopes: Vec<&str> = v["meters"].as_array().unwrap().iter().map(|r| r["scope"].as_str().unwrap()).collect();
        assert_eq!(scopes, vec!["P-nassar", "TS-a"]);
        assert_eq!(v["meters"][1]["meters"]["ai_calls"]["used"], json!(1));
        assert_eq!(v["limits"]["placeholder"], json!(true));
        let v = st.view_for("Morami49", Some("P-dev"));
        assert_eq!(v["meters"][0]["scope"], json!("P-dev"));
        assert_eq!(v["meters"][0]["exempt"], json!(true));
        assert_eq!(v["meters"][0]["active"], json!(true));
        assert!(st.view_for("gus", None)["meters"].as_array().unwrap().iter().all(|r| r["kind"] == json!("profile")));
    }
}
