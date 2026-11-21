//! Runnable flows (D-174 C2, Q30 A): steps wired on the Studio canvas, run by the hub itself.
//!
//! The canvas records a flow as Studio events (item null, so item histories stay clean):
//!   flow.node {board, id, step, title, config, x, y, deleted?}   latest event per id wins
//!   flow.wire {board, id, from, to, deleted?}                    latest event per id wins
//! The hub reads those from its own record (never from the browser), plans the steps reachable
//! from the one you pressed Run on, and runs them in order. Each step gets the items its inputs
//! produced. Everything here is pure (no network, clock or secrets) so every rule is tested;
//! the node (hub/src/lib.rs) does the fetching, the AI calls and the writes.
//!
//! Steps: fetch (GET a URL, take a list), filter (keep items meeting every condition), calc (set a
//! field from two values), ai (ask a model per item, store its answer), post (comment each item on
//! a Studio item, e.g. the Income lobby). No step can run code it was sent: conditions and sums are
//! data, not scripts, so a shared flow cannot do anything its steps don't name.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

pub const STEPS: [&str; 5] = ["fetch", "filter", "calc", "ai", "post"];
pub const MAX_STEPS: usize = 20;
pub const MAX_ITEMS: usize = 500;

#[derive(Clone, Debug, PartialEq)]
pub struct Node {
    pub id: String,
    pub step: String,
    pub title: String,
    pub config: Value,
}

#[derive(Default, Debug)]
pub struct Flow {
    pub nodes: BTreeMap<String, Node>,
    /// (from, to), in the order the wires were drawn
    pub wires: Vec<(String, String)>,
}

/// Fold the canvas's flow events (Studio event objects, oldest first) into the current flow on `board`.
pub fn fold<'a>(events: impl IntoIterator<Item = &'a Value>, board: &str) -> Flow {
    let mut nodes: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    let mut wires: BTreeMap<String, (usize, Map<String, Value>)> = BTreeMap::new();
    let mut n = 0;
    for e in events {
        let kind = e.get("kind").and_then(Value::as_str).unwrap_or("");
        if e.get("branch").and_then(Value::as_str).is_some_and(|b| b != "main") {
            continue;
        }
        let Some(b) = e.get("body").and_then(Value::as_object) else { continue };
        if b.get("board").and_then(Value::as_str).unwrap_or("main") != board {
            continue;
        }
        let Some(id) = b.get("id").and_then(Value::as_str) else { continue };
        let target = match kind {
            "flow.node" => nodes.entry(id.to_string()).or_default(),
            "flow.wire" => { n += 1; &mut wires.entry(id.to_string()).or_insert((n, Map::new())).1 }
            _ => continue,
        };
        for (k, v) in b {
            target.insert(k.clone(), v.clone());
        }
    }
    let alive = |m: &Map<String, Value>| !m.get("deleted").and_then(Value::as_bool).unwrap_or(false);
    let mut f = Flow::default();
    for (id, m) in nodes.into_iter().filter(|(_, m)| alive(m)) {
        let step = m.get("step").and_then(Value::as_str).unwrap_or("").to_string();
        if !STEPS.contains(&step.as_str()) {
            continue;
        }
        f.nodes.insert(id.clone(), Node { id, step, title: m.get("title").and_then(Value::as_str).unwrap_or("").to_string(),
            config: m.get("config").cloned().unwrap_or(json!({})) });
    }
    let mut ws: Vec<_> = wires.into_values().filter(|(_, m)| alive(m)).collect();
    ws.sort_by_key(|(i, _)| *i);
    for (_, m) in ws {
        if let (Some(a), Some(b)) = (m.get("from").and_then(Value::as_str), m.get("to").and_then(Value::as_str)) {
            if a != b && f.nodes.contains_key(a) && f.nodes.contains_key(b) && !f.wires.iter().any(|(x, y)| x == a && y == b) {
                f.wires.push((a.to_string(), b.to_string()));
            }
        }
    }
    f
}

/// The steps reachable from `start`, each after all of its reachable inputs. Refuses loops and huge flows.
pub fn plan(f: &Flow, start: &str) -> Result<Vec<String>, String> {
    if !f.nodes.contains_key(start) {
        return Err(format!("{start} isn't a step on this board"));
    }
    let mut reach = HashSet::from([start.to_string()]);
    let mut q = VecDeque::from([start.to_string()]);
    while let Some(x) = q.pop_front() {
        for (a, b) in &f.wires {
            if a == &x && reach.insert(b.clone()) {
                q.push_back(b.clone());
            }
        }
    }
    if reach.len() > MAX_STEPS {
        return Err(format!("{} steps is more than the {MAX_STEPS} one run may take", reach.len()));
    }
    let mut indeg: HashMap<&str, usize> = reach.iter().map(|x| (x.as_str(), 0)).collect();
    for (a, b) in &f.wires {
        if reach.contains(a) && reach.contains(b) {
            *indeg.get_mut(b.as_str()).unwrap() += 1;
        }
    }
    let mut ready: Vec<&str> = vec![start];
    if indeg[start] != 0 {
        return Err("the flow loops back to its first step".into());
    }
    let mut out = vec![];
    while let Some(x) = ready.pop() {
        out.push(x.to_string());
        for (a, b) in &f.wires {
            if a == x && reach.contains(b) {
                let d = indeg.get_mut(b.as_str()).unwrap();
                *d -= 1;
                if *d == 0 {
                    ready.push(b);
                }
            }
        }
    }
    if out.len() != reach.len() {
        return Err("the flow has a loop; a step can't wait for itself".into());
    }
    Ok(out)
}

/// Steps wired into `id`, in drawing order.
pub fn inputs_of(f: &Flow, id: &str) -> Vec<String> {
    f.wires.iter().filter(|(_, b)| b == id).map(|(a, _)| a.clone()).collect()
}

/// `a.b.0.c` into a JSON value; Null when missing. An empty path is the value itself.
pub fn get_path(v: &Value, path: &str) -> Value {
    let mut cur = v;
    for part in path.split('.').filter(|p| !p.is_empty()) {
        cur = match cur {
            Value::Object(m) => match m.get(part) { Some(x) => x, None => return Value::Null },
            Value::Array(a) => match part.parse::<usize>().ok().and_then(|i| a.get(i)) { Some(x) => x, None => return Value::Null },
            _ => return Value::Null,
        };
    }
    cur.clone()
}

/// A fetched body (or anything) as a list of items, capped at MAX_ITEMS.
pub fn as_items(v: Value, list_path: &str) -> Vec<Value> {
    match get_path(&v, list_path) {
        Value::Array(a) => a.into_iter().take(MAX_ITEMS).collect(),
        Value::Null => vec![],
        x => vec![x],
    }
}

fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().trim_start_matches('£').replace(',', "").parse().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        x => x.to_string(),
    }
}

/// One condition `{field, op, value}`. ops: exists, missing, =, !=, >, >=, <, <=, contains, not_contains (case-insensitive).
pub fn holds(item: &Value, cond: &Value) -> Result<bool, String> {
    let field = cond.get("field").and_then(Value::as_str).ok_or("a condition needs a field")?;
    let op = cond.get("op").and_then(Value::as_str).unwrap_or("exists");
    let have = get_path(item, field);
    let want = cond.get("value").cloned().unwrap_or(Value::Null);
    let present = !matches!(have, Value::Null) && !(have.is_string() && have.as_str() == Some(""));
    Ok(match op {
        "exists" => present,
        "missing" => !present,
        "=" | "!=" => {
            let eq = match (num(&have), num(&want)) {
                (Some(a), Some(b)) if !have.is_string() || !want.is_string() => (a - b).abs() < 1e-9,
                _ => text(&have).eq_ignore_ascii_case(&text(&want)),
            };
            if op == "=" { eq } else { !eq }
        }
        ">" | ">=" | "<" | "<=" => match (num(&have), num(&want)) {
            (Some(a), Some(b)) => match op { ">" => a > b, ">=" => a >= b, "<" => a < b, _ => a <= b },
            _ => false,
        },
        "contains" | "not_contains" => {
            let c = text(&have).to_lowercase().contains(&text(&want).to_lowercase());
            if op == "contains" { c } else { !c }
        }
        other => return Err(format!("unknown condition {other:?}")),
    })
}

/// Keep the items meeting every condition in `config.all` (and none in `config.none`).
pub fn filter(items: Vec<Value>, config: &Value) -> Result<Vec<Value>, String> {
    let all: Vec<Value> = config.get("all").and_then(Value::as_array).cloned().unwrap_or_default();
    let none: Vec<Value> = config.get("none").and_then(Value::as_array).cloned().unwrap_or_default();
    let mut out = vec![];
    for it in items {
        let mut keep = true;
        for c in &all { if !holds(&it, c)? { keep = false; break; } }
        if keep { for c in &none { if holds(&it, c)? { keep = false; break; } } }
        if keep { out.push(it); }
    }
    Ok(out)
}

/// `config.sets`: [{set, a, op, b, round?}]. a and b are field paths or numbers; op is + - * / max min.
/// A sum that can't be made (missing field, divide by zero) sets null rather than guessing.
pub fn calc(items: Vec<Value>, config: &Value) -> Result<Vec<Value>, String> {
    let sets: Vec<Value> = config.get("sets").and_then(Value::as_array).cloned().unwrap_or_default();
    let operand = |it: &Value, x: &Value| -> Option<f64> {
        match x { Value::String(s) => num(&get_path(it, s)).or_else(|| s.parse().ok()), v => num(v) }
    };
    items.into_iter().map(|mut it| {
        for s in &sets {
            let set = s.get("set").and_then(Value::as_str).ok_or("a sum needs a field to set")?;
            let a = operand(&it, s.get("a").unwrap_or(&Value::Null));
            let b = operand(&it, s.get("b").unwrap_or(&json!(0)));
            let op = s.get("op").and_then(Value::as_str).unwrap_or("+");
            let r = match (a, b) {
                (Some(a), Some(b)) => match op {
                    "+" => Some(a + b), "-" => Some(a - b), "*" => Some(a * b),
                    "/" => (b != 0.0).then(|| a / b), "max" => Some(a.max(b)), "min" => Some(a.min(b)),
                    o => return Err(format!("unknown sum {o:?}")),
                },
                _ => None,
            };
            let places = s.get("round").and_then(Value::as_u64).unwrap_or(2).min(6) as i32;
            let r = r.map(|x| { let p = 10f64.powi(places); (x * p).round() / p });
            if let Value::Object(m) = &mut it {
                m.insert(set.to_string(), r.map(|x| json!(x)).unwrap_or(Value::Null));
            }
        }
        Ok(it)
    }).collect()
}

/// `{{field}}` / `{{a.b}}` from the item; missing fields render empty.
pub fn render(template: &str, item: &Value) -> String {
    let mut out = String::new();
    let mut rest = template;
    while let Some(i) = rest.find("{{") {
        out.push_str(&rest[..i]);
        match rest[i + 2..].find("}}") {
            Some(j) => { out.push_str(&text(&get_path(item, rest[i + 2..i + 2 + j].trim()))); rest = &rest[i + 4 + j..]; }
            None => { out.push_str(&rest[i..]); rest = ""; }
        }
    }
    out.push_str(rest);
    out
}

/// An Ask AI step may name private values to use in its prompt (18 Nov): `vars: {"home": "HOME_AREA"}` reads
/// HOME_AREA from the run owner's vault (V1 d) and renders it as `{{vars.home}}`. The value is used for the prompt only and
/// never written into the items, the run record or the template, so a personal detail stays out of shared data.
pub fn with_vars(item: &Value, vars: &serde_json::Map<String, Value>) -> Value {
    let mut view = item.clone();
    if let Some(m) = view.as_object_mut() { m.insert("vars".into(), Value::Object(vars.clone())); }
    view
}

/// Secrets a step may name: letters, digits and _, read from the hub's `NODE_SECRET_<NAME>` secrets.
/// Anything else is refused, so a flow can never read the hub's own secrets.
pub fn secret_var(name: &str) -> Option<String> {
    let n = name.trim().to_ascii_uppercase();
    (!n.is_empty() && n.len() <= 40 && n.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')).then(|| format!("NODE_SECRET_{n}"))
}

/// Only https, and never the hub's own hosts (a flow can't call its own API with someone else's rights).
/// The hub answers on its workers.dev addresses and on nodach.com and every subdomain (studio., staging., …).
/// Whether an AI step should wait and try again (then the lighter model) after this answer status.
/// Busy or briefly unreachable models: 429, 500 and 502-504, Cloudflare's 520-524 (524 = the origin took too long, seen on the
/// 21 Nov 18:00 job flow), Google's 529 overload, 408, and 0 for a network error before any status. Anything else
/// (400 bad request, 401/403 key, 404 model) is a real fault and stops the flow at once.
pub fn ai_retryable(status: u16) -> bool {
    matches!(status, 0 | 408 | 429 | 500 | 502..=504 | 520..=524 | 529)
}

pub fn url_allowed(url: &str) -> bool {
    let Some(rest) = url.strip_prefix("https://") else { return false };
    let host = rest.split(['/', '?', '#']).next().unwrap_or("").to_ascii_lowercase();
    let host = host.split(':').next().unwrap_or("").trim_end_matches('.');
    let own = host.ends_with(".workers.dev") || host == "nodach.com" || host.ends_with(".nodach.com");
    !host.is_empty() && !rest.split(['/', '?', '#']).next().unwrap_or("").contains('@') && !own && host != "localhost" && !host.starts_with("127.")
}

pub fn base64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::new();
    for c in bytes.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        o.push(T[(n >> 18) as usize & 63] as char);
        o.push(T[(n >> 12) as usize & 63] as char);
        o.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        o.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    o
}

/// The flow runner's signing seed, derived from a hub secret with its own label.
pub fn runner_seed(secret: &str) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(b"nodach-flow-runner-v1\0");
    h.update(secret.trim().as_bytes());
    h.finalize().into()
}

/// A short look at a step's output for the run record: the first few items, long text trimmed.
pub fn sample(items: &[Value], n: usize) -> Value {
    fn trim(v: &Value) -> Value {
        match v {
            Value::String(s) if s.chars().count() > 160 => json!(format!("{}…", s.chars().take(160).collect::<String>())),
            Value::Object(m) => Value::Object(m.iter().take(24).map(|(k, v)| (k.clone(), trim(v))).collect()),
            Value::Array(a) => Value::Array(a.iter().take(5).map(trim).collect()),
            x => x.clone(),
        }
    }
    Value::Array(items.iter().take(n).map(trim).collect())
}

// ------------------------------------------------------------------ schedules (S3, Q32: executive call)
// A start step (one with nothing wired into it) may carry `schedule`: UK hours, e.g. [7, 18] or "7, 18".
// The hub wakes hourly; each due start runs once per slot (UK date + hour). The slot is written into the run
// record, so "already ran this hour" is read from history, never from hidden state.

/// Days since 1970-01-01 → (year, month, day). (Howard Hinnant's civil_from_days.)
fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}
/// (year, month, day) → days since 1970-01-01.
fn days_from(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (i64::from(m) + 9) % 12;
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1;
    era * 146_097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719_468
}
/// Last Sunday of a 31-day month, as days since 1970-01-01 (1970-01-01 was a Thursday).
fn last_sunday(y: i64, m: u32) -> i64 {
    let d = days_from(y, m, 31);
    d - (d + 4).rem_euclid(7)
}
/// UK hour and slot ("YYYY-MM-DDTHH", UK time) for a UTC instant in ms. British Summer Time runs from
/// 01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday of October.
pub fn uk_slot(ms: i64) -> (u32, String) {
    let hour_ms = 3_600_000;
    let (y, _, _) = civil(ms.div_euclid(86_400_000));
    let start = last_sunday(y, 3) * 86_400_000 + hour_ms;
    let end = last_sunday(y, 10) * 86_400_000 + hour_ms;
    let local = if (start..end).contains(&ms) { ms + hour_ms } else { ms };
    let days = local.div_euclid(86_400_000);
    let h = (local.rem_euclid(86_400_000) / hour_ms) as u32;
    let (y, m, d) = civil(days);
    (h, format!("{y:04}-{m:02}-{d:02}T{h:02}"))
}

/// The UK hours in a step's `schedule` (numbers or "7, 18"); out-of-range values are dropped.
pub fn schedule_hours(config: &Value) -> Vec<u32> {
    let raw: Vec<Value> = match config.get("schedule") {
        Some(Value::Array(a)) => a.clone(),
        Some(Value::String(s)) => s.split([',', ' ']).filter(|t| !t.is_empty()).map(|t| json!(t)).collect(),
        Some(Value::Number(n)) => vec![Value::Number(n.clone())],
        _ => vec![],
    };
    let mut hs: Vec<u32> = raw.iter().filter_map(|v| v.as_u64().or_else(|| v.as_str().and_then(|s| s.trim().parse().ok())))
        .filter(|h| *h < 24).map(|h| h as u32).collect();
    hs.sort_unstable();
    hs.dedup();
    hs
}

/// Boards that have flow steps, in first-seen order.
pub fn boards<'a>(events: impl IntoIterator<Item = &'a Value>) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    for e in events {
        if e.get("kind").and_then(Value::as_str) != Some("flow.node") { continue; }
        let b = e.pointer("/body/board").and_then(Value::as_str).unwrap_or("main").to_string();
        if !out.contains(&b) { out.push(b); }
    }
    out
}

/// (board, start step, slot) for every scheduled start that is due at `now_ms` and hasn't run in this slot.
pub fn due(events: &[Value], now_ms: i64) -> Vec<(String, String, String)> {
    let (hour, slot) = uk_slot(now_ms);
    let ran = |board: &str, start: &str| events.iter().any(|e| e.get("kind").and_then(Value::as_str) == Some("flow.run")
        && e.pointer("/body/slot").and_then(Value::as_str) == Some(slot.as_str())
        && e.pointer("/body/start").and_then(Value::as_str) == Some(start)
        && e.pointer("/body/board").and_then(Value::as_str).unwrap_or("main") == board);
    let mut out = vec![];
    for board in boards(events) {
        let f = fold(events, &board);
        for (id, n) in &f.nodes {
            if inputs_of(&f, id).is_empty() && schedule_hours(&n.config).contains(&hour) && !ran(&board, id) {
                out.push((board.clone(), id.clone(), slot.clone()));
            }
        }
    }
    out
}

/// Keys already posted to `item` by a post step with "only new" on (comments carry `key`).
pub fn posted_keys<'a>(events: impl IntoIterator<Item = &'a Value>, item: &str) -> std::collections::HashSet<String> {
    events.into_iter()
        .filter(|e| e.get("kind").and_then(Value::as_str) == Some("comment") && e.get("item").and_then(Value::as_str) == Some(item))
        .filter_map(|e| e.pointer("/body/key").and_then(Value::as_str).map(str::to_string))
        .collect()
}
