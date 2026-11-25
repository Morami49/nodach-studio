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
    // he is told it exists (117b a: by id only, no name), so he can ask for support access
    let v = h.teamspaces_at(at(NOW)).view_for("Morami49");
    assert_eq!(v["others"], json!([{"id": "TS-n", "label": null, "support_until": null}]));
    assert!(!v.to_string().contains("Insydr internal"), "the name stays out of everything the hub owner reads");
    let (docs, _, _, _) = h.studio_events_for("Morami49", 0, usize::MAX, at(NOW));
    assert!(!serde_json::to_string(&docs).unwrap().contains("Insydr internal"), "not in the record he reads either");
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
    // the directory says until when, and (117b a) names it now that support access is open
    let v = h.teamspaces_at(opened + 60_000).view_for("Morami49");
    assert_eq!(v["others"][0]["support_until"], json!("2026-11-23T12:00:30.000Z"));
    assert_eq!(v["others"][0]["label"], json!("Insydr internal"));
    let (docs, _, _, _) = h.studio_events_for("Morami49", 0, usize::MAX, opened + 60_000);
    assert!(serde_json::to_string(&docs).unwrap().contains("Insydr internal"), "and the record he reads names it too");
    let v = h.teamspaces_at(opened + teamspaces::SUPPORT_MS).view_for("Morami49");
    assert_eq!(v["others"][0]["label"], json!(null), "ids only again after 24 hours");
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

// ---- F-ci-token-refused (23 Nov): production writers after #74/#79, and the CI token since A1 d (18 Nov) ----

fn tool(h: &mut Hub, login: &str, name: &str, args: Value) -> Value {
    let key = KeyPair::from_seed([7; 32]);
    mcp::handle(h, login, &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}), at(NOW))
        .unwrap()["result"].clone()
}

/// Exactly what scripts/run_record.py sends for one hub-staging / hub-deploy run.
fn run_record(h: &mut Hub, executor: &str) -> Vec<Value> {
    let item = format!("RUNS-{executor}");
    vec![
        tool(h, access::SERVICE_LOGIN, "write_event", json!({"kind": "item.create", "item": null, "idempotency_key": format!("runs-item/{executor}"),
            "body": {"id": item, "type": "asset", "title": "Runs", "status": "built", "refs": ["P-13"]}})),
        tool(h, access::SERVICE_LOGIN, "write_event", json!({"kind": "comment", "item": item, "idempotency_key": format!("run/{executor}/1/1"),
            "body": {"text": "Staging deploy", "outcome": "failure"}})),
    ]
}

#[test]
fn ci_token_records_runs_on_the_production_hub_again() {
    // The 23 Nov hub-staging run 45222430169 got {"error": "RUNS-hub-staging isn't in a space you've been given"}
    let mut h = hub();
    for executor in ["hub-staging", "hub-deploy", "branch-cleanup"] {
        for r in run_record(&mut h, executor) {
            assert_eq!(r["isError"], false, "{executor}: {r}");
        }
    }
    let (docs, _, _, _) = h.studio_events(0, usize::MAX);
    assert!(docs.iter().any(|d| d["event"]["item"] == "RUNS-hub-staging" && d["event"]["kind"] == "comment"));
}

#[test]
fn ci_token_reads_the_deploy_policy_again() {
    // policy.py read "Studio: unknown" on every run since 18 Nov: SET-policy-hub-deploy was invisible to the token
    let mut h = hub();
    assert_eq!(tool(&mut h, "Morami49", "write_event", json!({"kind": "item.create", "item": null,
        "body": {"id": "SET-policy-hub-deploy", "type": "setting", "title": "hub-deploy policy", "value": "ask"}}))["isError"], false);
    let seen = items_seen(&h, access::SERVICE_LOGIN);
    assert!(seen.contains(&"SET-policy-hub-deploy".to_string()), "{seen:?}");
    // ...but never inside a teamspace it isn't in (it can't open support access either)
    assert!(!seen.contains(&"N-plan".to_string()) && !seen.contains(&"MR-pilot".to_string()), "{seen:?}");
}

#[test]
fn ci_token_is_still_bound_by_teamspaces_and_cannot_grant() {
    let mut h = hub();
    let r = tool(&mut h, access::SERVICE_LOGIN, "write_event", json!({"kind": "comment", "item": "N-plan", "body": {"text": "x"}}));
    assert_eq!(r["structuredContent"]["refused"], true, "{r}");
    for (kind, item, body) in [
        ("access.grant", Value::Null, json!({"login": "someone", "prefixes": ["D-"]})),
        ("member.role", json!("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-nassar", "role": "owner"})),
        ("node.share", json!("N-plan"), json!({"node": "N-plan", "profile": "P-nassar", "level": "editor"})),
    ] {
        let r = tool(&mut h, access::SERVICE_LOGIN, "write_event", json!({"kind": kind, "item": item, "body": body}));
        assert_eq!(r["structuredContent"]["refused"], true, "{kind}: {r}");
    }
}

#[test]
fn other_production_writers_are_unchanged_outside_teamspaces() {
    let mut h = hub();
    // the Claude connector (signed-in owner) writes outside teamspaces
    assert_eq!(tool(&mut h, "Morami49", "write_event", json!({"kind": "comment", "item": "D-nodach-org-model", "body": {"text": "ok"}}))["isError"], false);
    // the importer (Studio Web sync, /import and import_studio_events): events outside teamspaces, any author
    let acc = h.access();
    let outside = vec![
        ("w1".to_string(), ev("Morami49", "item.create", None, json!({"id": "Q-1", "type": "question", "title": "q"}), 20)),
        ("w2".to_string(), ev("Morami49", "comment", Some("Q-1"), json!({"text": "from Studio Web"}), 21)),
    ];
    assert_eq!(authorship::batch_refusal(&acc, "Morami49", &outside), None);
    assert_eq!(authorship::batch_teamspace_refusal(&acc, &outside), None);
    let made = h.import(&KeyPair::from_seed([9; 32]), outside, at(NOW)).unwrap();
    assert!(!made.is_empty());
    // Desktop and Live (POST /studio/api/events as the signed-in owner) outside teamspaces
    let acc = h.access();
    for e in [ev("Morami49", "comment", Some("Q-1"), json!({"text": "desktop"}), 22),
              ev("Morami49", "item.create", None, json!({"id": "LIVE-s1", "type": "session", "title": "live"}), 23),
              ev("Morami49", "canvas.layout", None, json!({"board": "main", "moves": []}), 24)] {
        assert_eq!(authorship::refusal(&acc, "Morami49", &e), None, "{e}");
    }
    // a member outside their spaces is still refused exactly as before
    assert!(authorship::refusal(&acc, "nassar", &ev("nassar", "comment", Some("Q-1"), json!({"text": "x"}), 25)).is_some());
}

#[test]
fn the_hub_owner_still_sees_shares_of_nodes_outside_every_teamspace() {
    // "Everything outside teamspaces stays his" (114b a): a member's share of a node in no teamspace was hidden
    let mut h = hub();
    let docs = vec![
        ("s1".to_string(), ev("Morami49", "access.grant", None, json!({"login": "nassar", "prefixes": ["INSYDR-"]}), 30)),
        ("s2".to_string(), ev("nassar", "item.create", None, json!({"id": "INSYDR-brief", "type": "idea", "title": "brief"}), 31)),
        ("s3".to_string(), ev("nassar", "node.share", Some("INSYDR-brief"), json!({"node": "INSYDR-brief", "profile": "P-someone", "level": "viewer"}), 32)),
    ];
    h.import(&KeyPair::from_seed([9; 32]), docs, at(NOW)).unwrap();
    let (docs, _, _, _) = h.studio_events_for("Morami49", 0, usize::MAX, at(NOW));
    assert!(docs.iter().any(|d| d["event"]["kind"] == "node.share" && d["event"]["item"] == "INSYDR-brief"));
}

/// BQ-slice-home-insydr U1 (MAP-stakeholders S1, S2): the map's "systems" are exactly what the hub lets each person
/// read. S1, Nowed (hub owner): his home system (everything outside teamspaces) and the Insydr system he is in.
/// S2, an Insydr member: the Insydr system only. No home node, no other teamspace, not even its name.
#[test]
fn slice_u1_s1_owner_sees_both_systems_s2_insydr_member_sees_only_insydr() {
    let mut h = hub();
    let docs = vec![
        ev("Morami49", "item.create", None, json!({"id": "D-home", "type": "decision", "title": "Home decision"}), 30),
        ev("Morami49", "comment", Some("D-home"), json!({"text": "home only"}), 31),
        ev("Morami49", "member.invite", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-insydr-tester", "role": "member"}), 32),
        ev("insydr-tester", "member.join", Some("TS-pilot"), json!({"teamspace": "TS-pilot"}), 33),
    ];
    let docs: Vec<(String, Value)> = docs.into_iter().enumerate().map(|(i, e)| (format!("s{i}"), e)).collect();
    h.import(&KeyPair::from_seed([9; 32]), docs, 3).unwrap();
    with_nassars_space(&mut h, vec![]);
    // S1: home and Insydr (TS-pilot) both; Nassar's own teamspace stays out (114b a)
    let nowed = items_seen(&h, "Morami49");
    for id in ["D-home", "TS-pilot", "MR-pilot", "N-plan", "N-quote"] { assert!(nowed.contains(&id.to_string()), "S1 missing {id}: {nowed:?}"); }
    assert!(!nowed.contains(&"TS-n".to_string()));
    // S2: a member of Insydr reads the whole Insydr system (its nodes and money rules) and nothing of home
    let member = items_seen(&h, "insydr-tester");
    assert_eq!(member, vec!["MR-pilot", "N-plan", "N-quote", "TS-pilot"], "S2: only the Insydr system");
    let v = h.teamspaces_at(at(NOW)).view_for("insydr-tester");
    assert!(v["teamspaces"].as_array().unwrap().iter().any(|t| t["id"] == "TS-pilot"));
    assert_eq!(v["others"], json!([]), "S2: no directory of other systems");
    let (docs, _, _, _) = h.studio_events_for("insydr-tester", 0, usize::MAX, at(NOW));
    let read = serde_json::to_string(&docs).unwrap();
    assert!(!read.contains("Home decision") && !read.contains("Insydr internal"), "S2: no home node, no other teamspace's name");
}
