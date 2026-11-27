//! Multi-chat guard (D-nodach-parallel-guard v2, C12 d, C13).
//!
//! Nowed works on Nodach across many chats and tools on purpose. The guard lets them work in parallel and only
//! stops what went wrong three times on 16–17 Nov: two sessions with the same role answering the same message and
//! changing the same items.
//!
//! - Duplicate-message check: a session records a fingerprint (a hash, never the text) of the message it answers.
//!   A second session answering the same message is told which session got there first, and its reply id.
//! - Locks by item: a session claims the items it is about to change, for a short time. Other sessions may only
//!   comment on a claimed item until the claim is released or expires.
//! - Roles: builder, interviewer, auditor, explorer. An auditor writes comments, answers, links and findings only,
//!   so it can disagree without being blocked. An explorer writes only to its own Studio branch.
//!
//! Claims are ordinary Studio-shaped events (`session.claim`, `session.release`) in the shared record, so every
//! chat and tool sees the same claims and the history shows who worked on what. Pure logic: the node passes `now`.

use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};

pub const ROLES: [&str; 4] = ["builder", "interviewer", "auditor", "explorer"];
pub const DEFAULT_MINUTES: i64 = 15;
pub const MAX_MINUTES: i64 = 60;
pub const MAX_ITEMS: usize = 50;
const AUDITOR_KINDS: [&str; 3] = ["comment", "answer", "link"];

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Claim {
    pub session: String,
    pub by: String,
    pub role: String,
    pub items: Vec<String>,
    pub fingerprint: String,
    pub branch: String,
    pub since: String,
    pub until_ms: i64,
    pub released: Option<String>,
    /// the reply item (R-...) the session wrote, once released
    pub reply: Option<String>,
    /// the Brief this session works for (BQ-in-flight-briefs); its pause or stop binds the session
    #[serde(skip_serializing_if = "Option::is_none")]
    pub brief: Option<String>,
}

#[derive(Default, Clone, Debug)]
pub struct State {
    pub claims: BTreeMap<String, Claim>,
    order: Vec<String>,
    pub branches: HashSet<String>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

/// The message fingerprint: SHA-256 over the lowercased message with whitespace collapsed, first 16 hex characters.
/// The message itself is never stored.
pub fn fingerprint(message: &str) -> String {
    let norm: String = message.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase();
    hex::encode(Sha256::digest(norm.as_bytes()))[..16].to_string()
}

pub fn session_id(fp: &str, login: &str, now: i64) -> String {
    let h = hex::encode(Sha256::digest(format!("{fp}\0{}\0{now}", login.to_ascii_lowercase()).as_bytes()));
    format!("S-{}", &h[..12])
}

pub fn valid_branch(name: &str) -> bool {
    name != "main" && !name.is_empty() && name.len() <= 60 && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
}

impl State {
    pub fn fold<'a>(docs: impl IntoIterator<Item = &'a Value>) -> Self {
        let mut st = State::default();
        for ev in docs {
            st.observe(ev);
        }
        st
    }

    pub fn observe(&mut self, ev: &Value) {
        let b = ev.get("body").cloned().unwrap_or(Value::Null);
        match s(ev, "kind") {
            "branch.create" => {
                if let Some(n) = b.get("name").and_then(Value::as_str) {
                    self.branches.insert(n.to_string());
                }
            }
            "session.claim" => {
                let session = s(&b, "session").to_string();
                if session.is_empty() || self.claims.contains_key(&session) {
                    return;
                }
                let items = b.get("items").and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(Value::as_str).map(String::from).collect()).unwrap_or_default();
                self.claims.insert(session.clone(), Claim {
                    session: session.clone(),
                    by: ev.pointer("/author/id").and_then(Value::as_str).unwrap_or("").to_string(),
                    role: s(&b, "role").to_string(),
                    items,
                    fingerprint: s(&b, "fingerprint").to_string(),
                    branch: match s(&b, "branch") { "" => "main".into(), x => x.into() },
                    since: s(ev, "ts").to_string(),
                    until_ms: b.get("until_ms").and_then(Value::as_i64).unwrap_or(0),
                    released: None,
                    reply: None,
                    brief: Some(s(&b, "brief").to_string()).filter(|x| !x.is_empty()),
                });
                self.order.push(session);
            }
            "session.release" => {
                if let Some(c) = self.claims.get_mut(s(&b, "session")) {
                    if c.released.is_none() {
                        c.released = Some(s(ev, "ts").to_string());
                        c.reply = b.get("reply").and_then(Value::as_str).map(String::from);
                    }
                }
            }
            _ => {}
        }
    }

    pub fn is_active(c: &Claim, now: i64) -> bool {
        c.released.is_none() && now < c.until_ms
    }

    /// Active claims, oldest first.
    pub fn active(&self, now: i64) -> Vec<&Claim> {
        self.order.iter().filter_map(|id| self.claims.get(id)).filter(|c| Self::is_active(c, now)).collect()
    }

    /// The first earlier session that answered (or is answering) the same message.
    pub fn duplicate_of(&self, fp: &str) -> Option<&Claim> {
        self.order.iter().filter_map(|id| self.claims.get(id)).find(|c| c.fingerprint == fp)
    }

    /// Which of `items` another active session has claimed: (item, that claim).
    pub fn locked<'a>(&'a self, items: &[String], now: i64, except: Option<&str>) -> Vec<(String, &'a Claim)> {
        let mut out = vec![];
        for it in items {
            if let Some(c) = self.active(now).into_iter().find(|c| Some(c.session.as_str()) != except && c.items.contains(it)) {
                out.push((it.clone(), c));
            }
        }
        out
    }

    /// May this write go ahead? Returns the branch to write to. `session` is the writer's claim, if it made one.
    /// `steer` marks a pause, stop, resume or redirect of a Brief: claims don't lock those, so whoever runs agents can
    /// always stop one that holds its Brief (BQ-in-flight-briefs).
    pub fn check_write(&self, session: Option<&str>, kind: &str, item: Option<&str>, body: &Value, now: i64, steer: bool) -> Result<String, String> {
        let mine = match session {
            None => None,
            Some(id) => {
                let c = self.claims.get(id).ok_or(format!("session {id} doesn't exist; call begin_answer first"))?;
                if c.released.is_some() {
                    return Err(format!("session {id} already ended; call begin_answer again"));
                }
                if now >= c.until_ms {
                    return Err(format!("session {id} expired; call begin_answer again (then read recent events: others may have written)"));
                }
                Some(c)
            }
        };
        if let Some(c) = mine {
            if c.role == "auditor" {
                let finding = kind == "item.create" && s(body, "type") == "finding";
                if !AUDITOR_KINDS.contains(&kind) && !finding {
                    return Err("an auditor writes comments, answers, links and findings, not edits or status changes: post a comment or a finding instead".into());
                }
            }
        }
        let target = if kind == "item.create" { s(body, "id") } else { item.unwrap_or("") };
        if !target.is_empty() && kind != "comment" && !steer {
            if let Some((_, other)) = self.locked(&[target.to_string()], now, session).first() {
                return Err(format!("{target} is claimed by session {} ({}) until {}; comment on it, or read its writes and wait",
                    other.session, other.role, crate::mcp::iso(other.until_ms)));
            }
        }
        Ok(mine.map(|c| c.branch.clone()).unwrap_or_else(|| "main".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const NOW: i64 = 1_763_400_000_000;

    fn claim(session: &str, role: &str, items: &[&str], fp: &str, until: i64, branch: &str) -> Value {
        json!({"ts": "2026-11-17T16:00:00.000Z", "kind": "session.claim", "item": null, "author": {"id": "github:Morami49"},
               "body": {"session": session, "role": role, "items": items, "fingerprint": fp, "until_ms": until, "branch": branch}})
    }
    fn release(session: &str, reply: &str) -> Value {
        json!({"ts": "2026-11-17T16:05:00.000Z", "kind": "session.release", "item": null, "body": {"session": session, "reply": reply}})
    }

    #[test]
    fn fingerprints_ignore_spacing_and_case_and_hide_the_text() {
        assert_eq!(fingerprint("c11) d\nc12)  D"), fingerprint("C11) d c12) d"));
        assert_ne!(fingerprint("c11) d"), fingerprint("c11) c"));
        assert_eq!(fingerprint("x").len(), 16);
    }

    #[test]
    fn the_second_session_on_the_same_message_is_told_about_the_first() {
        let fp = fingerprint("d. on point 3, I cannot afford a solicitor");
        let st = State::fold(&[claim("S-1", "builder", &["A-spec-identity"], &fp, NOW + 60_000, ""), release("S-1", "R-261117-7")]);
        let first = st.duplicate_of(&fp).unwrap();
        assert_eq!((first.session.as_str(), first.reply.as_deref()), ("S-1", Some("R-261117-7")));
        assert!(st.duplicate_of(&fingerprint("something else")).is_none());
    }

    #[test]
    fn claimed_items_take_only_comments_from_others() {
        let st = State::fold(&[claim("S-1", "builder", &["A-spec-identity"], "f1", NOW + 60_000, "")]);
        let edit = st.check_write(Some("S-x"), "item.edit", Some("A-spec-identity"), &json!({}), NOW, false);
        assert!(edit.unwrap_err().contains("doesn't exist"));
        let anon = st.check_write(None, "item.edit", Some("A-spec-identity"), &json!({}), NOW, false).unwrap_err();
        assert!(anon.contains("claimed by session S-1 (builder)"));
        assert!(st.check_write(None, "comment", Some("A-spec-identity"), &json!({"text": "x"}), NOW, false).is_ok());
        assert!(st.check_write(None, "item.edit", Some("D-other"), &json!({}), NOW, false).is_ok(), "other items stay free");
        assert!(st.check_write(Some("S-1"), "item.edit", Some("A-spec-identity"), &json!({}), NOW, false).is_ok(), "the owner of the claim may write");
        let dup_create = st.check_write(None, "item.create", None, &json!({"id": "A-spec-identity"}), NOW, false);
        assert!(dup_create.is_err(), "creating a claimed id is blocked too");
        // BQ-in-flight-briefs: a claim never stops someone pausing or stopping the Brief it holds
        assert!(st.check_write(None, "status", Some("A-spec-identity"), &json!({"status": "paused"}), NOW, true).is_ok());
    }

    #[test]
    fn claims_end_on_release_or_expiry() {
        let st = State::fold(&[claim("S-1", "builder", &["X"], "f", NOW + 1000, ""), claim("S-2", "builder", &["Y"], "g", NOW + 60_000, ""), release("S-2", "R-1")]);
        assert!(st.check_write(None, "status", Some("X"), &json!({}), NOW, false).is_err());
        assert!(st.check_write(None, "status", Some("X"), &json!({}), NOW + 1000, false).is_ok(), "expired");
        assert!(st.check_write(None, "status", Some("Y"), &json!({}), NOW, false).is_ok(), "released");
        assert!(st.check_write(Some("S-2"), "status", Some("Y"), &json!({}), NOW, false).unwrap_err().contains("already ended"));
        assert!(st.check_write(Some("S-1"), "status", Some("X"), &json!({}), NOW + 1000, false).unwrap_err().contains("expired"));
        assert_eq!(st.active(NOW).len(), 1);
    }

    #[test]
    fn auditors_disagree_by_comment_or_finding_never_by_edit() {
        let st = State::fold(&[claim("S-a", "auditor", &[], "f", NOW + 60_000, "")]);
        for k in ["comment", "answer", "link"] {
            assert!(st.check_write(Some("S-a"), k, Some("D-1"), &json!({}), NOW, false).is_ok(), "{k}");
        }
        assert!(st.check_write(Some("S-a"), "item.create", None, &json!({"id": "F-9", "type": "finding"}), NOW, false).is_ok());
        for k in ["item.edit", "status"] {
            assert!(st.check_write(Some("S-a"), k, Some("D-1"), &json!({}), NOW, false).unwrap_err().contains("auditor"));
        }
        assert!(st.check_write(Some("S-a"), "item.create", None, &json!({"id": "D-9", "type": "decision"}), NOW, false).is_err());
    }

    #[test]
    fn explorers_write_to_their_own_branch() {
        let st = State::fold(&[claim("S-e", "explorer", &["D-work-lane"], "f", NOW + 60_000, "what-if-shifts-first")]);
        assert_eq!(st.check_write(Some("S-e"), "item.edit", Some("D-work-lane"), &json!({}), NOW, false).unwrap(), "what-if-shifts-first");
        assert_eq!(st.check_write(None, "comment", Some("D-1"), &json!({}), NOW, false).unwrap(), "main");
        assert!(valid_branch("what-if-1") && !valid_branch("main") && !valid_branch("a b"));
    }

    #[test]
    fn locked_lists_each_claimed_item_once_and_ignores_my_own_claim() {
        let st = State::fold(&[claim("S-1", "builder", &["A", "B"], "f", NOW + 60_000, "")]);
        let l = st.locked(&["A".into(), "B".into(), "C".into()], NOW, None);
        assert_eq!(l.iter().map(|(i, _)| i.as_str()).collect::<Vec<_>>(), ["A", "B"]);
        assert!(st.locked(&["A".into()], NOW, Some("S-1")).is_empty());
    }
}
