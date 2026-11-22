//! Runnable flows (D-174 C2): what the hub plans and computes from a canvas, without any network.

use hub_core::flows::{self, *};
use serde_json::{json, Value};

fn node(id: &str, step: &str, config: Value) -> Value {
    json!({"kind": "flow.node", "branch": "main", "item": null, "body": {"board": "main", "id": id, "step": step, "title": id, "config": config}})
}
fn wire(id: &str, a: &str, b: &str) -> Value {
    json!({"kind": "flow.wire", "branch": "main", "item": null, "body": {"board": "main", "id": id, "from": a, "to": b}})
}

#[test]
fn fold_keeps_latest_config_and_drops_deleted() {
    let evs = vec![
        node("N1", "fetch", json!({"url": "https://a"})),
        node("N2", "filter", json!({})),
        node("N3", "post", json!({})),
        wire("W1", "N1", "N2"), wire("W2", "N2", "N3"),
        json!({"kind": "flow.node", "branch": "main", "body": {"board": "main", "id": "N1", "config": {"url": "https://b"}}}),
        json!({"kind": "flow.node", "branch": "main", "body": {"board": "main", "id": "N3", "deleted": true}}),
        node("NX", "shell", json!({})),                       // not a step: ignored
        json!({"kind": "flow.node", "branch": "try", "body": {"board": "main", "id": "NB", "step": "post"}}), // other branch
    ];
    let f = fold(&evs, "main");
    assert_eq!(f.nodes.len(), 2);
    assert_eq!(f.nodes["N1"].config["url"], "https://b");
    assert_eq!(f.nodes["N1"].step, "fetch", "an edit without step keeps the step");
    assert_eq!(f.wires, vec![("N1".into(), "N2".into())], "wires to deleted steps are gone");
}

#[test]
fn plan_orders_steps_after_their_inputs() {
    // N1 → N2 → N4, N1 → N3 → N4
    let evs = vec![node("N1", "fetch", json!({})), node("N2", "filter", json!({})), node("N3", "calc", json!({})), node("N4", "post", json!({})),
                   wire("a", "N1", "N2"), wire("b", "N1", "N3"), wire("c", "N2", "N4"), wire("d", "N3", "N4")];
    let f = fold(&evs, "main");
    let p = plan(&f, "N1").unwrap();
    let pos = |x: &str| p.iter().position(|y| y == x).unwrap();
    assert_eq!(p.len(), 4);
    assert!(pos("N1") < pos("N2") && pos("N1") < pos("N3") && pos("N2") < pos("N4") && pos("N3") < pos("N4"));
    assert_eq!(inputs_of(&f, "N4"), vec!["N2".to_string(), "N3".to_string()]);
    // starting in the middle runs only what follows
    assert_eq!(plan(&f, "N3").unwrap(), vec!["N3".to_string(), "N4".to_string()]);
}

#[test]
fn plan_refuses_loops_and_unknown_steps() {
    let evs = vec![node("N1", "fetch", json!({})), node("N2", "filter", json!({})), wire("a", "N1", "N2"), wire("b", "N2", "N1")];
    assert!(plan(&fold(&evs, "main"), "N1").is_err());
    assert!(plan(&fold(&evs, "main"), "NOPE").is_err());
}

#[test]
fn filter_with_all_and_none() {
    let items = vec![
        json!({"title": "Night warehouse operative", "pay": 14.5, "needs": "none"}),
        json!({"title": "Delivery driver (own car)", "pay": 16, "needs": "licence"}),
        json!({"title": "Event steward", "pay": "", "needs": "none"}),
        json!({"title": "Kitchen porter", "pay": "£12.20", "needs": "steel-toe boots"}),
    ];
    let cfg = json!({"all": [{"field": "pay", "op": "exists"}, {"field": "pay", "op": ">=", "value": 12}],
                     "none": [{"field": "needs", "op": "contains", "value": "licence"}, {"field": "needs", "op": "contains", "value": "boots"}]});
    let out = filter(items, &cfg).unwrap();
    assert_eq!(out.len(), 1);
    assert_eq!(out[0]["title"], "Night warehouse operative");
    assert!(filter(vec![json!({"a": 1})], &json!({"all": [{"field": "a", "op": "~="}]})).is_err());
}

#[test]
fn calc_sets_fields_and_never_guesses() {
    let items = vec![json!({"pay": 120, "travel": 10, "stay": 30}), json!({"pay": 100, "travel": 0, "stay": 0}), json!({"pay": 90})];
    let cfg = json!({"sets": [{"set": "cost", "a": "travel", "op": "+", "b": "stay"}, {"set": "ratio", "a": "pay", "op": "/", "b": "cost"}]});
    let out = calc(items, &cfg).unwrap();
    assert_eq!(out[0]["cost"], json!(40.0));
    assert_eq!(out[0]["ratio"], json!(3.0));
    assert_eq!(out[1]["ratio"], Value::Null, "divide by zero gives no number, not infinity");
    assert_eq!(out[2]["cost"], Value::Null, "missing fields give no number");
    let out = calc(vec![json!({"p": 10})], &json!({"sets": [{"set": "x", "a": "p", "op": "*", "b": 1.35}]})).unwrap();
    assert_eq!(out[0]["x"], json!(13.5));
}

#[test]
fn render_templates() {
    let it = json!({"jobTitle": "Steward", "pay": {"min": 12.5}, "n": 3});
    assert_eq!(render("{{jobTitle}} · £{{pay.min}} · {{n}} · {{missing}}", &it), "Steward · £12.5 · 3 · ");
    assert_eq!(render("no braces", &it), "no braces");
    assert_eq!(render("half {{open", &it), "half {{open");
}

#[test]
fn items_from_a_fetched_body() {
    let body = json!({"results": [{"a": 1}, {"a": 2}], "total": 2});
    assert_eq!(as_items(body.clone(), "results").len(), 2);
    assert_eq!(as_items(body.clone(), "").len(), 1);
    assert_eq!(as_items(body, "nope").len(), 0);
    assert_eq!(get_path(&json!({"a": [{"b": 7}]}), "a.0.b"), json!(7));
}

#[test]
fn flows_cannot_reach_hub_secrets_or_the_hub_itself() {
    assert_eq!(secret_var("reed").as_deref(), Some("NODE_SECRET_REED"));
    for bad in ["", "HUB WRITE", "../x", "a-b"] { assert!(secret_var(bad).is_none(), "{bad}"); }
    assert!(url_allowed("https://www.reed.co.uk/api/1.0/search?keywords=temp"));
    assert!(url_allowed("https://notnodach.com/x"), "a different domain that merely ends in nodach.com is allowed");
    for bad in ["http://plain.example", "https://clanstack-hub.ndab-utube.workers.dev/studio/api/events", "https://localhost/x",
                "https://user@evil.example/", "ftp://x", "https://127.0.0.1/",
                "https://studio.nodach.com/studio/api/events", "https://staging.nodach.com/x", "https://nodach.com/",
                "https://STUDIO.NODACH.COM/x", "https://studio.nodach.com./x", "https://studio.nodach.com:443/x"] {
        assert!(!url_allowed(bad), "{bad}");
    }
}

#[test]
fn base64_matches_rfc_4648() {
    for (i, o) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg=="), ("fooba", "Zm9vYmE="), ("foobar", "Zm9vYmFy")] {
        assert_eq!(base64(i.as_bytes()), o);
    }
}

#[test]
fn runner_seed_is_separate_from_sessions() {
    assert_ne!(runner_seed("s"), hub_core::session::session_key("s"));
    assert_eq!(runner_seed("s"), runner_seed(" s "));
}

// ---- schedules (S3)
fn ms(y: i64, mo: u32, d: u32, h: u32) -> i64 {
    // test-only: UTC wall time → ms, via chrono-free arithmetic on a known anchor (2026-01-01T00:00Z)
    let anchor = 1_767_225_600_000i64;
    let days_before = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334][(mo - 1) as usize] as i64;
    assert_eq!(y, 2026);
    anchor + (days_before + d as i64 - 1) * 86_400_000 + h as i64 * 3_600_000
}

#[test]
fn uk_slot_follows_british_summer_time() {
    assert_eq!(flows::uk_slot(ms(2026, 1, 15, 7)), (7, "2026-01-15T07".into()));         // winter: UK = UTC
    assert_eq!(flows::uk_slot(ms(2026, 7, 1, 6)), (7, "2026-07-01T07".into()));          // summer: UK = UTC+1
    // clocks go forward 29 Mar 2026 at 01:00 UTC
    assert_eq!(flows::uk_slot(ms(2026, 3, 29, 0)).0, 0);
    assert_eq!(flows::uk_slot(ms(2026, 3, 29, 1)).0, 2);
    // clocks go back 25 Oct 2026 at 01:00 UTC
    assert_eq!(flows::uk_slot(ms(2026, 10, 25, 0)).0, 1);
    assert_eq!(flows::uk_slot(ms(2026, 10, 25, 1)).0, 1);
    assert_eq!(flows::uk_slot(ms(2026, 11, 13, 23)), (23, "2026-11-13T23".into()));
}

#[test]
fn schedule_hours_reads_numbers_or_text() {
    assert_eq!(flows::schedule_hours(&json!({"schedule": [18, 7, 7]})), vec![7, 18]);
    assert_eq!(flows::schedule_hours(&json!({"schedule": "7, 18 25 x"})), vec![7, 18]);
    assert!(flows::schedule_hours(&json!({})).is_empty());
}

#[test]
fn due_runs_each_scheduled_start_once_per_slot() {
    let node = |id: &str, step: &str, cfg: Value| json!({"kind": "flow.node", "item": null, "body": {"board": "main", "id": id, "step": step, "config": cfg}});
    let mut evs = vec![
        node("S-a", "fetch", json!({"schedule": "7,18"})),
        node("S-b", "post", json!({})),
        json!({"kind": "flow.wire", "item": null, "body": {"board": "main", "id": "W-1", "from": "S-a", "to": "S-b"}}),
        node("S-c", "fetch", json!({})),                       // no schedule: by hand only
    ];
    let seven = ms(2026, 11, 13, 7);
    assert_eq!(flows::due(&evs, seven), vec![("main".into(), "S-a".into(), "2026-11-13T07".into())]);
    assert!(flows::due(&evs, ms(2026, 11, 13, 8)).is_empty());
    evs.push(json!({"kind": "flow.run", "item": null, "body": {"board": "main", "start": "S-a", "slot": "2026-11-13T07", "ok": true}}));
    assert!(flows::due(&evs, seven + 1_800_000).is_empty(), "already ran in this slot");
    assert_eq!(flows::due(&evs, ms(2026, 11, 13, 18)).len(), 1);
}

#[test]
fn posted_keys_are_read_from_the_items_comments() {
    let evs = vec![
        json!({"kind": "comment", "item": "LOBBY-income", "body": {"text": "a", "key": "123"}}),
        json!({"kind": "comment", "item": "LOBBY-income", "body": {"text": "by hand"}}),
        json!({"kind": "comment", "item": "OTHER", "body": {"text": "b", "key": "456"}}),
    ];
    let k = flows::posted_keys(&evs, "LOBBY-income");
    assert!(k.contains("123") && !k.contains("456") && k.len() == 1);
}

#[test]
fn private_vars_render_in_the_prompt_but_never_reach_the_item() {
    // Shift Scout's starting area comes from a hub secret, not the template or the record (18 Nov).
    let it = serde_json::json!({"locationName": "Hounslow"});
    let mut v = serde_json::Map::new();
    v.insert("home".into(), serde_json::json!("North Pole"));
    let view = hub_core::flows::with_vars(&it, &v);
    assert_eq!(hub_core::flows::render("from {{vars.home}} to {{locationName}}", &view), "from North Pole to Hounslow");
    assert!(it.get("vars").is_none());
}

#[test]
fn ai_step_retries_busy_and_timed_out_models_only() {
    // 21 Nov 18:00 job flow stopped on "the AI (gemini-flash-latest) answered 524": a timeout, worth another try.
    for s in [0u16, 408, 429, 500, 502, 503, 504, 520, 522, 524, 529] {
        assert!(hub_core::flows::ai_retryable(s), "{s} should be retried");
    }
    for s in [200u16, 400, 401, 403, 404, 413, 501, 505, 525, 530] {
        assert!(!hub_core::flows::ai_retryable(s), "{s} should stop the flow");
    }
}
