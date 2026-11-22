//! MCP server for the hub (D-104, D-121, D-128): JSON-RPC 2.0 over Streamable HTTP (POST, JSON
//! responses). Pure logic; the worker passes the signed-in login, the connector key and `now`.
//!
//! Tools (all act on the shared record, except read_private, which serves the owner's own Private tier):
//!   hub_status   counts and whether the whole chain verifies
//!   read_events  Studio-shaped events from a cursor: imported Studio Web events + writes from other nodes
//!   hub_log      (served by the node, not here) the private request log: time, tool, outcome, duration; never contents
//!   import_studio_events  (served by the node) Studio Web's events copied in with their own authors (D-129, Q99 A)
//!   write_event  add a Studio-shaped event (answer, comment, status, item.create, item.edit, link),
//!                signed by the connector key; it reaches Studio Web through /studio-export (D-101). Profile kinds
//!                (account.link, profile.*, connection.*) are checked against the profile rules before writing (I2)
//!   link_sign_in  owner only: link an email sign-in (Google or emailed link) to your own account. The address is
//!                hashed here and dropped; only account.link {identity: email:sha256:<hash>} is written (F-two-accounts-one-person)
//!   read_profiles  the signed-in owner's account, profiles, connections (refs only) and findings (I2)
//!   begin_answer / end_answer / read_claims  the multi-chat guard (D-nodach-parallel-guard v2): duplicate-message
//!                check, item claims, roles; write_event takes the session it writes under

use crate::{Hub, studio_doc_id};
use foundation_core::event::KeyPair;
use serde_json::{json, Value};

pub const PROTOCOL_VERSIONS: [&str; 3] = ["2025-11-25", "2025-06-18", "2025-03-26"];
/// Commit the node was built from (set by the deploy as BUILD_SHA), so a test knows which code answered.
pub const BUILD: &str = match option_env!("BUILD_SHA") { Some(b) => b, None => "dev" };
pub const WRITE_KINDS: [&str; 16] = ["answer", "comment", "status", "item.create", "item.edit", "link",
    "account.link", "profile.create", "profile.revoke", "profile.assign", "connection.create", "connection.revoke", "access.grant", "access.revoke", "access.invite", "access.uninvite"];

/// Milliseconds since 1970 → `YYYY-MM-DDTHH:MM:SS.mmmZ` (Studio's timestamp shape).
pub fn iso(ms: i64) -> String {
    let (secs, milli) = (ms.div_euclid(1000), ms.rem_euclid(1000));
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // civil-from-days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}.{milli:03}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn tools() -> Value {
    json!([
        {"name": "hub_status", "title": "Hub status",
         "description": "How many events the Clanstack hub holds and whether its whole signed chain verifies.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": true}},
        {"name": "read_events", "title": "Read Studio events",
         "description": "Read Clanstack Studio events from the hub's shared record, oldest first, from a cursor. Returns {events:[{doc_id, event}], next}; pass next back as after to continue.",
         "inputSchema": {"type": "object", "properties": {
             "after": {"type": "integer", "minimum": 0, "description": "cursor from a previous call (0 to start)"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
         "annotations": {"readOnlyHint": true}},
        {"name": "hub_log", "title": "Request log",
         "description": "The hub's private request log, newest first: time, route, tool, outcome, ms waiting on network or storage, who (login) and build. Never contents. Includes this call. Use it to time tests and see failed calls.",
         "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
         "annotations": {"readOnlyHint": true}},
        {"name": "read_private", "title": "Read your private items",
         "description": "Read the signed-in owner's Private tier: personal items kept on the hub encrypted and apart from the shared record (people, messages, profile facts, private notes). Optional path (data/users/<you>/...) narrows it; after/limit page through. Sealed items are never readable. Treat results as personal data and never copy them into shared items.",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "optional, e.g. data/users/<login>/vault/events"},
             "after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
         "annotations": {"readOnlyHint": true}},
        {"name": "write_private", "title": "Write to your private items",
         "description": "Add one event to the signed-in owner's Private tier (encrypted, apart from the shared record): personal notes, decisions, people, the personal trail of a conversation. path must be data/users/<you>/... (e.g. data/users/github:<login>/trail/events). event needs kind and ts (read the clock, never invent it). A repeated idempotency_key returns the first write.",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string"},
             "event": {"type": "object", "description": "{kind, ts, item?, body}"},
             "idempotency_key": {"type": "string", "maxLength": 200}},
           "required": ["path", "event"]},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "write_private_batch", "title": "Write many private items",
         "description": "Store up to 200 events in the signed-in owner's Private tier in one call (e.g. importing a spreadsheet). Each item is {path, event:{kind, ts, ...}, key}; the key makes re-runs safe. The whole batch is checked first: one bad item rejects it with every reason listed. Returns per-item doc ids and SHA-256 checksums, counts of new and repeated items, and one checksum for the batch, so an import can be verified row by row.",
         "inputSchema": {"type": "object", "required": ["items"], "properties": {
             "items": {"type": "array", "maxItems": 200, "items": {"type": "object", "required": ["path", "event", "key"], "properties": {
                 "path": {"type": "string"}, "event": {"type": "object"}, "key": {"type": "string", "maxLength": 200}}}}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "send_invite", "title": "Invite someone by email",
         "description": "Owner only. Invites a person to sign in to Studio with Google or an emailed link, optionally giving them spaces (id prefixes such as INSYDR-), and emails them a welcome with how to sign in. The address is used to send that email and is never stored: the record keeps only its SHA-256 fingerprint and the label you give.",
         "inputSchema": {"type": "object", "required": ["email", "label"], "properties": {
             "email": {"type": "string"}, "label": {"type": "string", "maxLength": 80, "description": "how they appear to you, e.g. 'Nassar (Insydr)'"},
             "prefixes": {"type": "array", "maxItems": 10, "items": {"type": "string"}, "description": "spaces to give them, e.g. [\"INSYDR-\"]"},
             "note": {"type": "string", "maxLength": 600, "description": "a line from you to include in the welcome email"},
             "send_email": {"type": "boolean", "description": "default true; false records the invite without emailing"}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "link_sign_in", "title": "Also sign in as me with an email",
         "description": "Owner only. Links an email address to your own account, so signing in to Studio with that address (Google or an emailed link) opens your account instead of a separate email account. The address is hashed on the hub and never stored: the record keeps only account.link {identity: email:sha256:<hash>}. An address already linked to another account is refused. unlink: true removes the link again.",
         "inputSchema": {"type": "object", "required": ["email"], "properties": {
             "email": {"type": "string"},
             "unlink": {"type": "boolean", "description": "default false; true removes an existing link"}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "read_profiles", "title": "Read your profiles",
         "description": "The signed-in owner's account, linked sign-ins, profiles (Personal, Developer, ...), tool connections (where each secret lives, never its value), how many items each profile owns, and any profile events the hub ignored with the reason.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": true}},
        {"name": "begin_answer", "title": "Begin answering a message",
         "description": "Call before writing anything for a message from Nowed. Pass the message text (only its fingerprint is stored), your role (builder, interviewer, auditor, explorer) and the item ids you expect to change. Returns a session id to pass to write_event, any earlier session that already answered the same message (read its reply and add only what's missing), and any items other sessions have claimed (comment on those, don't edit them). An explorer must name a branch; its writes go there, not to main.",
         "inputSchema": {"type": "object", "required": ["message", "role"], "properties": {
             "message": {"type": "string", "description": "the message being answered; hashed, never stored"},
             "role": {"type": "string", "enum": ["builder", "interviewer", "auditor", "explorer"]},
             "items": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
             "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "description": "how long the claim lasts (default 15)"},
             "branch": {"type": "string", "description": "explorer only: the Studio branch to write to"}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false}},
        {"name": "end_answer", "title": "Finish answering",
         "description": "Release your session's claims once your reply item (R-...) is written, so other sessions can edit those items.",
         "inputSchema": {"type": "object", "required": ["session"], "properties": {
             "session": {"type": "string"}, "reply": {"type": "string", "description": "the R- item you wrote"}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "resume", "title": "Resume context (read first)",
         "description": "What every new chat or tool reads before answering Nowed: the active rules (G-, RULE-), open questions (Q-, Q-PACK-) with their last answer code, the latest replies (R-) from every chat, and open findings. Keeps context across compressed and parallel chats. begin_answer returns the same digest.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": true}},
        {"name": "read_claims", "title": "Who is working on what",
         "description": "Active session claims (session, role, items, until), so a new chat can see what other chats are doing before it starts.",
         "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": true}},
        {"name": "import_studio_events", "title": "Import Studio events",
         "description": "Copy Studio Web events into the hub's shared record, keeping each event's original author (Q99 A); the hub's importer key signs the copy. Doc ids already in the hub are skipped, so repeating is safe. Used by Studio Web's sync (D-129).",
         "inputSchema": {"type": "object", "required": ["docs"], "properties": {
             "docs": {"type": "array", "maxItems": 200, "items": {"type": "object", "required": ["doc_id", "event"], "properties": {
                 "doc_id": {"type": "string"}, "event": {"type": "object"}}}}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": true}},
        {"name": "write_event", "title": "Write a Studio event",
         "description": "Add an event to the shared Clanstack record, e.g. an answer to a question, a comment, a status change or a new item. It appears in Studio Web on its next sync. Never use for private material. Profile kinds (account.link {identity}, profile.create {id,kind,label}, profile.revoke {profile}, profile.assign {profile,items}, connection.create {id,profile,tool,scope,secret_ref}, connection.revoke {connection}) are checked first and refused with the reason if they break a rule.",
         "inputSchema": {"type": "object", "required": ["kind", "body"], "properties": {
             "kind": {"type": "string", "enum": WRITE_KINDS},
             "item": {"type": ["string", "null"], "description": "target item id, e.g. Q-duress; null for item.create"},
             "idempotency_key": {"type": "string", "maxLength": 200, "description": "optional; repeating a write with the same key returns the first write instead of adding another (use one per intended change, e.g. R-261109-1/3)"},
             "session": {"type": "string", "description": "the session id from begin_answer; its role and branch apply. Without one, the write still respects other sessions' claims."},
             "body": {"type": "object", "description": "event body, e.g. {text} for a comment, {code,text} for an answer, {status} for a status, {id,type,title,body,status,refs} for item.create"}}},
         "annotations": {"readOnlyHint": false, "destructiveHint": false}}
    ])
}

/// Check and unpack `import_studio_events` input: 1..=200 docs, each a doc id (1-200 chars from the
/// Studio id alphabet) and an event object with a string `ts` and `kind`. Returns the docs or an error text.
pub fn import_docs(args: &Value) -> Result<Vec<(String, Value)>, String> {
    let docs = args.get("docs").and_then(Value::as_array).ok_or("docs must be an array")?;
    if docs.is_empty() || docs.len() > 200 { return Err("send 1 to 200 docs per call".into()); }
    let mut out = Vec::with_capacity(docs.len());
    for (i, d) in docs.iter().enumerate() {
        let id = d.get("doc_id").and_then(Value::as_str).unwrap_or("");
        let ok_id = !id.is_empty() && id.len() <= 200 && id.chars().all(|c| c.is_ascii_alphanumeric() || "_-.~:@+".contains(c));
        let ev = d.get("event").filter(|e| e.is_object()).ok_or(format!("docs[{i}].event must be an object"))?;
        if !ok_id { return Err(format!("docs[{i}].doc_id is not a Studio doc id")); }
        if ev.get("ts").and_then(Value::as_str).is_none() || ev.get("kind").and_then(Value::as_str).is_none() {
            return Err(format!("docs[{i}].event needs ts and kind"));
        }
        out.push((id.to_string(), ev.clone()));
    }
    Ok(out)
}

/// The result envelope for a tool answer (used by the node for the tools it serves itself).
pub fn tool_result(v: Value, is_error: bool) -> Value { text_result(v, is_error) }

fn text_result(v: Value, is_error: bool) -> Value {
    json!({"content": [{"type": "text", "text": v.to_string()}], "structuredContent": v, "isError": is_error})
}

fn call(hub: &mut Hub, login: &str, key: &KeyPair, name: &str, args: &Value, now: i64) -> Value {
    match name {
        "hub_status" => {
            let mut v = serde_json::to_value(hub.status()).unwrap_or(Value::Null);
            if let Some(o) = v.as_object_mut() { o.insert("build".into(), json!(BUILD)); }
            text_result(v, false)
        }
        "hub_log" | "import_studio_events" | "read_private" | "write_private" | "write_private_batch" | "send_invite" => text_result(json!({"error": format!("{name} is served by the node, not hub-core")}), true),
        "read_profiles" => text_result(hub.profiles().view_for(login), false),
        "link_sign_in" => {
            let email = args.get("email").and_then(Value::as_str).unwrap_or("");
            let unlink = args.get("unlink").and_then(Value::as_bool).unwrap_or(false);
            match link_sign_in(hub, login, key, email, unlink, "claude-connector", now) {
                Ok(v) => text_result(v, false),
                Err(e) => text_result(json!({"error": e, "refused": true}), true),
            }
        }
        "resume" => text_result(hub.digest_for(login), false),
        "read_claims" => {
            let g = hub.guard();
            text_result(json!({"active": g.active(now), "now": iso(now)}), false)
        }
        "begin_answer" => begin_answer(hub, login, key, args, now),
        "end_answer" => {
            let session = args.get("session").and_then(Value::as_str).unwrap_or("");
            let g = hub.guard();
            match g.claims.get(session) {
                None => text_result(json!({"error": format!("session {session} doesn't exist")}), true),
                Some(c) if c.released.is_some() => text_result(json!({"ended": session, "repeat": true}), false),
                Some(_) => {
                    let mut body = json!({"session": session});
                    if let Some(r) = args.get("reply").and_then(Value::as_str) { body["reply"] = json!(r); }
                    match write_studio(hub, login, key, "session.release", Value::Null, body, "main", Some(&format!("{session}/end")), now) {
                        Ok(_) => text_result(json!({"ended": session}), false),
                        Err(e) => text_result(json!({"error": e}), true),
                    }
                }
            }
        }
        "read_events" => {
            let after = args.get("after").and_then(Value::as_u64).unwrap_or(0) as usize;
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(100).clamp(1, 200) as usize;
            // A1 d: a member gets only the spaces they were given and their own writes
            let (events, next, total, skipped) = hub.studio_events_for(login, after, limit);
            // F-55: say what was skipped, so nobody guesses (a key's first fact is its identity, not a Studio event)
            text_result(json!({"events": events, "next": next, "total": total, "skipped_non_studio": skipped,
                "note": "Events that are not Studio events (e.g. a key's identity fact) are skipped and counted in skipped_non_studio. Private items are never in this shared record: they are kept apart, encrypted, and only read_private serves them to their owner."}), false)
        }
        "write_event" => {
            let kind = args.get("kind").and_then(Value::as_str).unwrap_or("");
            if !WRITE_KINDS.contains(&kind) {
                return text_result(json!({"error": format!("kind must be one of {WRITE_KINDS:?}")}), true);
            }
            let body = match args.get("body") { Some(b) if b.is_object() => b.clone(), _ => return text_result(json!({"error": "body must be an object"}), true) };
            let item = args.get("item").cloned().unwrap_or(Value::Null);
            // A1 d: members write only inside the spaces they were given; only owners grant access
            let subject = if kind == "item.create" { body.get("id").and_then(Value::as_str).map(str::to_string) } else { item.as_str().map(str::to_string) };
            if let Err(e) = hub.access().check_write(login, kind, subject.as_deref()) {
                return text_result(json!({"error": e, "refused": true}), true);
            }
            if kind != "item.create" && !crate::profiles::KINDS.contains(&kind) && !crate::access::KINDS.contains(&kind) && !item.is_string() {
                return text_result(json!({"error": "item is required for this kind"}), true);
            }
            if kind == "item.create" && body.get("id").and_then(Value::as_str).is_none() {
                return text_result(json!({"error": "item.create needs body.id"}), true);
            }
            // F-48: a repeated write (a retry after a timeout, a re-run turn) returns the first one
            let idem = args.get("idempotency_key").and_then(Value::as_str).filter(|k| !k.is_empty() && k.len() <= 200);
            if let Some(doc) = idem.and_then(|k| hub.written_with(k)) {
                return text_result(json!({"written": doc, "repeat": true, "note": "a write with this idempotency_key already exists; nothing was added"}), false);
            }
            // I2: profile kinds must pass the profile rules now, so a refused event never lands in the record
            if crate::profiles::KINDS.contains(&kind) {
                let probe = json!({"ts": iso(now), "kind": kind, "item": item, "body": body,
                                   "author": {"id": format!("github:{login}"), "role": "owner"}});
                if let Err(reason) = hub.profiles().check(&probe) {
                    return text_result(json!({"error": reason, "refused": true}), true);
                }
            }
            // D-nodach-parallel-guard v2: claims by other sessions, the writer's role and its branch
            let session = args.get("session").and_then(Value::as_str);
            let branch = match hub.guard().check_write(session, kind, item.as_str(), &body, now) {
                Ok(b) => b,
                Err(reason) => return text_result(json!({"error": reason, "refused": true}), true),
            };
            // F-47: say when the target isn't known here, so a typo or an unsynced item doesn't pass silently
            let warning = match (kind, item.as_str()) {
                ("item.create", _) => body.get("id").and_then(Value::as_str).filter(|id| hub.knows_item(id))
                    .map(|id| format!("{id} already exists here; Studio keeps the first and ignores this create (F-19). Use item.edit instead.")),
                (_, Some(t)) if !hub.knows_item(t) => Some(format!("{t} isn't in the hub's copy of Studio. If it's new in Studio, sync Studio first; otherwise check the id.")),
                _ => None,
            };
            let mut ev = json!({"ts": iso(now), "kind": kind, "item": item, "branch": branch, "body": body,
                            "author": {"id": format!("github:{login}"), "label": login, "role": "owner"},
                            "via": "claude-connector"});
            if let Some(k) = idem { ev["idem"] = json!(k); }
            if let Some(sid) = session { ev["session"] = json!(sid); }
            match hub.write(key, &format!("connector ({login})"), ev, now) {
                Ok(made) => {
                    let mut r = json!({"written": studio_doc_id(&made.last().map(|e| e.id.clone()).unwrap_or_default())});
                    if let Some(w) = warning { r["warning"] = json!(w); }
                    text_result(r, false)
                }
                Err(e) => text_result(json!({"error": format!("{e:?}")}), true),
            }
        }
        _ => text_result(json!({"error": format!("unknown tool {name}")}), true),
    }
}

/// Link (or, with `unlink`, unlink) an email sign-in to the caller's own account (F-two-accounts-one-person).
/// Owner only; the caller is always the account linked to, so a link can never point at someone else. The address
/// is hashed here and never written or returned: the result names only the first 12 hex characters of its hash.
pub fn link_sign_in(hub: &mut Hub, login: &str, key: &KeyPair, email: &str, unlink: bool, via: &str, now: i64) -> Result<Value, String> {
    if !hub.access().is_owner(login) {
        return Err("only an owner can link sign-ins".into());
    }
    if login.to_ascii_lowercase().starts_with(crate::session::EMAIL_LOGIN_PREFIX) {
        return Err("sign in with your GitHub account to link an email to it".into());
    }
    if !crate::session::plausible_email(email) {
        return Err("that doesn't look like an email address".into());
    }
    let hash = crate::session::email_hash(email);
    let identity = format!("email:sha256:{hash}");
    let mut body = json!({"identity": identity, "account": format!("github:{login}"), "verified": false});
    if unlink { body["revoked"] = json!(true); }
    let ev = json!({"ts": iso(now), "kind": "account.link", "item": null, "branch": "main", "body": body,
                    "author": {"id": format!("github:{login}"), "label": login, "role": "owner"}, "via": via});
    hub.profiles().check(&ev)?;
    let made = hub.write(key, &format!("{via} ({login})"), ev, now).map_err(|e| format!("{e:?}"))?;
    let doc = studio_doc_id(&made.last().map(|e| e.id.clone()).unwrap_or_default());
    let mut out = json!({"written": doc, "account": format!("github:{login}"),
              "sign_in_as": if unlink { crate::session::email_login(email) } else { login.to_string() },
              "stored": "the address's SHA-256 fingerprint only"});
    out[if unlink { "unlinked" } else { "linked" }] = json!(format!("email:sha256:{}…", &hash[..12]));
    Ok(out)
}

/// Write one Studio-shaped event as the signed-in person (used by the guard tools).
#[allow(clippy::too_many_arguments)]
fn write_studio(hub: &mut Hub, login: &str, key: &KeyPair, kind: &str, item: Value, body: Value, branch: &str, idem: Option<&str>, now: i64) -> Result<String, String> {
    if let Some(doc) = idem.and_then(|k| hub.written_with(k)) {
        return Ok(doc.clone());
    }
    let mut ev = json!({"ts": iso(now), "kind": kind, "item": item, "branch": branch, "body": body,
                        "author": {"id": format!("github:{login}"), "label": login, "role": "owner"}, "via": "claude-connector"});
    if let Some(k) = idem { ev["idem"] = json!(k); }
    hub.write(key, &format!("connector ({login})"), ev, now).map(|made| studio_doc_id(&made.last().map(|e| e.id.clone()).unwrap_or_default())).map_err(|e| format!("{e:?}"))
}

fn begin_answer(hub: &mut Hub, login: &str, key: &KeyPair, args: &Value, now: i64) -> Value {
    use crate::guard;
    let message = args.get("message").and_then(Value::as_str).unwrap_or("");
    let role = args.get("role").and_then(Value::as_str).unwrap_or("");
    if message.trim().is_empty() { return text_result(json!({"error": "message is required (only its fingerprint is stored)"}), true); }
    if !guard::ROLES.contains(&role) { return text_result(json!({"error": format!("role must be one of {:?}", guard::ROLES)}), true); }
    let items: Vec<String> = args.get("items").and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_str).map(String::from).collect()).unwrap_or_default();
    if items.len() > guard::MAX_ITEMS { return text_result(json!({"error": format!("claim at most {} items", guard::MAX_ITEMS)}), true); }
    let minutes = args.get("minutes").and_then(Value::as_i64).unwrap_or(guard::DEFAULT_MINUTES).clamp(1, guard::MAX_MINUTES);
    let branch = match (role, args.get("branch").and_then(Value::as_str)) {
        ("explorer", Some(b)) if guard::valid_branch(b) => b.to_string(),
        ("explorer", _) => return text_result(json!({"error": "an explorer needs a branch name (letters, digits, - _ .; not main)"}), true),
        _ => "main".to_string(),
    };
    let fp = guard::fingerprint(message);
    let g = hub.guard();
    let duplicate = g.duplicate_of(&fp).cloned();
    let locked: Vec<Value> = g.locked(&items, now, None).into_iter()
        .map(|(it, c)| json!({"item": it, "session": c.session, "role": c.role, "until": iso(c.until_ms)})).collect();
    let mine: Vec<String> = items.iter().filter(|i| !locked.iter().any(|l| l["item"] == json!(i))).cloned().collect();
    let session = guard::session_id(&fp, login, now);
    if branch != "main" && !g.branches.contains(&branch) {
        if let Err(e) = write_studio(hub, login, key, "branch.create", Value::Null, json!({"name": branch, "fork_ts": iso(now), "from": "main"}), "main", None, now) {
            return text_result(json!({"error": e}), true);
        }
    }
    let body = json!({"session": session, "role": role, "items": mine, "fingerprint": fp, "branch": branch, "until_ms": now + minutes * 60_000});
    if let Err(e) = write_studio(hub, login, key, "session.claim", Value::Null, body, "main", Some(&format!("{session}/claim")), now) {
        return text_result(json!({"error": e}), true);
    }
    let mut r = json!({"session": session, "role": role, "branch": branch, "claimed": mine, "until": iso(now + minutes * 60_000), "locked_by_others": locked,
                       "resume": hub.digest_for(login)});
    if let Some(d) = duplicate {
        r["duplicate_of"] = json!({"session": d.session, "role": d.role, "since": d.since, "reply": d.reply, "still_working": d.released.is_none() && now < d.until_ms});
        r["note"] = json!("Another session already took this message. Read its reply (or its recent writes) and add only what's missing.");
    }
    text_result(r, false)
}

/// Handle one JSON-RPC message. Returns None for notifications. `made` collects new events to persist.
pub fn handle(hub: &mut Hub, login: &str, key: &KeyPair, msg: &Value, now: i64) -> Option<Value> {
    let id = msg.get("id").cloned();
    let method = msg.get("method").and_then(Value::as_str).unwrap_or("");
    let id = id?; // notifications (no id) get no response
    let ok = |r: Value| Some(json!({"jsonrpc": "2.0", "id": id.clone(), "result": r}));
    let err = |code: i64, m: &str| Some(json!({"jsonrpc": "2.0", "id": id.clone(), "error": {"code": code, "message": m}}));
    match method {
        "initialize" => {
            let asked = msg.pointer("/params/protocolVersion").and_then(Value::as_str).unwrap_or("");
            let v = if PROTOCOL_VERSIONS.contains(&asked) { asked } else { PROTOCOL_VERSIONS[0] };
            ok(json!({"protocolVersion": v, "capabilities": {"tools": {"listChanged": false}},
                      "serverInfo": {"name": "clanstack-hub", "title": "Clanstack hub", "version": format!("{}+{}", env!("CARGO_PKG_VERSION"), BUILD)},
                      "instructions": "Clanstack's shared record. hub_status: counts, chain check and build. read_events: Studio events from a cursor. hub_log: the request log (time, tool, outcome, duration; never contents); use it whenever asked for the hub log. import_studio_events: used by Studio Web's sync. write_event: answers, comments, status changes and items (shared record only; never put private material there). read_private / write_private: the signed-in owner's Private tier, stored apart and encrypted (personal material goes there, never into write_event); Sealed items are never readable. Several chats and AI tools work on this record at once (D-nodach-parallel-guard): before writing anything for a message, call begin_answer with the message, your role and the items you expect to change, pass the returned session to every write_event, and call end_answer with your reply id when done. If begin_answer reports duplicate_of, another session already took the message: read its reply and add only what is missing. Items in locked_by_others take comments only. read_claims shows who is working on what; a new chat calls resume (or reads begin_answer's resume digest) before replying: it lists the rules every chat follows, the open questions and the latest replies from every chat."}))
        }
        "ping" => ok(json!({})),
        "tools/list" => ok(json!({"tools": tools()})),
        "tools/call" => {
            let name = msg.pointer("/params/name").and_then(Value::as_str).unwrap_or("");
            let args = msg.pointer("/params/arguments").cloned().unwrap_or(json!({}));
            ok(call(hub, login, key, name, &args, now))
        }
        "" => err(-32600, "invalid request"),
        _ => err(-32601, "method not found"),
    }
}
