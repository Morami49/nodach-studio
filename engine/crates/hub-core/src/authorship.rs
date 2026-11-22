//! Who an event may claim to be written by (F-profile-kinds-spoofable, 20 Nov 2026).
//!
//! Events that reach the record through Studio Web (`POST /studio/api/events`) or `import_studio_events` carry their
//! own `author`, and the importer key vouches for the copy. Several folds trust that author: access grants count only
//! when an owner wrote them, profiles and connections belong to their author, sign-in links point at their author.
//! So a member who could write an event authored as the owner could grant themselves spaces or claim an owner profile.
//! #50 closed this for `account.link` only. The rule here, checked on both write paths before anything is stored:
//!   1. profile and access kinds (`SIGNER_ONLY`) must be authored by the signed-in account itself, owner or not;
//!   2. a member (not an owner) may only write events authored by themselves, and only where `check_write` allows
//!      (inside spaces they were given). An owner may still import other people's ordinary events (D-129 sync).
//! Rollback: revert the PR; nothing is stored by this module.
use crate::access;
use crate::profiles;
use serde_json::Value;

/// Kinds whose meaning depends on who wrote them.
pub fn signer_only(kind: &str) -> bool {
    profiles::KINDS.contains(&kind) || access::KINDS.contains(&kind)
}

fn authored_by(ev: &Value, login: &str) -> bool {
    ev.pointer("/author/id").and_then(Value::as_str)
        .is_some_and(|a| a.eq_ignore_ascii_case(&format!("github:{}", login.trim())))
}

/// Why `login` may not put this event on the record, or None if it may.
pub fn refusal(acc: &access::State, login: &str, ev: &Value) -> Option<String> {
    let kind = ev.get("kind").and_then(Value::as_str).unwrap_or("");
    let own = authored_by(ev, login);
    if signer_only(kind) && !own {
        return Some(format!("{kind} must be written by the signed-in account itself"));
    }
    if acc.is_owner(login) {
        return None;
    }
    if !own {
        return Some("you can only write events authored by yourself".into());
    }
    acc.check_write(login, kind, access::subject(ev).as_deref()).err()
}

/// The first refusal in a batch, naming the doc, or None if every doc may be written.
pub fn batch_refusal(acc: &access::State, login: &str, docs: &[(String, Value)]) -> Option<String> {
    docs.iter().find_map(|(id, ev)| refusal(acc, login, ev).map(|r| format!("{id}: {r}")))
}

/// B-profile-windows (98b b): the header a Studio window sends with every write, naming the profile it is working as.
pub const PROFILE_HEADER: &str = "x-nodach-profile";

fn account_for(st: &profiles::State, login: &str) -> String {
    st.account_of(&format!("github:{}", login.trim()))
        .cloned()
        .unwrap_or_else(|| format!("acct:github:{}", login.trim().to_ascii_lowercase()))
}

/// Why `login` may not write as `profile`, or None if it is one of the signed-in account's active profiles.
/// A profile is not a sign-in: one account has several sign-ins and several profiles, and a window works as one of
/// them. Someone else's profile, a revoked one or one that doesn't exist is refused, owner or not.
/// The account's own Personal profile (`P-<login>`) always counts, even before any event has created the account.
pub fn profile_refusal(st: &profiles::State, login: &str, profile: &str) -> Option<String> {
    let profile = profile.trim();
    let acct = account_for(st, login);
    match st.profiles.get(profile) {
        None if profile.eq_ignore_ascii_case(&profiles::personal_id(login)) => None,
        None => Some(format!("profile {profile} doesn't exist; open Studio in one of your own profiles. Nothing was saved.")),
        Some(p) if p.account != acct => Some(format!(
            "profile {profile} isn't one of yours (signed in as {}); this window can only write as your own profiles. Nothing was saved.",
            login.trim()
        )),
        Some(p) if p.revoked.is_some() => Some(format!("profile {profile} was revoked; switch this window to another profile. Nothing was saved.")),
        Some(_) => None,
    }
}

/// The profile a write carries: the window's header and the event's `author.profile`, which must agree when both are
/// given. Ok(None) when neither is (older pages and other clients: the write counts as the author's Personal profile).
pub fn write_profile(header: Option<&str>, ev: &Value) -> Result<Option<String>, String> {
    let h = header.map(str::trim).filter(|x| !x.is_empty());
    let e = ev.pointer("/author/profile").and_then(Value::as_str).map(str::trim).filter(|x| !x.is_empty());
    match (h, e) {
        (Some(a), Some(b)) if a != b => Err(format!("this window works as profile {a} but the event says {b}. Nothing was saved.")),
        (a, b) => Ok(a.or(b).map(str::to_string)),
    }
}

/// The profile check for one Studio write by `login`: a profile named by the window (header) or the event must be
/// one of `login`'s active profiles; an event's `author.profile` must also belong to the event's own author (so an
/// owner importing someone else's event can't re-attribute it, and nobody can borrow another person's profile).
pub fn write_profile_refusal(st: &profiles::State, login: &str, header: Option<&str>, ev: &Value) -> Option<String> {
    let p = match write_profile(header, ev) {
        Err(e) => return Some(e),
        Ok(None) => return None,
        Ok(Some(p)) => p,
    };
    let author = ev.pointer("/author/id").and_then(Value::as_str).and_then(|a| a.strip_prefix("github:")).unwrap_or(login);
    if header.is_some_and(|h| !h.trim().is_empty()) {
        if let Some(r) = profile_refusal(st, login, &p) { return Some(r); }
    }
    profile_refusal(st, author, &p)
}

/// The first profile refusal in a batch (connector import), naming the doc.
pub fn batch_profile_refusal(st: &profiles::State, login: &str, docs: &[(String, Value)]) -> Option<String> {
    docs.iter().find_map(|(id, ev)| write_profile_refusal(st, login, None, ev).map(|r| format!("{id}: {r}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(author: &str, kind: &str, item: Option<&str>, body: Value) -> Value {
        json!({"ts": "2026-11-20T12:00:00.000Z", "kind": kind, "item": item, "body": body,
               "author": {"id": format!("github:{author}"), "label": author, "role": "member"}})
    }

    /// owner Morami49; member "friend" was given the INSYDR- space
    fn state() -> access::State {
        access::State::fold(&["Morami49".into()], &[
            ev("Morami49", "access.grant", None, json!({"login": "friend", "prefixes": ["INSYDR-"]})),
        ])
    }

    #[test]
    fn a_member_cannot_claim_an_owner_profile() {
        let st = state();
        let forged = ev("Morami49", "profile.create", None, json!({"id": "P-x", "kind": "developer", "label": "x", "role": "owner"}));
        assert!(refusal(&st, "friend", &forged).unwrap().contains("profile.create"));
        // every profile and access kind, not only account.link
        for k in profiles::KINDS.iter().chain(access::KINDS.iter()) {
            assert!(refusal(&st, "friend", &ev("Morami49", k, None, json!({}))).is_some(), "{k}");
        }
    }

    #[test]
    fn a_member_cannot_grant_themselves_a_space_by_forging_the_owner() {
        let st = state();
        let grant = ev("Morami49", "access.grant", None, json!({"login": "friend", "prefixes": [""]}));
        assert!(refusal(&st, "friend", &grant).is_some());
        // and writing it as themselves is refused by check_write (grants are owner-only)
        assert!(refusal(&st, "friend", &ev("friend", "access.grant", None, json!({"login": "friend"}))).is_some());
    }

    #[test]
    fn a_member_writes_only_as_themselves_and_only_inside_their_spaces() {
        let st = state();
        assert_eq!(refusal(&st, "friend", &ev("friend", "comment", Some("INSYDR-1"), json!({"text": "hi"}))), None);
        assert_eq!(refusal(&st, "FRIEND", &ev("friend", "comment", Some("INSYDR-1"), json!({"text": "hi"}))), None);
        assert!(refusal(&st, "friend", &ev("Morami49", "comment", Some("INSYDR-1"), json!({"text": "hi"}))).is_some());
        assert!(refusal(&st, "friend", &ev("friend", "comment", Some("Q-RB1"), json!({"text": "hi"}))).is_some());
        // an event with no author is not the member's own
        let mut bare = ev("friend", "comment", Some("INSYDR-1"), json!({}));
        bare.as_object_mut().unwrap().remove("author");
        assert!(refusal(&st, "friend", &bare).is_some());
    }

    #[test]
    fn an_own_profile_event_is_fine_for_a_member() {
        let st = state();
        // profile kinds pass the author rule; check_write still decides where a member may write
        let mine = ev("friend", "profile.create", Some("INSYDR-P1"), json!({"id": "INSYDR-P1", "kind": "developer", "label": "me"}));
        assert_eq!(refusal(&st, "friend", &mine), None);
    }

    #[test]
    fn the_owner_still_imports_other_peoples_ordinary_events_but_not_their_profile_events() {
        let st = state();
        assert_eq!(refusal(&st, "Morami49", &ev("friend", "comment", Some("INSYDR-1"), json!({"text": "hi"}))), None);
        assert_eq!(refusal(&st, "morami49", &ev("Morami49", "access.grant", None, json!({"login": "x"}))), None);
        assert!(refusal(&st, "Morami49", &ev("friend", "profile.create", None, json!({"id": "P-y"}))).is_some());
        assert!(refusal(&st, "Morami49", &ev("friend", "account.link", None, json!({"identity": "email:sha256:00"}))).is_some());
    }

    #[test]
    fn a_batch_names_the_first_bad_doc() {
        let st = state();
        let docs = vec![("d1".to_string(), ev("friend", "comment", Some("INSYDR-1"), json!({}))),
                        ("d2".to_string(), ev("Morami49", "profile.create", None, json!({})))];
        assert!(batch_refusal(&st, "friend", &docs).unwrap().starts_with("d2: "));
        assert_eq!(batch_refusal(&st, "friend", &docs[..1]), None);
    }

    // ---------------------------------------------------------------- B-profile-windows: profile vs account
    fn prof_state() -> profiles::State {
        let evs = [
            ev("Morami49", "profile.create", None, json!({"id": "P-work", "kind": "work", "label": "Work"})),
            ev("Morami49", "profile.create", None, json!({"id": "P-old", "kind": "venture", "label": "Old venture"})),
            ev("Morami49", "profile.revoke", None, json!({"profile": "P-old"})),
            ev("friend", "profile.create", None, json!({"id": "P-friend-work", "kind": "work", "label": "Friend work"})),
        ];
        let ids: Vec<String> = (0..evs.len()).map(|i| format!("d{i}")).collect();
        profiles::State::fold(ids.iter().map(String::as_str).zip(evs.iter()))
    }

    #[test]
    fn own_profile_is_allowed() {
        let st = prof_state();
        assert_eq!(profile_refusal(&st, "Morami49", "P-work"), None);
        assert_eq!(profile_refusal(&st, "morami49", "P-morami49"), None);
        // the implicit Personal profile counts before the account has written anything
        assert_eq!(profile_refusal(&st, "newcomer", "P-newcomer"), None);
        let e = ev("Morami49", "comment", Some("Q-1"), json!({}));
        assert_eq!(write_profile_refusal(&st, "Morami49", Some("P-work"), &e), None);
    }

    #[test]
    fn someone_elses_profile_is_refused() {
        let st = prof_state();
        let r = profile_refusal(&st, "Morami49", "P-friend-work").unwrap();
        assert!(r.contains("isn't one of yours") && r.contains("Nothing was saved"), "{r}");
        assert!(profile_refusal(&st, "friend", "P-work").is_some());
        assert!(profile_refusal(&st, "friend", "P-morami49").is_some());
        assert!(profile_refusal(&st, "friend", "P-nope").unwrap().contains("doesn't exist"));
        // the owner is not exempt: a profile is the person's, not a role
        let e = ev("Morami49", "comment", Some("Q-1"), json!({}));
        assert!(write_profile_refusal(&st, "Morami49", Some("P-friend-work"), &e).is_some());
    }

    #[test]
    fn revoked_profile_is_refused() {
        let st = prof_state();
        assert!(profile_refusal(&st, "Morami49", "P-old").unwrap().contains("revoked"));
    }

    #[test]
    fn header_and_event_profile_must_agree_and_belong_to_the_author() {
        let st = prof_state();
        let mut e = ev("Morami49", "comment", Some("Q-1"), json!({}));
        e["author"]["profile"] = json!("P-work");
        assert_eq!(write_profile_refusal(&st, "Morami49", Some("P-work"), &e), None);
        assert_eq!(write_profile_refusal(&st, "Morami49", None, &e), None);
        assert!(write_profile_refusal(&st, "Morami49", Some("P-morami49"), &e).unwrap().contains("event says"));
        // no profile at all (older pages, the desktop client): counts as Personal, not refused
        assert_eq!(write_profile_refusal(&st, "friend", None, &ev("friend", "comment", None, json!({}))), None);
        // an owner importing a member's event can't re-attribute it to the owner's profile
        let mut theirs = ev("friend", "comment", Some("INSYDR-1"), json!({}));
        theirs["author"]["profile"] = json!("P-work");
        let docs = vec![("d9".to_string(), theirs)];
        assert!(batch_profile_refusal(&st, "Morami49", &docs).unwrap().starts_with("d9: "));
    }
}
