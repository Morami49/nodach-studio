//! Briefs and In flight (BQ-in-flight-briefs; 215b a, 215c c, 221c a; D-access-model).
//!
//! A Brief hands one piece of work to a person or an agent. It is an ordinary item (`item.create`, type `brief`) whose
//! `meta` holds what 215b a set: goal, done_means, limits {touch, risk}, holder, budget, deadline. Everything else is
//! ordinary events on it:
//!
//!   status   {status: paused | stopped | running}   pause, stop or resume it (running = carry on after a pause)
//!   status   {status: done}                         the work is finished
//!   comment  {text, redirect: true}                 redirect: new direction the holder must follow from its next step
//!   comment  {text, report: {did, why, files?, prs?}} a report from the holder: what it did and why (221c a)
//!
//! Who may steer: a status change or a redirect on a Brief needs the `run_agents` switch where the Brief lives
//! (Owners hold every switch). `teamspaces::decide` enforces that for Studio and the connector alike.
//!
//! How an agent obeys: it names its Brief in `begin_answer` (`brief`). While that Brief is paused or stopped, every
//! write from the session is refused except a report (a comment) on the Brief itself, and `begin_answer` refuses to
//! start. Each write's result carries the Brief's state and any redirect written since the session began, so the agent
//! reads it before its next step without a separate call. Pure logic: the node passes `now`.

use serde::Serialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;

pub const TYPE: &str = "brief";
/// Statuses that only someone with `run_agents` sets. A plain `done` is a status too, so it needs the switch as well.
pub const HALTED: [&str; 2] = ["paused", "stopped"];
/// A Brief with one of these statuses has left In flight.
pub const FINISHED: [&str; 2] = ["done", "stopped"];

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Note {
    pub text: String,
    pub by: String,
    pub ts: String,
    #[serde(skip_serializing_if = "Value::is_null")]
    pub report: Value,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Brief {
    pub id: String,
    pub title: String,
    pub teamspace: Option<String>,
    pub goal: String,
    pub done_means: String,
    pub limits: Value,
    pub holder: String,
    pub budget: String,
    pub deadline: String,
    pub status: String,
    pub status_by: String,
    pub status_ts: String,
    pub since: String,
    pub redirect: Option<Note>,
    pub last_report: Option<Note>,
    pub reports: usize,
}

#[derive(Default, Clone, Debug)]
pub struct State {
    pub briefs: BTreeMap<String, Brief>,
    order: Vec<String>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str { v.get(k).and_then(Value::as_str).unwrap_or("") }
fn author(ev: &Value) -> String {
    let a = ev.pointer("/author/id").and_then(Value::as_str).unwrap_or("");
    a.strip_prefix("github:").unwrap_or(a).to_string()
}
fn text(v: &Value, k: &str) -> String {
    match v.get(k) { Some(Value::String(x)) => x.chars().take(2000).collect(), Some(Value::Null) | None => String::new(), Some(x) => x.to_string() }
}

/// Is this event a status change or a redirect on a Brief? (`item_type` answers "what type is this item?")
pub fn is_steer(ev: &Value, item_type: impl Fn(&str) -> Option<String>) -> bool {
    let kind = s(ev, "kind");
    let item = s(ev, "item");
    if item.is_empty() || item_type(item).as_deref() != Some(TYPE) { return false; }
    kind == "status" || (kind == "comment" && ev.pointer("/body/redirect").and_then(Value::as_bool) == Some(true))
}

impl State {
    pub fn fold<'a>(docs: impl IntoIterator<Item = &'a Value>) -> Self {
        let mut st = State::default();
        for ev in docs { st.observe(ev); }
        st
    }

    fn apply_meta(b: &mut Brief, m: &Value) {
        if m.get("goal").is_some() { b.goal = text(m, "goal"); }
        if m.get("done_means").is_some() { b.done_means = text(m, "done_means"); }
        if let Some(l) = m.get("limits") { b.limits = l.clone(); }
        if m.get("holder").is_some() { b.holder = text(m, "holder"); }
        if m.get("budget").is_some() { b.budget = text(m, "budget"); }
        if m.get("deadline").is_some() { b.deadline = text(m, "deadline"); }
    }

    pub fn observe(&mut self, ev: &Value) {
        let body = ev.get("body").cloned().unwrap_or(Value::Null);
        let ts = s(ev, "ts").to_string();
        match s(ev, "kind") {
            "item.create" if s(&body, "type") == TYPE => {
                let id = s(&body, "id").to_string();
                if id.is_empty() || self.briefs.contains_key(&id) { return; } // F-19: the first create wins
                let mut b = Brief {
                    id: id.clone(), title: text(&body, "title"),
                    teamspace: Some(s(&body, "teamspace").to_string()).filter(|t| !t.is_empty()),
                    goal: String::new(), done_means: String::new(), limits: Value::Null, holder: String::new(),
                    budget: String::new(), deadline: String::new(),
                    status: match s(&body, "status") { "" => "open".into(), x => x.into() },
                    status_by: author(ev), status_ts: ts.clone(), since: ts, redirect: None, last_report: None, reports: 0,
                };
                if b.goal.is_empty() { b.goal = text(&body, "body"); }
                Self::apply_meta(&mut b, body.get("meta").unwrap_or(&Value::Null));
                self.briefs.insert(id.clone(), b);
                self.order.push(id);
            }
            "item.edit" => {
                if let Some(b) = self.briefs.get_mut(s(ev, "item")) {
                    if body.get("title").is_some() { b.title = text(&body, "title"); }
                    Self::apply_meta(b, body.get("meta").unwrap_or(&Value::Null));
                }
            }
            "status" => {
                if let Some(b) = self.briefs.get_mut(s(ev, "item")) {
                    let st = s(&body, "status");
                    if !st.is_empty() { b.status = st.into(); b.status_by = author(ev); b.status_ts = ts; }
                }
            }
            "comment" => {
                if let Some(b) = self.briefs.get_mut(s(ev, "item")) {
                    let note = Note { text: text(&body, "text"), by: author(ev), ts, report: Value::Null };
                    if body.get("redirect").and_then(Value::as_bool) == Some(true) {
                        b.redirect = Some(note);
                    } else if let Some(r) = body.get("report").filter(|r| !r.is_null() && r.as_bool() != Some(false)) {
                        b.reports += 1;
                        b.last_report = Some(Note { report: if r.is_object() { r.clone() } else { Value::Null }, ..note });
                    }
                }
            }
            _ => {}
        }
    }

    pub fn get(&self, id: &str) -> Option<&Brief> { self.briefs.get(id) }

    /// Some(status) when the Brief is paused or stopped.
    pub fn halted(&self, id: &str) -> Option<&Brief> { self.briefs.get(id).filter(|b| HALTED.contains(&b.status.as_str())) }

    /// May a session working for `brief` make this write? Paused or stopped: only a report (comment) on the Brief.
    pub fn check_session_write(&self, brief: &str, kind: &str, item: Option<&str>) -> Result<(), String> {
        let Some(b) = self.halted(brief) else { return Ok(()) };
        if kind == "comment" && item == Some(brief) { return Ok(()); }
        Err(format!("{brief} was {} by {} at {}: stop here. Report what you did as a comment on {brief}, then wait{}.",
            b.status, b.status_by, b.status_ts, if b.status == "paused" { " until someone resumes it" } else { "; it won't resume" }))
    }

    /// What an agent working for `brief` must read before its next step: the state, and a redirect newer than `since`.
    pub fn notice(&self, brief: &str, since: &str) -> Value {
        let Some(b) = self.briefs.get(brief) else {
            return json!({"id": brief, "unknown": true, "note": format!("{brief} isn't a Brief on the hub's record; check the id or sync Studio")});
        };
        let mut v = json!({"id": b.id, "status": b.status, "by": b.status_by, "at": b.status_ts});
        if let Some(r) = b.redirect.as_ref().filter(|r| r.ts.as_str() > since) {
            v["redirect"] = json!({"text": r.text, "by": r.by, "at": r.ts});
            v["note"] = json!("redirected since you began: follow the redirect from your next step");
        }
        if HALTED.contains(&b.status.as_str()) { v["note"] = json!(format!("{}: stop and report", b.status)); }
        v
    }

    /// In flight: every Brief that hasn't finished, newest first, with the sessions working on it now.
    pub fn in_flight(&self, guard: &crate::guard::State, now: i64, visible: impl Fn(&Brief) -> bool) -> Value {
        let active = guard.active(now);
        let list: Vec<Value> = self.order.iter().rev().filter_map(|id| self.briefs.get(id))
            .filter(|b| !FINISHED.contains(&b.status.as_str()) && visible(b))
            .map(|b| {
                let claims: Vec<Value> = active.iter().filter(|c| c.brief.as_deref() == Some(b.id.as_str()) || c.items.contains(&b.id))
                    .map(|c| json!({"session": c.session, "by": c.by, "role": c.role, "since": c.since, "until": crate::mcp::iso(c.until_ms)})).collect();
                let mut v = serde_json::to_value(b).unwrap_or(Value::Null);
                v["claims"] = json!(claims);
                v
            }).collect();
        json!({"in_flight": list, "now": crate::mcp::iso(now)})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(ts: &str, kind: &str, item: Option<&str>, body: Value, by: &str) -> Value {
        json!({"ts": ts, "kind": kind, "item": item, "body": body, "author": {"id": format!("github:{by}")}})
    }

    #[test]
    fn a_brief_folds_its_meta_status_redirect_and_reports() {
        let evs = [
            ev("2026-11-27T10:00:00.000Z", "item.create", None, json!({"id": "BRIEF-1", "type": "brief", "title": "Fix the deploy check",
                "status": "open", "meta": {"goal": "Green deploy check", "done_means": "RUNS-hub-deploy shows pass", "limits": {"touch": ["scripts/"], "risk": "low"},
                "holder": "agent:sam", "budget": "2 h", "deadline": "2026-11-28"}}), "Morami49"),
            ev("2026-11-27T10:05:00.000Z", "comment", Some("BRIEF-1"), json!({"text": "ran the tests", "report": {"did": "ran tests", "why": "baseline"}}), "sam"),
            ev("2026-11-27T10:06:00.000Z", "comment", Some("BRIEF-1"), json!({"text": "only touch smoke_hub.py", "redirect": true}), "Morami49"),
            ev("2026-11-27T10:07:00.000Z", "status", Some("BRIEF-1"), json!({"status": "paused"}), "Morami49"),
            ev("2026-11-27T10:08:00.000Z", "item.edit", Some("BRIEF-1"), json!({"meta": {"budget": "3 h"}}), "Morami49"),
        ];
        let st = State::fold(evs.iter());
        let b = st.get("BRIEF-1").unwrap();
        assert_eq!((b.goal.as_str(), b.holder.as_str(), b.budget.as_str(), b.status.as_str()), ("Green deploy check", "agent:sam", "3 h", "paused"));
        assert_eq!(b.limits["risk"], "low");
        assert_eq!(b.reports, 1);
        assert_eq!(b.last_report.as_ref().unwrap().report["why"], "baseline");
        assert_eq!(b.redirect.as_ref().unwrap().text, "only touch smoke_hub.py");
        // paused: only a report on the Brief goes through
        assert!(st.check_session_write("BRIEF-1", "comment", Some("BRIEF-1")).is_ok());
        let e = st.check_session_write("BRIEF-1", "item.edit", Some("D-1")).unwrap_err();
        assert!(e.contains("paused by Morami49") && e.contains("until someone resumes"), "{e}");
        assert!(st.check_session_write("BRIEF-1", "status", Some("BRIEF-1")).is_err(), "an agent can't resume its own Brief");
        // the redirect is news only to sessions that began before it
        assert!(st.notice("BRIEF-1", "2026-11-27T10:00:30.000Z").get("redirect").is_some());
        assert!(st.notice("BRIEF-1", "2026-11-27T10:06:30.000Z").get("redirect").is_none());
        assert_eq!(st.notice("NOPE", "")["unknown"], true);
    }

    #[test]
    fn steering_is_a_status_or_a_redirect_on_a_brief() {
        let ty = |id: &str| (id == "BRIEF-1").then(|| TYPE.to_string());
        assert!(is_steer(&ev("t", "status", Some("BRIEF-1"), json!({"status": "paused"}), "x"), ty));
        assert!(is_steer(&ev("t", "comment", Some("BRIEF-1"), json!({"text": "go left", "redirect": true}), "x"), ty));
        assert!(!is_steer(&ev("t", "comment", Some("BRIEF-1"), json!({"text": "report", "report": {"did": "x"}}), "x"), ty));
        assert!(!is_steer(&ev("t", "status", Some("D-1"), json!({"status": "paused"}), "x"), ty));
    }

    #[test]
    fn stopped_and_done_briefs_leave_in_flight() {
        let mk = |id: &str| ev("2026-11-27T10:00:00.000Z", "item.create", None, json!({"id": id, "type": "brief", "title": id}), "Morami49");
        let evs = [mk("B-1"), mk("B-2"), mk("B-3"),
            ev("2026-11-27T10:01:00.000Z", "status", Some("B-2"), json!({"status": "stopped"}), "Morami49"),
            ev("2026-11-27T10:01:00.000Z", "status", Some("B-3"), json!({"status": "done"}), "Morami49")];
        let st = State::fold(evs.iter());
        let v = st.in_flight(&crate::guard::State::default(), 0, |_| true);
        let ids: Vec<&str> = v["in_flight"].as_array().unwrap().iter().map(|b| b["id"].as_str().unwrap()).collect();
        assert_eq!(ids, ["B-1"]);
        assert_eq!(st.check_session_write("B-2", "item.edit", Some("X")).unwrap_err().contains("won't resume"), true);
    }
}
