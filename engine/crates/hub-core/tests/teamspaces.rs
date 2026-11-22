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

fn items_seen(h: &Hub, login: &str) -> Vec<String> {
    let (docs, _, _, _) = h.studio_events_for(login, 0, usize::MAX);
    let mut v: Vec<String> = docs.iter().filter_map(|d| access::subject(d.get("event")?)).collect();
    v.sort(); v.dedup(); v
}

#[test]
fn a_guest_reads_only_the_shared_node_and_the_teamspace_itself() {
    let h = hub();
    assert_eq!(items_seen(&h, "nassar"), vec!["N-quote", "TS-pilot"]);
    assert!(items_seen(&h, "stranger").is_empty());
    assert!(items_seen(&h, "Morami49").contains(&"N-plan".to_string()), "the hub owner still sees the whole record");
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
