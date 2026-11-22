//! Profiles (D-nodach-identity, A-spec-identity I2): accounts, linked sign-ins, profiles, item ownership and
//! tool connections, folded from Studio-shaped events in the shared record.
//!
//! Pure logic: no clock, no network. The record stays append-only: an event that breaks a rule is kept, ignored
//! and reported as a finding with its reason (the same way F-19 ignores a second create), never deleted.
//!
//! Kinds (all Studio-shaped: {ts, kind, item, body, author}):
//!   account.link       {identity, verified?, account?, revoked?}   link another sign-in to the author's account;
//!                      `revoked: true` unlinks it again (only the account it is linked to can do that)
//!   profile.create     {id, kind, label, for?}       personal | developer | work | venture | public
//!   profile.revoke     {profile}                     the Personal profile can't be revoked
//!   profile.assign     {profile, items:[..], migration?}   owner_profile links; the latest one wins
//!   connection.create  {id, profile, tool, scope, secret_ref}   a tool connection registered under a profile
//!   connection.revoke  {connection}
//!
//! Rules:
//! - One account per person. An account is named after the first sign-in seen (`acct:github:<login>`), and every
//!   author gets one implicitly, with an implicit Personal profile `P-<login>`. Linking an identity that already
//!   belongs to another account is refused: joining two accounts needs an explicit merge (not built yet).
//! - A link always points at its author's own account: `account`, if given, must be the author (a link that points
//!   at someone else's account is refused, whoever writes it). Signing in with a linked email then gives the
//!   linked account's session instead of the email's own `e-` login (19 Nov 2026, F-two-accounts-one-person).
//! - Email addresses never go into the shared record (D-nodach-trail): an email sign-in is linked as
//!   `email:sha256:<64 hex>`; a raw address is refused.
//! - A Developer profile is granted by the Nodach owner (author role `owner`) and can be revoked by the owner.
//!   Revoking a profile also switches off its connections; items assigned to it keep their link (history), and
//!   personal data is untouched.
//! - Secret values never go into events: `secret_ref` names a Worker secret (`secret:NAME`) or a private path
//!   (`private:data/users/<login>/...`).
//! - Ownership (`owner_profile`) is a link, not part of an item's id (D-nodach-ids), so moving an item between
//!   profiles never changes its id or refs. Items without a link belong to their author's Personal profile.

use serde::Serialize;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};

pub const KINDS: [&str; 6] = ["account.link", "profile.create", "profile.revoke", "profile.assign", "connection.create", "connection.revoke"];
pub const PROFILE_TYPES: [&str; 5] = ["personal", "developer", "work", "venture", "public"];
/// Most items one profile.assign may move (a migration is split into several events).
pub const MAX_ASSIGN: usize = 500;

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Account {
    pub id: String,
    pub identities: Vec<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Profile {
    pub id: String,
    pub account: String,
    pub kind: String,
    pub label: String,
    /// true for the Personal profile every account has without an event
    pub implicit: bool,
    pub created: Option<String>,
    pub revoked: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Connection {
    pub id: String,
    pub profile: String,
    pub tool: String,
    pub scope: String,
    pub secret_ref: String,
    pub created: String,
    pub revoked: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Finding {
    pub doc_id: String,
    pub kind: String,
    pub reason: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Assignment {
    pub profile: String,
    pub ts: String,
    pub doc_id: String,
    pub migration: bool,
}

#[derive(Default, Clone, Debug)]
pub struct State {
    pub accounts: BTreeMap<String, Account>,
    by_identity: HashMap<String, String>,
    pub profiles: BTreeMap<String, Profile>,
    pub connections: BTreeMap<String, Connection>,
    /// item id → its latest owner_profile link
    pub assigned: HashMap<String, Assignment>,
    pub findings: Vec<Finding>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 80 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b))
}

/// `github:<login>` (GitHub login rules) or `email:sha256:<64 lowercase hex>`. Returns the normalised form.
pub fn normalise_identity(identity: &str) -> Result<String, String> {
    if let Some(login) = identity.strip_prefix("github:") {
        return if crate::session::valid_login(login) { Ok(format!("github:{}", login.to_ascii_lowercase())) } else { Err("not a GitHub login".into()) };
    }
    if let Some(rest) = identity.strip_prefix("email:") {
        if let Some(h) = rest.strip_prefix("sha256:") {
            if h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Ok(format!("email:sha256:{}", h.to_ascii_lowercase()));
            }
            return Err("email:sha256: needs 64 hex characters".into());
        }
        return Err("email addresses never go into the shared record: link as email:sha256:<hash of the lowercased address>".into());
    }
    Err("identity must be github:<login> or email:sha256:<hash>".into())
}

/// A secret_ref names where a secret lives, never the secret itself.
fn valid_secret_ref(r: &str, login: &str) -> bool {
    if let Some(name) = r.strip_prefix("secret:") {
        return !name.is_empty() && name.len() <= 64 && name.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_');
    }
    if let Some(path) = r.strip_prefix("private:") {
        return crate::private::path_owned_by(path, login);
    }
    false
}

pub fn personal_id(login: &str) -> String {
    format!("P-{}", login.to_ascii_lowercase())
}

fn account_id(identity: &str) -> String {
    format!("acct:{identity}")
}

impl State {
    /// Fold Studio-shaped events, in record order, into the current state.
    pub fn fold<'a>(docs: impl IntoIterator<Item = (&'a str, &'a Value)>) -> Self {
        let mut st = State::default();
        for (doc_id, ev) in docs {
            st.observe(doc_id, ev);
        }
        st
    }

    /// Apply one event if it is a profile kind; a rule break becomes a finding.
    pub fn observe(&mut self, doc_id: &str, ev: &Value) {
        let kind = s(ev, "kind");
        if !KINDS.contains(&kind) {
            return;
        }
        if let Err(reason) = self.apply(ev) {
            self.findings.push(Finding { doc_id: doc_id.to_string(), kind: kind.to_string(), reason });
        }
    }

    /// Would this event be accepted now? Used before writing (check-before-write, D-nodach-parallel-guard).
    pub fn check(&self, ev: &Value) -> Result<(), String> {
        self.clone().apply(ev)
    }

    fn author_login(ev: &Value) -> Result<String, String> {
        let id = ev.pointer("/author/id").and_then(Value::as_str).unwrap_or("");
        let login = id.strip_prefix("github:").ok_or("author must be a github: identity")?;
        if !crate::session::valid_login(login) {
            return Err("author must be a github: identity".into());
        }
        Ok(login.to_ascii_lowercase())
    }

    fn is_owner(ev: &Value) -> bool {
        ev.pointer("/author/role").and_then(Value::as_str) == Some("owner")
    }

    /// The account of a GitHub login, creating it (and its Personal profile) the first time it is seen.
    fn ensure_account(&mut self, login: &str) -> String {
        let ident = format!("github:{}", login.to_ascii_lowercase());
        if let Some(a) = self.by_identity.get(&ident) {
            return a.clone();
        }
        let acct = account_id(&ident);
        self.accounts.insert(acct.clone(), Account { id: acct.clone(), identities: vec![ident.clone()] });
        self.by_identity.insert(ident, acct.clone());
        let pid = personal_id(login);
        self.profiles.entry(pid.clone()).or_insert(Profile {
            id: pid, account: acct.clone(), kind: "personal".into(), label: "Personal".into(), implicit: true, created: None, revoked: None,
        });
        acct
    }

    fn active_profile(&self, id: &str) -> Result<&Profile, String> {
        match self.profiles.get(id) {
            None => Err(format!("profile {id} doesn't exist")),
            Some(p) if p.revoked.is_some() => Err(format!("profile {id} was revoked")),
            Some(p) => Ok(p),
        }
    }

    fn apply(&mut self, ev: &Value) -> Result<(), String> {
        let login = Self::author_login(ev)?;
        let ts = s(ev, "ts").to_string();
        let b = ev.get("body").cloned().unwrap_or(Value::Null);
        let me = self.ensure_account(&login);
        match s(ev, "kind") {
            "account.link" => {
                let ident = normalise_identity(s(&b, "identity"))?;
                match s(&b, "account") {
                    "" => {}
                    a if normalise_identity(a).ok().map(|i| account_id(&i)) == Some(me.clone()) || a == me => {}
                    _ => return Err("a link can only point at your own account: sign in as that account to link it".into()),
                }
                if b.get("revoked").and_then(Value::as_bool) == Some(true) {
                    if ident == format!("github:{login}") {
                        return Err("an account's own sign-in can't be unlinked".into());
                    }
                    return match self.by_identity.get(&ident) {
                        Some(a) if *a == me => {
                            self.by_identity.remove(&ident);
                            self.accounts.get_mut(&me).expect("ensured").identities.retain(|i| *i != ident);
                            Ok(())
                        }
                        Some(_) => Err(format!("{ident} is linked to another account; only that account can unlink it")),
                        None => Err(format!("{ident} isn't linked")),
                    };
                }
                match self.by_identity.get(&ident) {
                    Some(a) if *a == me => Err(format!("{ident} is already linked to this account")),
                    Some(a) => Err(format!("{ident} already belongs to {a}; joining two accounts needs an explicit merge")),
                    None => {
                        self.by_identity.insert(ident.clone(), me.clone());
                        self.accounts.get_mut(&me).expect("ensured").identities.push(ident);
                        Ok(())
                    }
                }
            }
            "profile.create" => {
                let (id, kind, label) = (s(&b, "id"), s(&b, "kind"), s(&b, "label"));
                if !valid_id(id) {
                    return Err("profile id: 1-80 letters, digits, - _ . :".into());
                }
                if self.profiles.contains_key(id) {
                    return Err(format!("profile {id} already exists"));
                }
                if !PROFILE_TYPES.contains(&kind) {
                    return Err(format!("kind must be one of {PROFILE_TYPES:?}"));
                }
                let target = match s(&b, "for") {
                    "" => me.clone(),
                    f => {
                        let ident = normalise_identity(f)?;
                        let Some(login_for) = ident.strip_prefix("github:") else { return Err("for must be a github: identity".into()) };
                        let acct = self.ensure_account(login_for);
                        if acct != me && !Self::is_owner(ev) {
                            return Err("only the Nodach owner creates profiles for someone else".into());
                        }
                        acct
                    }
                };
                if kind == "personal" {
                    return Err("every account already has its Personal profile".into());
                }
                if kind == "developer" && !Self::is_owner(ev) {
                    return Err("Developer profiles are granted by the Nodach owner".into());
                }
                if kind == "developer" && self.profiles.values().any(|p| p.account == target && p.kind == "developer" && p.revoked.is_none()) {
                    return Err("this account already has an active Developer profile".into());
                }
                self.profiles.insert(id.to_string(), Profile {
                    id: id.to_string(), account: target, kind: kind.to_string(),
                    label: if label.is_empty() { kind.to_string() } else { label.to_string() },
                    implicit: false, created: Some(ts), revoked: None,
                });
                Ok(())
            }
            "profile.revoke" => {
                let id = match s(&b, "profile") { "" => s(ev, "item"), p => p };
                let p = self.active_profile(id)?.clone();
                if p.kind == "personal" {
                    return Err("the Personal profile can't be revoked".into());
                }
                let allowed = if p.kind == "developer" { Self::is_owner(ev) } else { p.account == me || Self::is_owner(ev) };
                if !allowed {
                    return Err(format!("you can't revoke {id}"));
                }
                self.profiles.get_mut(id).expect("checked").revoked = Some(ts.clone());
                for c in self.connections.values_mut().filter(|c| c.profile == id && c.revoked.is_none()) {
                    c.revoked = Some(ts.clone());
                }
                Ok(())
            }
            "profile.assign" => {
                let pid = s(&b, "profile");
                let p = self.active_profile(pid)?.clone();
                if p.account != me && !Self::is_owner(ev) {
                    return Err(format!("{pid} isn't one of your profiles"));
                }
                let items: Vec<&str> = b.get("items").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
                if items.is_empty() || items.len() > MAX_ASSIGN {
                    return Err(format!("items: 1 to {MAX_ASSIGN} item ids"));
                }
                let migration = b.get("migration").and_then(Value::as_bool).unwrap_or(false);
                let doc = s(ev, "idem").to_string();
                for it in items {
                    self.assigned.insert(it.to_string(), Assignment { profile: pid.to_string(), ts: ts.clone(), doc_id: doc.clone(), migration });
                }
                Ok(())
            }
            "connection.create" => {
                let (id, pid, tool, scope, sref) = (s(&b, "id"), s(&b, "profile"), s(&b, "tool"), s(&b, "scope"), s(&b, "secret_ref"));
                if !valid_id(id) || self.connections.contains_key(id) {
                    return Err(format!("connection id {id:?} is missing, malformed or taken"));
                }
                let p = self.active_profile(pid)?.clone();
                if p.account != me {
                    return Err(format!("{pid} isn't one of your profiles"));
                }
                if tool.is_empty() || tool.len() > 40 || !tool.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-') {
                    return Err("tool: a short lowercase name, e.g. github, google, notion".into());
                }
                if !valid_secret_ref(sref, &login) {
                    return Err("secret_ref must name where the secret lives (secret:NAME or private:data/users/<you>/...), never the secret itself".into());
                }
                if self.connections.values().any(|c| c.profile == pid && c.tool == tool && c.scope == scope && c.revoked.is_none()) {
                    return Err(format!("{pid} already has an active {tool} connection with this scope"));
                }
                self.connections.insert(id.to_string(), Connection {
                    id: id.into(), profile: pid.into(), tool: tool.into(), scope: scope.into(), secret_ref: sref.into(), created: ts, revoked: None,
                });
                Ok(())
            }
            "connection.revoke" => {
                let id = match s(&b, "connection") { "" => s(ev, "item"), c => c };
                let c = self.connections.get(id).ok_or(format!("connection {id} doesn't exist"))?.clone();
                if c.revoked.is_some() {
                    return Err(format!("connection {id} was already revoked"));
                }
                let owner_acct = self.profiles.get(&c.profile).map(|p| p.account.clone()).unwrap_or_default();
                if owner_acct != me && !Self::is_owner(ev) {
                    return Err(format!("you can't revoke {id}"));
                }
                self.connections.get_mut(id).expect("checked").revoked = Some(ts);
                Ok(())
            }
            k => Err(format!("{k} isn't a profile kind")),
        }
    }

    /// The profile an item belongs to: its latest owner_profile link, else its author's Personal profile.
    pub fn owner_profile(&self, item: &str, author_login: &str) -> String {
        self.assigned.get(item).map(|a| a.profile.clone()).unwrap_or_else(|| personal_id(author_login))
    }

    /// The account a sign-in belongs to, if it has been seen.
    pub fn account_of(&self, identity: &str) -> Option<&String> {
        normalise_identity(identity).ok().and_then(|i| self.by_identity.get(&i))
    }

    /// The GitHub login an email sign-in is linked to, given the email's full SHA-256 (hex), if any.
    pub fn linked_login_for_email_hash(&self, hash: &str) -> Option<String> {
        let acct = self.account_of(&format!("email:sha256:{}", hash.trim()))?;
        acct.strip_prefix("acct:github:").map(str::to_string)
    }

    /// The login a sign-in by email gets: the linked account's login if that email is linked, else `e-<hash16>`.
    pub fn sign_in_login_for_email_hash(&self, hash: &str) -> Option<String> {
        self.linked_login_for_email_hash(hash).or_else(|| crate::session::login_for_hash(hash))
    }

    /// The same for an emailed sign-in link, which carries only the `e-<hash16>` login: a linked email whose hash
    /// starts with those 16 hex characters gives its account (none if two linked hashes share them; then the
    /// `e-` login is kept unchanged).
    pub fn sign_in_login_for_email_login(&self, e_login: &str) -> String {
        let Some(prefix) = e_login.to_ascii_lowercase().strip_prefix(crate::session::EMAIL_LOGIN_PREFIX).map(str::to_string) else { return e_login.to_string() };
        if prefix.len() != 16 { return e_login.to_string(); }
        let hits: Vec<&String> = self.by_identity.iter()
            .filter(|(i, _)| i.strip_prefix("email:sha256:").is_some_and(|h| h.starts_with(&prefix)))
            .map(|(_, a)| a).collect();
        match hits.as_slice() {
            [a] => a.strip_prefix("acct:github:").map(str::to_string).unwrap_or_else(|| e_login.to_string()),
            _ => e_login.to_string(),
        }
    }

    /// Everything one login may see about its own account: profiles (implicit Personal included), connections
    /// (secret_ref only, never values), assignment counts per profile, and findings on its events.
    pub fn view_for(&self, login: &str) -> Value {
        let ident = format!("github:{}", login.to_ascii_lowercase());
        let acct = self.by_identity.get(&ident).cloned().unwrap_or_else(|| account_id(&ident));
        let account = self.accounts.get(&acct).cloned().unwrap_or(Account { id: acct.clone(), identities: vec![ident] });
        let mut profiles: Vec<Profile> = self.profiles.values().filter(|p| p.account == acct).cloned().collect();
        if !profiles.iter().any(|p| p.kind == "personal") {
            let pid = personal_id(login);
            profiles.insert(0, Profile { id: pid, account: acct.clone(), kind: "personal".into(), label: "Personal".into(), implicit: true, created: None, revoked: None });
        }
        let ids: Vec<&str> = profiles.iter().map(|p| p.id.as_str()).collect();
        let connections: Vec<&Connection> = self.connections.values().filter(|c| ids.contains(&c.profile.as_str())).collect();
        let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
        for a in self.assigned.values().filter(|a| ids.contains(&a.profile.as_str())) {
            *counts.entry(a.profile.as_str()).or_default() += 1;
        }
        serde_json::json!({"account": account, "profiles": profiles, "connections": connections,
            "assigned_items": counts, "findings": self.findings})
    }
}

/// Which bucket an existing item goes to in the one-time migration (A-spec-identity I2).
#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Bucket {
    Developer,
    Personal,
    Review,
}

/// Title tags at the start of a title, e.g. "[PERSONAL][PLATFORM] LOOP: ..." → ["PERSONAL", "PLATFORM"].
pub fn title_tags(title: &str) -> Vec<String> {
    let mut out = vec![];
    let mut rest = title.trim_start();
    while let Some(r) = rest.strip_prefix('[') {
        let Some(end) = r.find(']') else { break };
        out.push(r[..end].trim().to_ascii_uppercase());
        rest = r[end + 1..].trim_start();
    }
    out
}

/// Migration rule for one item from its title tags. [PLATFORM] only → Developer; [PERSONAL] only → Personal;
/// both → `mixed`; no tag → `untagged`. The two policies are Nowed's call (asked in the reply that ships this).
pub fn bucket(title: &str, mixed: Bucket, untagged: Bucket) -> Bucket {
    let t = title_tags(title);
    match (t.iter().any(|x| x == "PLATFORM"), t.iter().any(|x| x == "PERSONAL")) {
        (true, false) => Bucket::Developer,
        (false, true) => Bucket::Personal,
        (true, true) => mixed,
        (false, false) => untagged,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(kind: &str, login: &str, role: &str, body: Value) -> Value {
        json!({"ts": "2026-11-16T22:00:00.000Z", "kind": kind, "item": null, "body": body,
               "author": {"id": format!("github:{login}"), "role": role}})
    }

    fn fold(evs: &[Value]) -> State {
        let ids: Vec<String> = (0..evs.len()).map(|i| format!("d{i}")).collect();
        State::fold(ids.iter().map(String::as_str).zip(evs.iter()))
    }

    #[test]
    fn every_author_has_an_account_and_a_personal_profile() {
        let st = fold(&[ev("profile.create", "Morami49", "owner", json!({"id": "P-dev", "kind": "developer", "label": "Developer"}))]);
        assert_eq!(st.account_of("github:morami49").unwrap(), "acct:github:morami49");
        assert_eq!(st.profiles["P-morami49"].kind, "personal");
        assert!(st.profiles["P-morami49"].implicit);
        assert_eq!(st.profiles["P-dev"].account, "acct:github:morami49");
        assert!(st.findings.is_empty());
    }

    #[test]
    fn only_the_owner_grants_developer_profiles() {
        let st = fold(&[
            ev("profile.create", "friend", "member", json!({"id": "P-f-dev", "kind": "developer"})),
            ev("profile.create", "Morami49", "owner", json!({"id": "P-f-dev2", "kind": "developer", "for": "github:friend"})),
            ev("profile.create", "Morami49", "owner", json!({"id": "P-f-dev3", "kind": "developer", "for": "github:friend"})),
        ]);
        assert!(!st.profiles.contains_key("P-f-dev"));
        assert_eq!(st.profiles["P-f-dev2"].account, "acct:github:friend");
        assert!(!st.profiles.contains_key("P-f-dev3"), "one active Developer profile per account");
        assert_eq!(st.findings.len(), 2);
        assert!(st.findings[0].reason.contains("granted by the Nodach owner"));
    }

    #[test]
    fn personal_profiles_are_implicit_and_permanent() {
        let st = fold(&[
            ev("profile.create", "a", "member", json!({"id": "P-x", "kind": "personal"})),
            ev("profile.revoke", "a", "owner", json!({"profile": "P-a"})),
        ]);
        assert_eq!(st.findings.len(), 2);
        assert!(st.profiles["P-a"].revoked.is_none());
    }

    #[test]
    fn linking_never_joins_two_accounts_silently() {
        let h = "a".repeat(64);
        let st = fold(&[
            ev("account.link", "a", "member", json!({"identity": format!("email:sha256:{h}")})),
            ev("account.link", "b", "member", json!({"identity": format!("email:sha256:{}", h.to_uppercase())})),
            ev("account.link", "a", "member", json!({"identity": "github:b"})),
            ev("account.link", "a", "member", json!({"identity": "email:me@example.com"})),
        ]);
        assert_eq!(st.account_of(&format!("email:sha256:{h}")).unwrap(), "acct:github:a");
        assert_eq!(st.findings.len(), 3);
        assert!(st.findings[0].reason.contains("explicit merge"), "same hash, other account");
        assert!(st.findings[1].reason.contains("explicit merge"), "b already has its own account");
        assert!(st.findings[2].reason.contains("never go into the shared record"));
    }

    // F-two-accounts-one-person (19 Nov 2026): one person signing in two ways must stay one person.
    fn gmail() -> String { crate::session::email_hash("Owner.Test@Gmail.com") }

    #[test]
    fn a_linked_email_signs_in_as_the_owner() {
        let h = gmail();
        let st = fold(&[ev("account.link", "Morami49", "owner", json!({"identity": format!("email:sha256:{h}"), "account": "github:Morami49"}))]);
        assert!(st.findings.is_empty(), "{:?}", st.findings);
        assert_eq!(st.sign_in_login_for_email_hash(&h).as_deref(), Some("morami49"));
        // the emailed link carries only e-<hash16>; it resolves the same way
        let e = crate::session::email_login("owner.test@gmail.com");
        assert_eq!(st.sign_in_login_for_email_login(&e), "morami49");
    }

    #[test]
    fn an_unlinked_email_stays_its_own_e_login() {
        let st = fold(&[ev("account.link", "Morami49", "owner", json!({"identity": format!("email:sha256:{}", gmail())}))]);
        let other = crate::session::email_hash("someone@example.com");
        assert_eq!(st.sign_in_login_for_email_hash(&other), crate::session::login_for_hash(&other));
        let e = crate::session::email_login("someone@example.com");
        assert_eq!(st.sign_in_login_for_email_login(&e), e);
        assert_eq!(st.sign_in_login_for_email_login("Morami49"), "Morami49", "non-email logins pass through");
    }

    #[test]
    fn a_link_for_someone_elses_account_is_ignored() {
        let h = gmail();
        let st = fold(&[
            // a member tries to point the owner's Gmail at the owner (not their account) and at themselves via `account`
            ev("account.link", "friend", "member", json!({"identity": format!("email:sha256:{h}"), "account": "github:Morami49"})),
            ev("account.link", "friend", "owner", json!({"identity": format!("email:sha256:{h}"), "account": "acct:github:morami49"})),
        ]);
        assert_eq!(st.findings.len(), 2, "{:?}", st.findings);
        assert!(st.findings.iter().all(|f| f.reason.contains("your own account")));
        assert_eq!(st.sign_in_login_for_email_hash(&h), crate::session::login_for_hash(&h));
        // once the owner links it, a later claim by someone else is refused, not merged
        let st = fold(&[
            ev("account.link", "Morami49", "owner", json!({"identity": format!("email:sha256:{h}")})),
            ev("account.link", "friend", "member", json!({"identity": format!("email:sha256:{h}")})),
        ]);
        assert_eq!(st.sign_in_login_for_email_hash(&h).as_deref(), Some("morami49"));
        assert!(st.findings[0].reason.contains("explicit merge"));
    }

    #[test]
    fn revoking_a_link_reverts_the_sign_in() {
        let h = gmail();
        let link = json!({"identity": format!("email:sha256:{h}")});
        let unlink = json!({"identity": format!("email:sha256:{h}"), "revoked": true});
        let st = fold(&[
            ev("account.link", "Morami49", "owner", link.clone()),
            ev("account.link", "friend", "member", unlink.clone()),
            ev("account.link", "Morami49", "owner", unlink.clone()),
        ]);
        assert_eq!(st.findings.len(), 1, "only the linked account can unlink: {:?}", st.findings);
        assert_eq!(st.sign_in_login_for_email_hash(&h), crate::session::login_for_hash(&h));
        assert!(!st.accounts["acct:github:morami49"].identities.iter().any(|i| i.starts_with("email:")));
        // relinking after a revoke works, and the account's own sign-in can never be unlinked
        let st = fold(&[ev("account.link", "Morami49", "owner", link.clone()), ev("account.link", "Morami49", "owner", unlink), ev("account.link", "Morami49", "owner", link)]);
        assert_eq!(st.sign_in_login_for_email_hash(&h).as_deref(), Some("morami49"));
        assert!(st.check(&ev("account.link", "Morami49", "owner", json!({"identity": "github:Morami49", "revoked": true}))).is_err());
    }

    #[test]
    fn revoking_a_developer_profile_switches_off_its_connections_and_keeps_personal_ones() {
        let st = fold(&[
            ev("profile.create", "Morami49", "owner", json!({"id": "P-dev", "kind": "developer"})),
            ev("connection.create", "Morami49", "owner", json!({"id": "C-gh-dev", "profile": "P-dev", "tool": "github", "scope": "repo", "secret_ref": "secret:GITHUB_DEV"})),
            ev("connection.create", "Morami49", "owner", json!({"id": "C-gh-me", "profile": "P-morami49", "tool": "github", "scope": "repo", "secret_ref": "private:data/users/github:Morami49/keys/github"})),
            ev("profile.revoke", "Morami49", "owner", json!({"profile": "P-dev"})),
        ]);
        assert!(st.findings.is_empty(), "{:?}", st.findings);
        assert!(st.connections["C-gh-dev"].revoked.is_some());
        assert!(st.connections["C-gh-me"].revoked.is_none(), "Q55b: two connections, so personal access survives");
        let later = st.check(&ev("profile.assign", "Morami49", "owner", json!({"profile": "P-dev", "items": ["D-1"]})));
        assert!(later.unwrap_err().contains("revoked"));
    }

    #[test]
    fn secrets_never_go_into_events() {
        let st = fold(&[ev("profile.create", "a", "owner", json!({"id": "P-w", "kind": "work"}))]);
        for bad in ["ghp_abc123", "secret:lower", "sk-live-xyz", "private:data/users/b/keys", "", "secret:"] {
            let r = st.check(&ev("connection.create", "a", "owner", json!({"id": "C1", "profile": "P-w", "tool": "google", "scope": "", "secret_ref": bad})));
            assert!(r.is_err(), "{bad} must be refused");
        }
        assert!(st.check(&ev("connection.create", "a", "owner", json!({"id": "C1", "profile": "P-w", "tool": "google", "scope": "calendar.read", "secret_ref": "secret:GOOGLE_WORK"}))).is_ok());
    }

    #[test]
    fn ownership_is_a_link_and_the_latest_one_wins() {
        let st = fold(&[
            ev("profile.create", "Morami49", "owner", json!({"id": "P-dev", "kind": "developer"})),
            ev("profile.assign", "Morami49", "owner", json!({"profile": "P-dev", "items": ["D-171", "A-loop"], "migration": true})),
            ev("profile.assign", "Morami49", "owner", json!({"profile": "P-morami49", "items": ["A-loop"]})),
        ]);
        assert_eq!(st.owner_profile("D-171", "Morami49"), "P-dev");
        assert_eq!(st.owner_profile("A-loop", "Morami49"), "P-morami49");
        assert_eq!(st.owner_profile("I-new", "Morami49"), "P-morami49", "default: the author's Personal profile");
        assert!(st.assigned["D-171"].migration);
    }

    #[test]
    fn nobody_assigns_items_to_someone_elses_profile() {
        let st = fold(&[ev("profile.create", "a", "member", json!({"id": "P-aw", "kind": "work"}))]);
        assert!(st.check(&ev("profile.assign", "b", "member", json!({"profile": "P-aw", "items": ["X"]}))).is_err());
        assert!(st.check(&ev("profile.assign", "a", "member", json!({"profile": "P-aw", "items": []}))).is_err());
        let many: Vec<String> = (0..=MAX_ASSIGN).map(|i| format!("I-{i}")).collect();
        assert!(st.check(&ev("profile.assign", "a", "member", json!({"profile": "P-aw", "items": many}))).is_err());
    }

    #[test]
    fn check_does_not_change_the_state() {
        let st = fold(&[]);
        assert!(st.check(&ev("profile.create", "a", "member", json!({"id": "P-v", "kind": "venture"}))).is_ok());
        assert!(st.profiles.is_empty());
    }

    #[test]
    fn non_profile_events_are_ignored() {
        let st = fold(&[ev("comment", "a", "owner", json!({"text": "hi"}))]);
        assert!(st.accounts.is_empty() && st.findings.is_empty());
    }

    #[test]
    fn the_view_shows_only_your_own_account() {
        let st = fold(&[
            ev("profile.create", "Morami49", "owner", json!({"id": "P-dev", "kind": "developer"})),
            ev("profile.create", "friend", "member", json!({"id": "P-fw", "kind": "work"})),
        ]);
        let v = st.view_for("Morami49");
        let ids: Vec<&str> = v["profiles"].as_array().unwrap().iter().map(|p| p["id"].as_str().unwrap()).collect();
        assert_eq!(ids, vec!["P-dev", "P-morami49"]);
        let fresh = st.view_for("newcomer");
        assert_eq!(fresh["profiles"][0]["id"], "P-newcomer");
    }

    #[test]
    fn migration_buckets_follow_the_title_tags() {
        assert_eq!(title_tags("[PERSONAL][PLATFORM] LOOP"), vec!["PERSONAL", "PLATFORM"]);
        assert_eq!(title_tags(" [platform] x [PERSONAL]"), vec!["PLATFORM"], "only leading tags count");
        assert_eq!(bucket("[PLATFORM] Identity", Bucket::Review, Bucket::Developer), Bucket::Developer);
        assert_eq!(bucket("[PERSONAL] Shift rules", Bucket::Review, Bucket::Developer), Bucket::Personal);
        assert_eq!(bucket("[PLATFORM][PERSONAL] Work lane", Bucket::Review, Bucket::Developer), Bucket::Review);
        assert_eq!(bucket("Build order", Bucket::Review, Bucket::Developer), Bucket::Developer);
    }
}
