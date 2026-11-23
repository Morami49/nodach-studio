//! BQ-1 step 2: teamspaces through the whole hub: the record is imported, then reads are filtered per login and
//! writes are refused by the same checks the Worker runs (hub_core::authorship) before anything is stored.
use foundation_core::event::KeyPair;
use hub_core::*;
use serde_json::{json, Value};

fn ev(author: &str, kind: &str, item: Option<&str>, body: Value, n: u32) -> Value {
    json!({"ts": format!("2026-11-22T12:00:{:02}.000Z", n), "kind": kind, "item": item, "body": body,
           "author": {"id": format!("github:{author}"), "label": author, "role": "member"}})
}

fn hub() -> Hub {
    let mut h = Hub::new();
    h.set_owners(vec!["Morami49".into()]);
    let docs = vec![
        ev("Morami49", "teamspace.create", Some("TS-pilot"), json!({"id": "TS-pilot", "label": "Insydr pilot"}), 1),
        ev("Morami49", "item.create", None, json!({"id": "MR-pilot", "type": "setting", "title": "Money rules", "teamspace": "TS-pilot", "meta": {"money_rules": true}}), 2),
        ev("Morami49", "member.invite", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-nassar", "role": "guest"}), 3),
        ev("nassar", "member.join", Some("TS-pilot"), json!({"teamspace": "TS-pilot"}), 4),
        ev("Morami49", "item.create", None, json!({"id": "N-plan", "type": "idea", "title": "Plan", "teamspace": "TS-pilot"}), 5),
        ev("Morami49", "item.create", None, json!({"id": "N-quote", "type": "idea", "title": "Quote", "teamspace": "TS-pilot"}), 6),
        ev("Morami49", "node.share", Some("N-quote"), json!({"node": "N-quote", "profile": "P-nassar", "level": "commenter"}), 7),
        ev("Morami49", "comment", Some("N-plan"), json!({"text": "internal"}), 8),
    ];
    let docs: Vec<(String, Value)> = docs.into_iter().enumerate().map(|(i, e)| (format!("d{i}"), e)).collect();
    h.import(&KeyPair::from_seed([9; 32]), docs, 1).unwrap();
    h
}

/// The clock reads are judged at in these tests: half an hour after the record above.
fn at(ts: &str) -> i64 { teamspaces::iso_ms(ts).unwrap() }
const NOW: &str = "2026-11-22T12:30:00.000Z";

fn items_seen(h: &Hub, login: &str) -> Vec<String> { items_seen_at(h, login, at(NOW)) }

fn items_seen_at(h: &Hub, login: &str, now: i64) -> Vec<String> {
    let (docs, _, _, _) = h.studio_events_for(login, 0, usize::MAX, now);
    let mut v: Vec<String> = docs.iter().filter_map(|d| access::subject(d.get("event")?)).collect();
    v.sort(); v.dedup(); v
}

#[test]
fn a_guest_reads_only_the_shared_node_and_the_teamspace_itself() {
    let h = hub();
    assert_eq!(items_seen(&h, "nassar"), vec!["N-quote", "TS-pilot"]);
    assert!(items_seen(&h, "stranger").is_empty());
    assert!(items_seen(&h, "Morami49").contains(&"N-plan".to_string()), "the hub owner reads the teamspaces he is in");
}

#[test]
fn refusals_name_the_rule_and_bind_the_hub_owner_too() {
    let h = hub();
    let acc = h.access();
    let r = authorship::refusal(&acc, "nassar", &ev("nassar", "comment", Some("N-plan"), json!({"text": "x"}), 9)).unwrap();
    assert!(r.contains("isn't shared with you"), "{r}");
    assert_eq!(authorship::refusal(&acc, "nassar", &ev("nassar", "comment", Some("N-quote"), json!({"text": "ok"}), 9)), None);
    let r = authorship::refusal(&acc, "nassar", &ev("nassar", "item.create", None, json!({"id": "N-x", "teamspace": "TS-pilot"}), 9)).unwrap();
    assert!(r.contains("member or editor rights"), "{r}");
    let r = authorship::refusal(&acc, "nassar", &ev("nassar", "member.role", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-nassar", "role": "owner"}), 9)).unwrap();
    assert!(r.contains("only an owner of TS-pilot can change roles"), "{r}");
    // the hub owner importing a guest's forbidden write is refused too (judged on the event's author)
    assert!(authorship::teamspace_refusal(&acc, &ev("nassar", "item.edit", Some("N-plan"), json!({"title": "x"}), 9)).is_some());
    // and the owner of the teamspace may change roles
    assert_eq!(authorship::refusal(&acc, "Morami49", &ev("Morami49", "member.role", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-nassar", "role": "member"}), 9)), None);
    // a teamspace write needs no access.grant: a member who isn't given any space can still create their own teamspace
    assert_eq!(authorship::refusal(&acc, "nassar", &ev("nassar", "teamspace.create", Some("TS-n"), json!({"id": "TS-n", "label": "Nassar's"}), 9)), None);
}

#[test]
fn the_connector_refuses_the_same_writes() {
    let mut h = hub();
    let key = KeyPair::from_seed([3; 32]);
    let call = |kind: &str, item: Value, body: Value| json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "write_event", "arguments": {"kind": kind, "item": item, "body": body}}});
    let r = mcp::handle(&mut h, "nassar", &key, &call("item.edit", json!("N-plan"), json!({"title": "x"})), 10).unwrap();
    assert_eq!(r.pointer("/result/isError"), Some(&json!(true)));
    let r = mcp::handle(&mut h, "Morami49", &key, &call("member.role", json!("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-morami49", "role": "guest"})), 10).unwrap();
    assert_eq!(r.pointer("/result/structuredContent/teamspace_refused"), Some(&json!(true)), "{r}");
    let r = mcp::handle(&mut h, "nassar", &key, &call("comment", json!("N-quote"), json!({"text": "fine"})), 10).unwrap();
    assert_eq!(r.pointer("/result/isError"), Some(&json!(false)), "{r}");
}

/// Nassar's own teamspace, which the hub owner (Morami49) is not in.
fn with_nassars_space(h: &mut Hub, extra: Vec<Value>) {
    let mut docs = vec![
        ev("nassar", "teamspace.create", Some("TS-n"), json!({"id": "TS-n", "label": "Insydr internal"}), 20),
        ev("nassar", "item.create", None, json!({"id": "N-margin", "type": "idea", "title": "Margins", "teamspace": "TS-n"}), 21),
        ev("nassar", "comment", Some("N-margin"), json!({"text": "private to Insydr"}), 22),
    ];
    docs.extend(extra);
    let docs: Vec<(String, Value)> = docs.into_iter().enumerate().map(|(i, e)| (format!("n{i}"), e)).collect();
    h.import(&KeyPair::from_seed([9; 32]), docs, 2).unwrap();
}

#[test]
fn decision_114b_the_hub_owner_reads_only_teamspaces_he_is_in() {
    let mut h = hub();
    with_nassars_space(&mut h, vec![]);
    let seen = items_seen(&h, "Morami49");
    assert!(!seen.contains(&"N-margin".to_string()) && !seen.contains(&"TS-n".to_string()), "{seen:?}");
    assert!(seen.contains(&"N-plan".to_string()), "his own teamspace stays his");
    assert!(items_seen(&h, "nassar").contains(&"N-margin".to_string()));
    // he is told it exists (id and name only), so he can ask for support access
    let v = h.teamspaces_at(at(NOW)).view_for("Morami49");
    assert_eq!(v["others"], json!([{"id": "TS-n", "label": "Insydr internal", "support_until": null}]));
    assert!(v["teamspaces"].as_array().unwrap().iter().all(|t| t["id"] != "TS-n"));
    // nobody else gets a directory
    assert_eq!(h.teamspaces_at(at(NOW)).view_for("nassar")["others"], json!([]));
}

#[test]
fn decision_114b_support_access_is_logged_shown_to_owners_and_ends_after_24_hours() {
    let mut h = hub();
    let support = ev("Morami49", "support.access", Some("TS-n"), json!({"teamspace": "TS-n", "reason": "Nassar asked for help with an import"}), 30);
    // only the hub owner may ask, and he must say why
    with_nassars_space(&mut h, vec![]);
    let acc2 = h.access();
    assert!(authorship::refusal(&acc2, "nassar", &ev("nassar", "support.access", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "reason": "curious"}), 30))
        .unwrap().contains("only the hub owner"));
    assert!(authorship::refusal(&acc2, "Morami49", &ev("Morami49", "support.access", Some("TS-n"), json!({"teamspace": "TS-n", "reason": ""}), 30))
        .unwrap().contains("say why"));
    assert!(authorship::refusal(&acc2, "Morami49", &support).is_none());
    let mut h = hub();
    with_nassars_space(&mut h, vec![support]);
    // open for 24 hours from the event's time, then closed by itself
    let opened = at("2026-11-22T12:00:30.000Z");
    assert!(items_seen_at(&h, "Morami49", opened + 60_000).contains(&"N-margin".to_string()));
    assert!(items_seen_at(&h, "Morami49", opened + teamspaces::SUPPORT_MS - 1).contains(&"N-margin".to_string()));
    assert!(!items_seen_at(&h, "Morami49", opened + teamspaces::SUPPORT_MS).contains(&"N-margin".to_string()), "closed after 24 h");
    assert!(!items_seen_at(&h, "Morami49", opened - 1).contains(&"N-margin".to_string()), "not before it was asked for");
    // the directory says until when
    let v = h.teamspaces_at(opened + 60_000).view_for("Morami49");
    assert_eq!(v["others"][0]["support_until"], json!("2026-11-23T12:00:30.000Z"));
    // the teamspace's owner sees the event in the record and in the support access log
    let (docs, _, _, _) = h.studio_events_for("nassar", 0, usize::MAX, at(NOW));
    assert!(docs.iter().any(|d| d["event"]["kind"] == "support.access"), "the teamspace's owner sees it");
    let mine = h.teamspaces_at(at(NOW)).view_for("nassar");
    let ts = mine["teamspaces"].as_array().unwrap().iter().find(|t| t["id"] == "TS-n").unwrap().clone();
    assert_eq!(ts["support_log"][0]["by"], json!("morami49"));
    assert_eq!(ts["support_log"][0]["reason"], json!("Nassar asked for help with an import"));
    // a stranger doesn't
    let (docs, _, _, _) = h.studio_events_for("stranger", 0, usize::MAX, at(NOW));
    assert!(docs.iter().all(|d| d["event"]["kind"] != "support.access"));
    // support access reads; it gives no right to write (114a)
    let acc = h.access_at(Some(opened + 60_000));
    assert!(authorship::refusal(&acc, "Morami49", &ev("Morami49", "item.edit", Some("N-margin"), json!({"title": "x"}), 40)).is_some());
    assert!(authorship::refusal(&acc, "Morami49", &ev("Morami49", "item.create", None, json!({"id": "N-x", "teamspace": "TS-n"}), 40)).unwrap().contains("aren't a member"));
}

#[test]
fn decision_114a_teamspace_rules_bind_the_hub_owner() {
    let mut h = hub();
    with_nassars_space(&mut h, vec![]);
    let acc = h.access();
    assert!(acc.is_owner("Morami49"));
    // in someone else's teamspace the hub owner has no role: no roles, invites, joins, nodes or shares
    let tries = [
        ev("Morami49", "member.role", Some("TS-n"), json!({"teamspace": "TS-n", "profile": "P-nassar", "role": "guest"}), 40),
        ev("Morami49", "member.invite", Some("TS-n"), json!({"teamspace": "TS-n", "profile": "P-morami49", "role": "owner"}), 40),
        ev("Morami49", "member.join", Some("TS-n"), json!({"teamspace": "TS-n"}), 40),
        ev("Morami49", "item.create", None, json!({"id": "N-y", "teamspace": "TS-n"}), 40),
        ev("Morami49", "comment", Some("N-margin"), json!({"text": "x"}), 40),
        ev("Morami49", "node.share", Some("N-margin"), json!({"node": "N-margin", "profile": "P-morami49", "level": "editor"}), 40),
    ];
    for t in &tries {
        assert!(authorship::refusal(&acc, "Morami49", t).is_some(), "the hub owner was let through: {t}");
    }
    // in his own teamspace the same rules apply to him as to anyone: he can't leave it ownerless
    let r = authorship::refusal(&acc, "Morami49", &ev("Morami49", "member.remove", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-morami49"}), 40)).unwrap();
    assert!(r.contains("at least one owner"), "{r}");
}
