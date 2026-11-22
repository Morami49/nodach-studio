//! BQ-1 step 3: the usage meter through the whole hub: runner facts on the record are counted per scope, notices the
//! hub raises are readable by the account they are for (and nobody else who isn't an owner), and the limits node is
//! read from the record.
use foundation_core::event::KeyPair;
use hub_core::usage::{self, Meter};
use hub_core::*;
use serde_json::{json, Value};
use std::collections::BTreeMap;

fn ev(author: &str, kind: &str, item: Option<&str>, body: Value, n: u32) -> Value {
    json!({"ts": format!("2026-11-22T12:00:{:02}.000Z", n), "kind": kind, "item": item, "body": body,
           "author": {"id": format!("github:{author}"), "label": author, "role": if author == "Morami49" { "owner" } else { "member" }}})
}

fn runner(kind: &str, body: Value, n: u32) -> Value {
    json!({"ts": format!("2026-11-22T12:01:{:02}.000Z", n), "kind": kind, "item": null, "body": body,
           "author": {"id": usage::RUNNER_ID, "label": "Flow runner", "role": "agent"}, "via": "flow-runner"})
}

#[test]
fn runs_are_counted_and_notices_reach_the_account_they_are_for() {
    let mut h = Hub::new();
    h.set_owners(vec!["Morami49".into()]);
    let docs = vec![
        ev("Morami49", "item.create", None, json!({"id": usage::LIMITS_ID, "type": "setting", "title": "Free limits", "value": "{\"ai_calls\":10,\"runs\":5,\"placeholder\":true}"}), 1),
        ev("nassar", "item.create", None, json!({"id": "B-nassar", "type": "idea", "title": "his board"}), 2),
    ];
    let docs: Vec<(String, Value)> = docs.into_iter().enumerate().map(|(i, e)| (format!("d{i}"), e)).collect();
    h.import(&KeyPair::from_seed([9; 32]), docs, 1).unwrap();
    let k = KeyPair::from_seed([7; 32]);
    for i in 0..4 {
        h.write(&k, "flow runner", runner("flow.run", json!({"board": "B-nassar", "ok": true, "usage": usage::usage_body("P-nassar", 1, 2)}), i), 2).unwrap();
    }
    let st = h.usage("2026-11", &BTreeMap::new());
    assert_eq!((st.used("P-nassar", Meter::Runs), st.used("P-nassar", Meter::AiCalls)), (4, 8));
    assert_eq!((st.limits.runs, st.limits.version), (5, 1));
    assert_eq!(st.scope_for_run("B-nassar", None, "schedule"), "P-nassar");
    assert!(st.allow("P-nassar", Meter::Runs, 1).is_ok());
    let due = st.due_notices();
    assert_eq!(due.len(), 2, "runs at 80%, AI calls at 80%: {due:?}");
    for n in due { h.write(&k, "usage meter", runner("item.create", n, 9), 3).unwrap(); }
    assert!(h.usage("2026-11", &BTreeMap::new()).due_notices().is_empty(), "raised once");
    let seen = |login: &str| -> Vec<String> {
        let (docs, _, _, _) = h.studio_events_for(login, 0, usize::MAX);
        docs.iter().filter_map(|d| d.pointer("/event/body/id")?.as_str().map(str::to_string)).filter(|i| i.starts_with(usage::NOTICE_PREFIX)).collect()
    };
    assert_eq!(seen("nassar"), vec!["USE-2026-11-ai_calls-80-P-nassar", "USE-2026-11-runs-80-P-nassar"]);
    assert!(seen("stranger").is_empty());
    assert_eq!(seen("Morami49").len(), 2, "hub owners see everything");
    // the fifth run is allowed, the sixth refused
    h.write(&k, "flow runner", runner("flow.run", json!({"board": "B-nassar", "usage": usage::usage_body("P-nassar", 1, 0)}), 20), 4).unwrap();
    assert_eq!(h.usage("2026-11", &BTreeMap::new()).allow("P-nassar", Meter::Runs, 1), Err(usage::REFUSAL.to_string()));
}
