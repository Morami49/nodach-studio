//! P1 c: the restore drill passes on a whole export and catches a lost, tampered or partial one.
use foundation_core::event::{KeyPair, Realm};
use foundation_core::store::Store;
use hub_core::drill::{run, set_sha256, Source};
use hub_core::Hub;
use serde_json::json;

fn key(n: u8) -> KeyPair { KeyPair::from_seed([n; 32]) }

fn hub_with_record() -> Hub {
    let k = key(1);
    let mut s = Store::new();
    let mut v = vec![s.fact(&k, "identity.genesis", json!({"label": "pc"}), Realm::Real, None, 1, &[]).unwrap()];
    for i in 0..5 { v.push(s.fact(&k, "capture", json!({"note": i}), Realm::Real, None, 2 + i as i64, &[]).unwrap()); }
    let mut h = Hub::new();
    h.push(v);
    let imp = key(9);
    h.import(&imp, vec![("claude-a".into(), json!({"ts": "2026-11-21T01:00:00.000Z", "kind": "item.create", "body": {"id": "Q-x"}}))], 100).unwrap();
    h
}

fn source(h: &Hub) -> Source { let s = h.status(); Source { events: s.events, verified: s.verified } }

#[test]
fn a_whole_export_rebuilds_to_the_same_record() {
    let h = hub_with_record();
    let r = run(&source(&h), h.pull(0, 100).events);
    assert!(r.ok, "{:?}", r.problems);
    assert_eq!((r.exported, r.rebuilt, r.refused, r.resend_added), (8, 8, 0, 0));
    assert_eq!(r.export_set_sha256, r.rebuilt_set_sha256);
    assert_eq!(r.imported_studio_events, 1);
}

#[test]
fn export_order_does_not_matter() {
    let h = hub_with_record();
    let mut evs = h.pull(0, 100).events;
    evs.reverse();
    assert!(run(&source(&h), evs).ok);
}

#[test]
fn a_partial_export_fails() {
    let h = hub_with_record();
    let mut evs = h.pull(0, 100).events;
    evs.pop();
    let r = run(&source(&h), evs);
    assert!(!r.ok);
    assert!(r.problems.iter().any(|p| p.contains("the source reported 8")));
}

#[test]
fn a_tampered_event_fails() {
    let h = hub_with_record();
    let mut evs = h.pull(0, 100).events;
    evs[3].u.body = json!({"note": "changed after signing"});
    let r = run(&source(&h), evs);
    assert!(!r.ok);
    assert!(r.refused >= 1);
}

#[test]
fn a_source_that_does_not_verify_fails() {
    let h = hub_with_record();
    let r = run(&Source { events: 8, verified: false }, h.pull(0, 100).events);
    assert!(!r.ok);
}

#[test]
fn the_report_holds_no_content() {
    let h = hub_with_record();
    let out = serde_json::to_string(&run(&source(&h), h.pull(0, 100).events)).unwrap();
    assert!(!out.contains("note") && !out.contains("Q-x") && !out.contains("claude-a"));
}

#[test]
fn set_checksum_ignores_order_and_repeats() {
    let a = vec!["b".to_string(), "a".to_string()];
    let b = vec!["a".to_string(), "b".to_string(), "a".to_string()];
    assert_eq!(set_sha256(&a), set_sha256(&b));
}
