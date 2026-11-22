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
}
