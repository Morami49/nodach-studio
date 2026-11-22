//! Connector tests (D-128, F-44): every OAuth rule and every MCP tool, end to end, no network.

use foundation_core::event::KeyPair;
use hub_core::mcp::{handle, iso};
use hub_core::oauth::*;
use hub_core::Hub;
use serde_json::{json, Value};

const T0: i64 = 1_762_600_000_000; // 2025-11-08T11:06:40Z
// RFC 7636 appendix B test vector
const VERIFIER: &str = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const CHALLENGE: &str = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

fn cfg() -> Config {
    Config { issuer: "https://hub.example".into(), allowed_logins: vec!["Morami49".into()] }
}
fn p(pairs: &[(&str, &str)]) -> Params {
    pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

/// Register Claude, authorise, sign in upstream as `login`, return (state, redirect URL or error).
fn sign_in(s: &mut OAuthState, login: &str) -> Result<String, OAuthError> {
    s.register(&json!({"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK]}), "c1".into(), T0).unwrap();
    s.authorize(&cfg(), &p(&[("response_type", "code"), ("client_id", "c1"), ("redirect_uri", CLAUDE_CALLBACK),
        ("code_challenge", CHALLENGE), ("code_challenge_method", "S256"), ("state", "claude-state"),
        ("resource", "https://hub.example/mcp")]), "up1".into(), T0).unwrap();
    s.complete(&cfg(), "up1", login, "CODE-1", T0 + 1000)
}

fn exchange(s: &mut OAuthState, verifier: &str) -> Result<Value, OAuthError> {
    s.token(&p(&[("grant_type", "authorization_code"), ("code", "CODE-1"), ("client_id", "c1"),
        ("redirect_uri", CLAUDE_CALLBACK), ("code_verifier", verifier)]), "AT-1", "RT-1", T0 + 2000)
}

#[test]
fn discovery_matches_claude_requirements() {
    let c = cfg();
    let pr = c.protected_resource_metadata();
    assert_eq!(pr["resource"], "https://hub.example/mcp");
    assert_eq!(pr["authorization_servers"][0], "https://hub.example");
    let asm = c.authorization_server_metadata();
    assert_eq!(asm["code_challenge_methods_supported"], json!(["S256"]));
    assert_eq!(asm["token_endpoint_auth_methods_supported"], json!(["none"]));
    assert_eq!(asm["registration_endpoint"], "https://hub.example/register");
    assert!(c.www_authenticate().starts_with("Bearer resource_metadata=\"https://hub.example/.well-known/oauth-protected-resource\""));
}

#[test]
fn pkce_and_redirects() {
    assert!(pkce_ok(VERIFIER, CHALLENGE));
    assert!(!pkce_ok("x".repeat(43).as_str(), CHALLENGE));
    assert!(!pkce_ok("short", CHALLENGE));
    assert!(redirect_allowed(CLAUDE_CALLBACK));
    assert!(redirect_allowed("http://localhost:3118/callback"));
    assert!(redirect_allowed("http://127.0.0.1:54321/callback"));
    assert!(redirect_allowed("http://localhost/callback"));
    for bad in ["https://claude.ai/api/mcp/auth_callback/x", "https://evil.example/callback", "http://localhost.evil.example/callback",
                "http://localhost:80@evil.example/callback", "http://127.0.0.1:12ab/callback", "http://localhost:3118/other"] {
        assert!(!redirect_allowed(bad), "{bad} must be refused");
    }
}

#[test]
fn registration_refuses_foreign_redirects_and_confidential_clients() {
    let mut s = OAuthState::default();
    assert_eq!(s.register(&json!({"redirect_uris": ["https://evil.example/cb"]}), "x".into(), T0).unwrap_err().error, "invalid_redirect_uri");
    assert_eq!(s.register(&json!({"redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": "client_secret_basic"}), "x".into(), T0).unwrap_err().error, "invalid_client_metadata");
    assert!(s.clients.is_empty());
}

#[test]
fn full_flow_issues_tokens_and_authenticates() {
    let mut s = OAuthState::default();
    let url = sign_in(&mut s, "morami49").unwrap(); // login match ignores case
    assert_eq!(url, "https://claude.ai/api/mcp/auth_callback?code=CODE-1&state=claude-state&iss=https%3A%2F%2Fhub.example");
    let t = exchange(&mut s, VERIFIER).unwrap();
    assert_eq!(t["access_token"], "AT-1");
    assert_eq!(t["refresh_token"], "RT-1");
    assert_eq!(s.authenticate(Some("Bearer AT-1"), T0 + 3000), Some("morami49".into()));
    assert_eq!(s.authenticate(Some("Bearer nope"), T0 + 3000), None);
    assert_eq!(s.authenticate(None, T0 + 3000), None);
    assert_eq!(s.authenticate(Some("Bearer AT-1"), T0 + 2000 + ACCESS_TTL_MS), None); // expired
    // only hashes are stored
    let dump = serde_json::to_string(&s).unwrap();
    assert!(!dump.contains("AT-1") && !dump.contains("RT-1") && !dump.contains("CODE-1"));
}

#[test]
fn strangers_cannot_sign_in() {
    let mut s = OAuthState::default();
    let e = sign_in(&mut s, "someone-else").unwrap_err();
    assert_eq!((e.status, e.error), (403, "access_denied"));
    assert!(s.codes.is_empty());
}

#[test]
fn authorize_rejects_bad_requests() {
    let mut s = OAuthState::default();
    s.register(&json!({"redirect_uris": [CLAUDE_CALLBACK]}), "c1".into(), T0).unwrap();
    let base = [("response_type", "code"), ("client_id", "c1"), ("redirect_uri", CLAUDE_CALLBACK),
                ("code_challenge", CHALLENGE), ("code_challenge_method", "S256")];
    let with = |k: &str, v: &str| { let mut q = p(&base); q.insert(k.into(), v.into()); q };
    for (q, want) in [(with("client_id", "unknown"), "invalid_client"), (with("redirect_uri", "http://localhost:1/callback"), "invalid_request"),
                      (with("code_challenge_method", "plain"), "invalid_request"), (with("response_type", "token"), "unsupported_response_type"),
                      (with("resource", "https://other.example/mcp"), "invalid_target")] {
        assert_eq!(s.authorize(&cfg(), &q, "u".into(), T0).unwrap_err().error, want);
    }
    let mut no_pkce = p(&base); no_pkce.remove("code_challenge");
    assert_eq!(s.authorize(&cfg(), &no_pkce, "u".into(), T0).unwrap_err().error, "invalid_request");
    assert!(s.pending.is_empty());
}

#[test]
fn code_rules_pkce_reuse_expiry_client_redirect() {
    // wrong verifier
    let mut s = OAuthState::default(); sign_in(&mut s, "Morami49").unwrap();
    assert_eq!(exchange(&mut s, &"a".repeat(43)).unwrap_err().error, "invalid_grant");
    // reuse revokes what the first exchange issued
    let mut s = OAuthState::default(); sign_in(&mut s, "Morami49").unwrap();
    exchange(&mut s, VERIFIER).unwrap();
    assert_eq!(exchange(&mut s, VERIFIER).unwrap_err().description, "code already used");
    assert_eq!(s.authenticate(Some("Bearer AT-1"), T0 + 3000), None);
    // expired code
    let mut s = OAuthState::default(); sign_in(&mut s, "Morami49").unwrap();
    let late = p(&[("grant_type", "authorization_code"), ("code", "CODE-1"), ("client_id", "c1"), ("redirect_uri", CLAUDE_CALLBACK), ("code_verifier", VERIFIER)]);
    assert_eq!(s.token(&late, "a", "r", T0 + 1000 + CODE_TTL_MS).unwrap_err().description, "code expired");
    // another client, or another redirect
    let mut s = OAuthState::default(); sign_in(&mut s, "Morami49").unwrap();
    let mut other = late.clone(); other.insert("client_id".into(), "c2".into());
    assert_eq!(s.token(&other, "a", "r", T0 + 2000).unwrap_err().error, "invalid_grant");
    let mut redir = late.clone(); redir.insert("redirect_uri".into(), "http://localhost:9/callback".into());
    assert_eq!(s.token(&redir, "a", "r", T0 + 2000).unwrap_err().error, "invalid_grant");
    // a stale upstream state can't be completed
    let mut s = OAuthState::default();
    s.register(&json!({"redirect_uris": [CLAUDE_CALLBACK]}), "c1".into(), T0).unwrap();
    s.authorize(&cfg(), &p(&[("response_type", "code"), ("client_id", "c1"), ("redirect_uri", CLAUDE_CALLBACK),
        ("code_challenge", CHALLENGE), ("code_challenge_method", "S256")]), "up".into(), T0).unwrap();
    assert!(s.complete(&cfg(), "up", "Morami49", "C", T0 + PENDING_TTL_MS + 1).is_err());
}

#[test]
fn refresh_rotates_and_old_token_dies() {
    let mut s = OAuthState::default(); sign_in(&mut s, "Morami49").unwrap(); exchange(&mut s, VERIFIER).unwrap();
    let r = |t: &str| p(&[("grant_type", "refresh_token"), ("refresh_token", t), ("client_id", "c1")]);
    let t2 = s.token(&r("RT-1"), "AT-2", "RT-2", T0 + 5000).unwrap();
    assert_eq!(t2["refresh_token"], "RT-2");
    assert_eq!(s.token(&r("RT-1"), "AT-3", "RT-3", T0 + 6000).unwrap_err().error, "invalid_grant"); // rotated away
    assert_eq!(s.authenticate(Some("Bearer AT-2"), T0 + 6000), Some("Morami49".into()));
    assert_eq!(s.token(&p(&[("grant_type", "password")]), "a", "r", T0).unwrap_err().error, "unsupported_grant_type");
}

#[test]
fn forms_parse_and_encode() {
    let f = parse_form("grant_type=authorization_code&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&x=a+b&bad=%zz");
    assert_eq!(f["redirect_uri"], CLAUDE_CALLBACK);
    assert_eq!(f["x"], "a b");
    assert_eq!(f["bad"], "%zz");
    assert_eq!(enc("a b/c"), "a%20b%2Fc");
}

#[test]
fn iso_timestamps() {
    assert_eq!(iso(0), "1970-01-01T00:00:00.000Z");
    assert_eq!(iso(T0), "2025-11-08T11:06:40.000Z");
    assert_eq!(iso(951_782_400_123), "2000-02-29T00:00:00.123Z"); // leap day
}

#[test]
fn mcp_initialize_list_and_tools() {
    let mut hub = Hub::new();
    let key = KeyPair::from_seed([7; 32]);
    let imp = KeyPair::from_seed([9; 32]);
    hub.import(&imp, vec![("claude-1".into(), json!({"ts": "2026-11-08T10:00:00.000Z", "kind": "comment", "item": "D-001", "body": {"text": "hi"}}))], T0).unwrap();

    let r = handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}), T0).unwrap();
    assert_eq!(r["result"]["protocolVersion"], "2025-06-18");
    assert!(r["result"]["instructions"].as_str().unwrap().contains("call begin_answer"), "every chat is told to use the guard");
    assert!(handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "method": "notifications/initialized"}), T0).is_none());
    let tools = handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), T0).unwrap();
    let names: Vec<&str> = tools["result"]["tools"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["hub_status", "read_events", "hub_log", "read_private", "write_private", "write_private_batch", "send_invite", "link_sign_in", "read_profiles", "begin_answer", "end_answer", "resume", "read_claims", "import_studio_events", "write_event"]);

    let call = |hub: &mut Hub, name: &str, args: Value| handle(hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}}), T0 + 60_000).unwrap()["result"].clone();
    let w = call(&mut hub, "write_event", json!({"kind": "answer", "item": "Q-duress", "body": {"code": "Q86", "text": "A"}}));
    assert_eq!(w["isError"], false);
    let doc = w["structuredContent"]["written"].as_str().unwrap().to_string();
    assert!(doc.starts_with("hub-"));
    // it is exported to Studio Web, authored by the signed-in person, and never re-imported
    let (docs, _) = hub.to_studio(0, 100);
    assert_eq!(docs.len(), 1);
    assert_eq!(docs[0].0, doc);
    assert_eq!(docs[0].1["author"]["id"], "github:Morami49");
    assert_eq!(docs[0].1["ts"], "2025-11-08T11:07:40.000Z");
    assert!(hub.import(&imp, vec![docs[0].clone()], T0 + 1).unwrap().is_empty());
    // read_events sees both the imported Studio event and the new write
    let r = call(&mut hub, "read_events", json!({"after": 0}));
    let evs = r["structuredContent"]["events"].as_array().unwrap();
    assert_eq!(evs.iter().map(|e| e["doc_id"].as_str().unwrap()).collect::<Vec<_>>(), ["claude-1", doc.as_str()]);
    assert_eq!(r["structuredContent"]["skipped_non_studio"], 2); // importer identity + connector identity (F-55)
    assert_eq!(call(&mut hub, "hub_status", json!({}))["structuredContent"]["verified"], true);
    assert_eq!(call(&mut hub, "hub_status", json!({}))["structuredContent"]["build"], hub_core::mcp::BUILD);
    // bad writes are tool errors, not crashes
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "delete-everything", "body": {}}))["isError"], true);
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "comment", "body": {"text": "no item"}}))["isError"], true);
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "item.create", "item": null, "body": {"title": "no id"}}))["isError"], true);
    let unknown = handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}), T0).unwrap();
    assert_eq!(unknown["error"]["code"], -32601);
}

#[test]
fn f46_pull_with_unbounded_limit_after_writes_does_not_panic() {
    // The worker asks for "everything after N" with usize::MAX after every /mcp call. Once the hub held
    // events, start + usize::MAX overflowed and every later call crashed (found in Nowed's W6 test, 8 Nov).
    let mut hub = Hub::new();
    let key = KeyPair::from_seed([7; 32]);
    let w = |hub: &mut Hub, n: u64| handle(hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": n, "method": "tools/call",
        "params": {"name": "write_event", "arguments": {"kind": "comment", "item": "D-128", "body": {"text": format!("write {n}")}}}}), T0 + n as i64).unwrap();
    w(&mut hub, 1);
    let before = hub.status().events;
    assert_eq!(hub.pull(before, usize::MAX).events.len(), 0);
    w(&mut hub, 2);
    assert_eq!(hub.pull(before, usize::MAX).events.len(), 1);
    assert_eq!(hub.pull(usize::MAX, usize::MAX).events.len(), 0);
    let st = handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "hub_status", "arguments": {}}}), T0).unwrap();
    assert_eq!(st["result"]["structuredContent"]["events"], 3); // connector identity + 2 writes
}

#[test]
fn import_docs_checks_input_and_import_keeps_authors() {
    use hub_core::mcp::import_docs;
    assert!(import_docs(&json!({})).is_err());
    assert!(import_docs(&json!({"docs": []})).is_err());
    assert!(import_docs(&json!({"docs": [{"doc_id": "a/b", "event": {"ts": "x", "kind": "comment"}}]})).is_err());
    assert!(import_docs(&json!({"docs": [{"doc_id": "a", "event": {"kind": "comment"}}]})).is_err());
    let ev = json!({"ts": "2026-11-08T22:19:01.000Z", "kind": "comment", "item": "F-58", "branch": "main",
                    "author": {"id": "claude", "label": "Claude", "role": "agent"}, "body": {"text": "hi"}});
    let docs = import_docs(&json!({"docs": [{"doc_id": "claude-11082219-q01", "event": ev}]})).unwrap();
    let mut hub = Hub::new();
    let importer = KeyPair::from_seed([7u8; 32]);
    let made = hub.import(&importer, docs.clone(), T0).unwrap();
    assert_eq!(made.len(), 2); // importer identity + the event
    assert_eq!(hub.import(&importer, docs, T0).unwrap().len(), 0); // repeating is safe
    let key = KeyPair::from_seed([9u8; 32]);
    let r = handle(&mut hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "read_events", "arguments": {"after": 0}}}), T0).unwrap();
    let got = &r["result"]["structuredContent"]["events"][0];
    assert_eq!(got["doc_id"], "claude-11082219-q01");
    assert_eq!(got["event"]["author"]["id"], "claude"); // Q99 A: original author kept
}

#[test]
fn f48_repeat_writes_and_f47_unknown_items() {
    let mut hub = Hub::new();
    let key = KeyPair::from_seed([9u8; 32]);
    let call = |hub: &mut Hub, args: Value| handle(hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "write_event", "arguments": args}}), T0).unwrap()["result"]["structuredContent"].clone();
    // F-47: an unknown target still writes, with a warning
    let a = call(&mut hub, json!({"kind": "comment", "item": "Q-nope", "body": {"text": "x"}, "idempotency_key": "R-1/1"}));
    assert!(a["warning"].as_str().unwrap().contains("Q-nope"));
    let created = call(&mut hub, json!({"kind": "item.create", "item": null, "body": {"id": "Q-new", "type": "question", "title": "t"}}));
    assert!(created.get("warning").is_none());
    let b = call(&mut hub, json!({"kind": "comment", "item": "Q-new", "body": {"text": "y"}}));
    assert!(b.get("warning").is_none());
    let dup = call(&mut hub, json!({"kind": "item.create", "item": null, "body": {"id": "Q-new", "type": "question", "title": "again"}}));
    assert!(dup["warning"].as_str().unwrap().contains("already exists"));
    // F-48: same key → same doc, nothing added, also after a cold start
    let before = hub.status().events;
    let again = call(&mut hub, json!({"kind": "comment", "item": "Q-nope", "body": {"text": "x"}, "idempotency_key": "R-1/1"}));
    assert_eq!(again["written"], a["written"]);
    assert_eq!(again["repeat"], true);
    assert_eq!(hub.status().events, before);
    let all = hub.pull(0, usize::MAX).events;
    let (mut cold, _) = Hub::restore(all);
    let after_restart = call(&mut cold, json!({"kind": "comment", "item": "Q-nope", "body": {"text": "x"}, "idempotency_key": "R-1/1"}));
    assert_eq!(after_restart["written"], a["written"]);
    assert!(cold.knows_item("Q-new"));
}

#[test]
fn i2_profile_writes_are_checked_first_and_read_back() {
    let mut hub = Hub::new();
    let key = KeyPair::from_seed([9u8; 32]);
    let call = |hub: &mut Hub, name: &str, args: Value| handle(hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}}), T0).unwrap()["result"].clone();
    let dev = call(&mut hub, "write_event", json!({"kind": "profile.create", "item": null, "body": {"id": "P-dev", "kind": "developer", "label": "Developer"}}));
    assert_eq!(dev["isError"], false, "{dev}");
    let before = hub.status().events;
    // refused before writing: nothing lands in the record
    for bad in [json!({"kind": "profile.create", "body": {"id": "P-dev", "kind": "work"}}),
                json!({"kind": "account.link", "body": {"identity": "email:nowed@example.com"}}),
                json!({"kind": "connection.create", "body": {"id": "C1", "profile": "P-dev", "tool": "github", "scope": "repo", "secret_ref": "ghp_live"}}),
                json!({"kind": "profile.revoke", "body": {"profile": "P-morami49"}})] {
        let r = call(&mut hub, "write_event", bad.clone());
        assert_eq!(r["isError"], true, "{bad} should be refused");
        assert_eq!(r["structuredContent"]["refused"], true);
    }
    assert_eq!(hub.status().events, before);
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "profile.assign", "body": {"profile": "P-dev", "items": ["D-171"], "migration": true}}))["isError"], false);
    let v = call(&mut hub, "read_profiles", json!({}))["structuredContent"].clone();
    let ids: Vec<&str> = v["profiles"].as_array().unwrap().iter().map(|p| p["id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["P-dev", "P-morami49"]);
    assert_eq!(v["assigned_items"]["P-dev"], 1);
    assert_eq!(hub.profiles().owner_profile("D-171", "Morami49"), "P-dev");
}

#[test]
fn guard_replays_the_17_nov_double_answer() {
    // Two chats get Nowed's "d + can't afford a solicitor" message. The first claims A-spec-identity and answers;
    // the second is told it's a duplicate, can't edit the claimed item, but can still comment.
    let mut hub = Hub::new();
    let key = KeyPair::from_seed([9u8; 32]);
    let call = |hub: &mut Hub, name: &str, args: Value, t: i64| handle(hub, "Morami49", &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}}), t).unwrap()["result"].clone();
    let msg = "d. on point 3, I cannot afford a solicitor's check yet";
    let a = call(&mut hub, "begin_answer", json!({"message": msg, "role": "builder", "items": ["A-spec-identity"]}), T0)["structuredContent"].clone();
    let sa = a["session"].as_str().unwrap().to_string();
    assert_eq!(a["claimed"], json!(["A-spec-identity"]));
    assert!(a.get("duplicate_of").is_none());
    // a second chat, a minute later, same message with different spacing
    let b = call(&mut hub, "begin_answer", json!({"message": "D. on point 3,  i cannot afford a solicitor's check yet", "role": "builder", "items": ["A-spec-identity", "R-2"]}), T0 + 60_000)["structuredContent"].clone();
    let sb = b["session"].as_str().unwrap().to_string();
    assert_eq!(b["duplicate_of"]["session"], json!(sa));
    assert_eq!(b["duplicate_of"]["still_working"], true);
    assert_eq!(b["claimed"], json!(["R-2"]));
    assert_eq!(b["locked_by_others"][0]["item"], "A-spec-identity");
    let edit = call(&mut hub, "write_event", json!({"kind": "item.edit", "item": "A-spec-identity", "session": sb, "body": {"title": "x"}}), T0 + 61_000);
    assert_eq!(edit["isError"], true);
    let comment = call(&mut hub, "write_event", json!({"kind": "comment", "item": "A-spec-identity", "session": sb, "body": {"text": "adds the missing part"}}), T0 + 62_000);
    assert_eq!(comment["isError"], false, "{comment}");
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "item.edit", "item": "A-spec-identity", "session": sa, "body": {"title": "v1.1"}}), T0 + 63_000)["isError"], false);
    assert_eq!(call(&mut hub, "read_claims", json!({}), T0 + 64_000)["structuredContent"]["active"].as_array().unwrap().len(), 2);
    // the first chat finishes; the item is free again, and a third chat sees the reply id
    assert_eq!(call(&mut hub, "end_answer", json!({"session": sa, "reply": "R-261117-7"}), T0 + 65_000)["isError"], false);
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "item.edit", "item": "A-spec-identity", "body": {"title": "y"}}), T0 + 66_000)["isError"], false);
    let c = call(&mut hub, "begin_answer", json!({"message": msg, "role": "auditor"}), T0 + 67_000)["structuredContent"].clone();
    assert_eq!(c["duplicate_of"]["reply"], "R-261117-7");
    let sc = c["session"].as_str().unwrap();
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "status", "item": "D-1", "session": sc, "body": {"status": "done"}}), T0 + 68_000)["isError"], true, "auditors don't change status");
    // an explorer writes to its own branch, created on first use
    let e = call(&mut hub, "begin_answer", json!({"message": "what if shifts first", "role": "explorer", "items": ["D-work-lane"], "branch": "what-if-shifts"}), T0 + 70_000)["structuredContent"].clone();
    let se = e["session"].as_str().unwrap();
    assert_eq!(call(&mut hub, "write_event", json!({"kind": "item.edit", "item": "D-work-lane", "session": se, "body": {"title": "t"}}), T0 + 71_000)["isError"], false);
    let (docs, _) = hub.to_studio(0, 500);
    let last = &docs.last().unwrap().1;
    assert_eq!(last["branch"], "what-if-shifts");
    assert!(docs.iter().any(|(_, ev)| ev["kind"] == "branch.create" && ev["body"]["name"] == "what-if-shifts"));
    assert!(!docs.iter().any(|(_, ev)| ev.to_string().contains("solicitor")), "the message text is never stored");
    assert_eq!(call(&mut hub, "begin_answer", json!({"message": "x", "role": "explorer"}), T0)["isError"], true);
}

#[test]
fn a_tenant_sees_and_writes_only_their_space() {
    // A1 d step one: before Insydr signs in, a member must never see Nodach's governance or other spaces.
    let mut hub = Hub::new();
    hub.set_owners(vec!["Morami49".into()]);
    let key = KeyPair::from_seed([9u8; 32]);
    let call = |hub: &mut Hub, who: &str, name: &str, args: Value| handle(hub, who, &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args}}), T0).unwrap()["result"].clone();
    let w = |hub: &mut Hub, who: &str, args: Value| call(hub, who, "write_event", args);
    assert_eq!(w(&mut hub, "Morami49", json!({"kind": "item.create", "item": null, "body": {"id": "D-nodach-org-model", "type": "decision", "title": "ownership"}}))["isError"], false);
    assert_eq!(w(&mut hub, "Morami49", json!({"kind": "item.create", "item": null, "body": {"id": "INSYDR-pilot", "type": "asset", "title": "pilot board"}}))["isError"], false);
    // a member can't grant themselves anything
    let forged = w(&mut hub, "nassar-insydr", json!({"kind": "access.grant", "item": null, "body": {"login": "nassar-insydr", "prefixes": ["D-"]}}));
    assert_eq!(forged["structuredContent"]["refused"], true, "{forged}");
    assert_eq!(w(&mut hub, "Morami49", json!({"kind": "access.grant", "item": null, "body": {"login": "nassar-insydr", "prefixes": ["INSYDR-"]}}))["isError"], false);
    let seen = call(&mut hub, "nassar-insydr", "read_events", json!({"after": 0, "limit": 200}))["structuredContent"]["events"].clone();
    let ids: Vec<String> = seen.as_array().unwrap().iter().filter_map(|d| d["event"]["body"]["id"].as_str().map(String::from)).collect();
    assert_eq!(ids, ["INSYDR-pilot"], "the member sees their space only");
    assert_eq!(w(&mut hub, "nassar-insydr", json!({"kind": "comment", "item": "INSYDR-pilot", "body": {"text": "start with the hotel fit-out"}}))["isError"], false);
    assert_eq!(w(&mut hub, "nassar-insydr", json!({"kind": "comment", "item": "D-nodach-org-model", "body": {"text": "hi"}}))["structuredContent"]["refused"], true);
    let owner_sees = call(&mut hub, "Morami49", "read_events", json!({"after": 0, "limit": 200}))["structuredContent"]["events"].as_array().unwrap().len();
    assert_eq!(owner_sees, 4, "the owner sees everything: two items, the grant and the tenant's comment");
}

#[test]
fn link_sign_in_hashes_the_address_and_is_owner_only() {
    // F-two-accounts-one-person: the owner links their Gmail once; the record holds only its hash.
    let mut hub = Hub::new();
    hub.set_owners(vec!["Morami49".into()]);
    let key = KeyPair::from_seed([9u8; 32]);
    let call = |hub: &mut Hub, who: &str, args: Value| handle(hub, who, &key, &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "link_sign_in", "arguments": args}}), T0).unwrap()["result"].clone();
    let r = call(&mut hub, "friend", json!({"email": "owner.test@gmail.com"}));
    assert_eq!(r["structuredContent"]["refused"], true, "non-owners can't link: {r}");
    let r = call(&mut hub, "Morami49", json!({"email": "Owner.Test@gmail.com"}));
    assert_eq!(r["isError"], false, "{r}");
    assert_eq!(r["structuredContent"]["sign_in_as"], "Morami49");
    assert!(!r.to_string().to_ascii_lowercase().contains("owner.test"), "the address is never returned");
    let (docs, _) = hub.to_studio(0, 500);
    assert!(!docs.iter().any(|(_, ev)| ev.to_string().to_ascii_lowercase().contains("owner.test")), "nor stored");
    let h = hub_core::session::email_hash("owner.test@gmail.com");
    assert_eq!(hub.profiles().sign_in_login_for_email_hash(&h).as_deref(), Some("morami49"));
    assert_eq!(call(&mut hub, "Morami49", json!({"email": "owner.test@gmail.com"}))["isError"], true, "already linked");
    assert_eq!(call(&mut hub, "Morami49", json!({"email": "not an email"}))["isError"], true);
    let r = call(&mut hub, "Morami49", json!({"email": "owner.test@gmail.com", "unlink": true}));
    assert_eq!(r["isError"], false, "{r}");
    assert_eq!(hub.profiles().sign_in_login_for_email_hash(&h), hub_core::session::login_for_hash(&h));
}
