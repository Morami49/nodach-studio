//! Money v1 (FUN-1 v2, BQ-1 step 4, 22 Nov 2026): a ledger inside each teamspace, fee rules as a versioned node, and
//! Stripe in TEST MODE only.
//!
//! The ledger is the record: nothing here is a balance that can be overwritten. Every line is an event, and the totals
//! at any past point are the events up to then (D-nodach-trail). Kinds (Studio-shaped; `item` is the invoice id, or the
//! teamspace id for the two teamspace settings):
//!   invoice.create   {id, teamspace, client, lines:[{desc, qty, unit}], total, currency:"GBP", co_vendor?}   a member
//!   invoice.checkout {invoice, teamspace, provider:"stripe", session, url, application_fee, mode:"test"}     the hub
//!   payment.received {id, invoice, teamspace, amount, method, provider_ref, recorded_by?}                    the hub
//!   fee.charged      {payment, invoice, teamspace, line, rate_bps, amount, payer, basis, estimated?, waived?} the hub
//!   refund           {id, payment, invoice, teamspace, amount, provider_ref}                                 the hub
//!   money.charging   {teamspace, on, note?}      a teamspace owner: "the first test passed, start charging"
//!   money.account    {teamspace, provider:"stripe", account:"acct_…"}   a teamspace owner: the Stripe connected account
//! All amounts are whole pence (GBP). `method` is card | bank | recorded_bank_transfer | own_provider.
//!
//! Who may write what (checked before anything is stored, in `decide`, and again in `fold`):
//!   - invoice.create needs owner or member rights in its teamspace; money.charging / money.account need owner;
//!   - payment.received, fee.charged, refund and invoice.checkout come only from the hub itself: from Stripe's signed
//!     webhook, or from "Record a payment" (a teamspace owner, through the hub's own route). Any copy that arrives
//!     through Studio, an import or another author is refused, and if one is on the record anyway it is a finding.
//!   - members and owners of a teamspace read its money; guests don't.
//!
//! Fee rules are a node, `FEE-rules` (type setting, `value` JSON), versioned like PLAN-limits: only a hub owner's edits
//! count. Defaults (Nowed's decisions, Q-take-rate):
//!   - through Nodach (card, bank via Stripe): 4.5% of each payment, no cap. Nodach pays Stripe's fee out of that 4.5%,
//!     and the Stripe cost is its own ledger line (`line: provider_cost`, payer nodach), so Nodach's net is visible;
//!   - co-vendor sales: no processing fee;
//!   - teamspace overrides (e.g. a managed-service contract): `overrides.<teamspace>.rate_bps` and/or `.plan`;
//!   - own payment provider: a surcharge, PLACEHOLDER 1.00% for Nowed to set;
//!   - recorded bank transfers: billed per the teamspace's plan, PLACEHOLDER 0% on plan "free" for Nowed to set.
//! Rounding: each fee is rounded to the nearest penny, halves up, once per payment.
//! Charging starts only once the teamspace's owner says its first test passed (`money.charging {on:true}`). Before that
//! the fee line is still written, at £0.00 with `waived`, so the ledger shows what would have been charged.
//!
//! TODO(BQ-1 later step): machine-capacity top-ups (usage::REFUSAL says "a top-up will be available soon") are not
//! part of this step; they will be invoices from Nodach to a teamspace, on this same ledger.
//!
//! Rollback: revert the PR. The events stay on the record (append-only) and simply stop meaning anything.
use crate::teamspaces::{self, Role};
use crate::profiles;
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};

pub const KINDS: [&str; 7] = ["invoice.create", "invoice.checkout", "payment.received", "fee.charged", "refund", "money.charging", "money.account"];
/// Written only by the hub (its flow runner key), never through Studio or an import.
pub const HUB_ONLY: [&str; 4] = ["invoice.checkout", "payment.received", "fee.charged", "refund"];
pub const RULES_ID: &str = "FEE-rules";
pub const RUNNER_ID: &str = crate::usage::RUNNER_ID;
/// The hub's Stripe secrets. TEST MODE ONLY: a key that isn't sk_test_/rk_test_ is refused.
pub const STRIPE_SECRET: &str = "NODE_SECRET_STRIPE_TEST";
pub const STRIPE_WEBHOOK_SECRET: &str = "NODE_SECRET_STRIPE_TEST_WEBHOOK";
pub const NO_KEY: &str = "Stripe test mode isn't set up yet (NODE_SECRET_STRIPE_TEST): invoices, recorded payments and the ledger still work; card links don't.";
pub const NOT_TEST: &str = "Only Stripe test mode is allowed in this build: NODE_SECRET_STRIPE_TEST must be a test key (sk_test_… or rk_test_…).";
pub const WAIVED: &str = "charging starts once this teamspace's first test has passed (its owner sets that)";
/// Stripe's signed timestamp may be at most this old (Stripe's own default tolerance).
pub const SIGNATURE_TOLERANCE_S: i64 = 300;
pub const CURRENCY: &str = "GBP";
pub const MAX_LINES: usize = 50;
/// £1,000,000 in pence: anything larger on one invoice is a typo in test mode.
pub const MAX_TOTAL: i64 = 100_000_000;

/// How a payment arrived.
#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Method { Card, Bank, RecordedBankTransfer, OwnProvider }

impl Method {
    pub fn parse(s: &str) -> Result<Method, String> {
        match s.trim() {
            "card" => Ok(Method::Card),
            "bank" => Ok(Method::Bank),
            "recorded_bank_transfer" => Ok(Method::RecordedBankTransfer),
            "own_provider" => Ok(Method::OwnProvider),
            x => Err(format!("method must be card, bank, recorded_bank_transfer or own_provider (got {x:?})")),
        }
    }
    pub fn key(self) -> &'static str {
        match self { Method::Card => "card", Method::Bank => "bank", Method::RecordedBankTransfer => "recorded_bank_transfer", Method::OwnProvider => "own_provider" }
    }
    /// Card and bank payments go through Nodach's Stripe account; Nodach pays Stripe's fee on them.
    pub fn through_nodach(self) -> bool { matches!(self, Method::Card | Method::Bank) }
}

#[derive(Serialize, Clone, Debug, PartialEq, Default)]
pub struct Override {
    /// replaces the 4.5% for payments through Nodach in this teamspace
    pub rate_bps: Option<u32>,
    /// the plan its recorded bank transfers are billed on
    pub plan: Option<String>,
    pub note: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct FeeRules {
    /// through Nodach: 450 = 4.5% (Nodach pays Stripe's fee out of it). No cap.
    pub nodach_bps: u32,
    /// co-vendor sales: 0 = no processing fee
    pub co_vendor_bps: u32,
    /// own payment provider surcharge (PLACEHOLDER)
    pub own_provider_bps: u32,
    /// recorded bank transfers, by plan (PLACEHOLDER)
    pub plans: BTreeMap<String, u32>,
    pub default_plan: String,
    /// Stripe's fee when Stripe hasn't told us the real one yet (UK standard cards, an estimate, marked as such)
    pub stripe_estimate_bps: u32,
    pub stripe_estimate_fixed: i64,
    /// teamspace id → override (managed-service contracts and the like)
    pub overrides: BTreeMap<String, Override>,
    /// which numbers are still placeholders for Nowed
    pub placeholders: Vec<String>,
    pub node: Option<String>,
    pub version: usize,
    pub updated: Option<String>,
    pub doc_id: Option<String>,
}

impl Default for FeeRules {
    fn default() -> Self {
        FeeRules { nodach_bps: 450, co_vendor_bps: 0, own_provider_bps: 100, plans: BTreeMap::from([("free".to_string(), 0)]),
                   default_plan: "free".into(), stripe_estimate_bps: 150, stripe_estimate_fixed: 20, overrides: BTreeMap::new(),
                   placeholders: vec!["own_provider_bps".into(), "plans".into()], node: None, version: 0, updated: None, doc_id: None }
    }
}

impl FeeRules {
    /// The value a new FEE-rules node starts with (the defaults, placeholders marked).
    pub fn default_value() -> Value {
        json!({"nodach_bps": 450, "co_vendor_bps": 0, "own_provider_bps": 100, "plans": {"free": 0}, "default_plan": "free",
               "stripe_estimate_bps": 150, "stripe_estimate_fixed": 20, "overrides": {}, "placeholders": ["own_provider_bps", "plans"]})
    }

    fn parse(v: &Value, prev: &FeeRules) -> Result<FeeRules, String> {
        let obj = match v {
            Value::String(t) => serde_json::from_str::<Value>(t).map_err(|_| "the fee rules value isn't JSON".to_string())?,
            o => o.clone(),
        };
        if !obj.is_object() { return Err("the fee rules value must be an object".into()); }
        let bps = |k: &str, d: u32| -> Result<u32, String> {
            match obj.get(k) {
                None | Some(Value::Null) => Ok(d),
                Some(x) => x.as_u64().filter(|n| *n <= 10_000).map(|n| n as u32).ok_or(format!("{k} must be basis points, 0 to 10000")),
            }
        };
        let mut r = FeeRules {
            nodach_bps: bps("nodach_bps", prev.nodach_bps)?,
            co_vendor_bps: bps("co_vendor_bps", prev.co_vendor_bps)?,
            own_provider_bps: bps("own_provider_bps", prev.own_provider_bps)?,
            stripe_estimate_bps: bps("stripe_estimate_bps", prev.stripe_estimate_bps)?,
            ..prev.clone()
        };
        if let Some(x) = obj.get("stripe_estimate_fixed").filter(|x| !x.is_null()) {
            r.stripe_estimate_fixed = x.as_i64().filter(|n| (0..=1000).contains(n)).ok_or("stripe_estimate_fixed must be pence, 0 to 1000")?;
        }
        if let Some(p) = obj.get("plans").filter(|x| !x.is_null()) {
            let m = p.as_object().ok_or("plans must be {plan: basis points}")?;
            let mut plans = BTreeMap::new();
            for (k, v) in m {
                plans.insert(k.clone(), v.as_u64().filter(|n| *n <= 10_000).ok_or(format!("plan {k} must be basis points, 0 to 10000"))? as u32);
            }
            r.plans = plans;
        }
        if let Some(d) = obj.get("default_plan").and_then(Value::as_str) { r.default_plan = d.trim().to_string(); }
        if let Some(o) = obj.get("overrides").filter(|x| !x.is_null()) {
            let m = o.as_object().ok_or("overrides must be {teamspace: {rate_bps?, plan?, note?}}")?;
            let mut out = BTreeMap::new();
            for (ts, v) in m {
                let rate = match v.get("rate_bps") {
                    None | Some(Value::Null) => None,
                    Some(x) => Some(x.as_u64().filter(|n| *n <= 10_000).ok_or(format!("overrides.{ts}.rate_bps must be basis points, 0 to 10000"))? as u32),
                };
                out.insert(ts.clone(), Override { rate_bps: rate, plan: v.get("plan").and_then(Value::as_str).map(str::to_string),
                    note: v.get("note").and_then(Value::as_str).unwrap_or("").chars().take(200).collect() });
            }
            r.overrides = out;
        }
        if let Some(p) = obj.get("placeholders").and_then(Value::as_array) {
            r.placeholders = p.iter().filter_map(Value::as_str).map(str::to_string).collect();
        }
        if !r.plans.contains_key(&r.default_plan) { return Err(format!("default_plan {:?} isn't one of the plans", r.default_plan)); }
        Ok(r)
    }

    fn plan_of(&self, teamspace: &str) -> String {
        self.overrides.get(teamspace).and_then(|o| o.plan.clone()).filter(|p| self.plans.contains_key(p)).unwrap_or_else(|| self.default_plan.clone())
    }
}

/// `amount` × `bps` / 10000, rounded to the nearest penny, halves up. Negative amounts round symmetrically.
pub fn pct_of(amount: i64, bps: u32) -> i64 {
    let a = amount.unsigned_abs() as u128 * bps as u128;
    let r = ((a + 5_000) / 10_000) as i64;
    if amount < 0 { -r } else { r }
}

/// Stripe's fee, estimated (only used until Stripe reports the real one).
pub fn stripe_estimate(rules: &FeeRules, amount: i64) -> i64 {
    if amount <= 0 { return 0; }
    pct_of(amount, rules.stripe_estimate_bps) + rules.stripe_estimate_fixed
}

/// What one payment costs whom.
#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Quote {
    pub basis: String,
    pub rate_bps: u32,
    /// what Nodach charges the vendor (0 while charging hasn't started)
    pub platform_fee: i64,
    /// what Stripe charges, paid by Nodach (0 when Stripe isn't involved)
    pub provider_cost: i64,
    pub provider_cost_estimated: bool,
    /// platform_fee − provider_cost (can be negative on small payments: no cap, and no minimum either)
    pub nodach_net: i64,
    /// the payment less Nodach's fee
    pub vendor_net: i64,
    pub waived: Option<String>,
}

/// The fee maths for one payment of `amount` pence in `teamspace`.
/// `provider_cost` is Stripe's own fee when known (from the balance transaction); None = estimate it.
pub fn quote(rules: &FeeRules, teamspace: &str, method: Method, amount: i64, co_vendor: bool, charging_on: bool, provider_cost: Option<i64>) -> Quote {
    let ov = rules.overrides.get(teamspace);
    let (rate_bps, basis) = if co_vendor {
        (rules.co_vendor_bps, "co-vendor sale: no processing fee".to_string())
    } else {
        match method {
            Method::Card | Method::Bank => match ov.and_then(|o| o.rate_bps) {
                Some(r) => (r, format!("teamspace override{}", ov.map(|o| if o.note.is_empty() { String::new() } else { format!(": {}", o.note) }).unwrap_or_default())),
                None => (rules.nodach_bps, "through Nodach (Nodach pays Stripe's fee out of it)".to_string()),
            },
            Method::OwnProvider => (rules.own_provider_bps, format!("own payment provider surcharge{}", if rules.placeholders.iter().any(|p| p == "own_provider_bps") { " (placeholder rate)" } else { "" })),
            Method::RecordedBankTransfer => {
                let plan = rules.plan_of(teamspace);
                (rules.plans.get(&plan).copied().unwrap_or(0), format!("recorded bank transfer, plan {plan}{}", if rules.placeholders.iter().any(|p| p == "plans") { " (placeholder rate)" } else { "" }))
            }
        }
    };
    let full = pct_of(amount, rate_bps);
    let (platform_fee, waived) = if charging_on { (full, None) } else { (0, Some(WAIVED.to_string())) };
    let (provider_cost, est) = if method.through_nodach() {
        match provider_cost { Some(c) => (c, false), None => (stripe_estimate(rules, amount), true) }
    } else { (0, false) };
    Quote { basis, rate_bps, platform_fee, provider_cost, provider_cost_estimated: est, nodach_net: platform_fee - provider_cost,
            vendor_net: amount - platform_fee, waived }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Line { pub desc: String, pub qty: i64, pub unit: i64 }

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Invoice {
    pub id: String,
    pub teamspace: String,
    pub client: String,
    pub lines: Vec<Line>,
    pub total: i64,
    pub currency: String,
    pub co_vendor: bool,
    pub created: String,
    pub author: String,
    pub checkout_url: Option<String>,
    pub paid: i64,
    pub refunded: i64,
    pub status: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Payment {
    pub id: String,
    pub invoice: String,
    pub teamspace: String,
    pub amount: i64,
    pub method: Method,
    pub provider_ref: String,
    pub ts: String,
    pub recorded_by: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct FeeLine {
    pub payment: String,
    pub invoice: String,
    pub teamspace: String,
    /// platform_fee (Nodach's fee, paid by the vendor) or provider_cost (Stripe's fee, paid by Nodach)
    pub line: String,
    pub rate_bps: u32,
    pub amount: i64,
    pub payer: String,
    pub basis: String,
    pub estimated: bool,
    pub waived: Option<String>,
    pub ts: String,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Refund { pub id: String, pub payment: String, pub invoice: String, pub teamspace: String, pub amount: i64, pub provider_ref: String, pub ts: String }

#[derive(Serialize, Clone, Debug, PartialEq, Default)]
pub struct Charging { pub on: bool, pub since: Option<String>, pub by: Option<String>, pub note: String }

#[derive(Serialize, Clone, Debug, PartialEq, Default)]
pub struct Totals {
    pub invoiced: i64,
    pub received: i64,
    pub refunded: i64,
    /// Nodach's fees charged to the vendor
    pub platform_fees: i64,
    /// Stripe's fees, paid by Nodach
    pub provider_costs: i64,
    pub nodach_net: i64,
    /// received − refunded − platform fees
    pub vendor_net: i64,
    pub outstanding: i64,
}

#[derive(Default, Clone, Debug)]
pub struct State {
    pub rules: FeeRules,
    pub invoices: BTreeMap<String, Invoice>,
    pub payments: BTreeMap<String, Payment>,
    pub fees: Vec<FeeLine>,
    pub refunds: Vec<Refund>,
    pub charging: BTreeMap<String, Charging>,
    pub accounts: BTreeMap<String, String>,
    /// every money event in record order, per teamspace: (doc id, the event)
    pub lines: BTreeMap<String, Vec<(String, Value)>>,
    pub findings: Vec<profiles::Finding>,
    by_ref: HashMap<String, String>,
    teams: teamspaces::State,
    hub_owners: Vec<String>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).map(str::trim).unwrap_or("")
}
fn lc(x: &str) -> String { x.trim().to_ascii_lowercase() }
fn author_login(ev: &Value) -> Option<String> {
    ev.pointer("/author/id").and_then(Value::as_str).and_then(|a| a.strip_prefix("github:")).map(lc).filter(|l| !l.is_empty())
}
fn author_id(ev: &Value) -> &str { ev.pointer("/author/id").and_then(Value::as_str).unwrap_or("") }

pub fn is_money_kind(kind: &str) -> bool { KINDS.contains(&kind) }

/// The teamspace a money event belongs to.
pub fn teamspace_of(ev: &Value) -> String {
    let b = ev.get("body").cloned().unwrap_or(Value::Null);
    match s(&b, "teamspace") { "" => s(ev, "item").to_string(), t => t.to_string() }
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 80 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b))
}

/// The id a payment gets from its provider reference (the same on every node, so a retried webhook is a no-op).
pub fn payment_id(provider_ref: &str) -> String {
    format!("PAY-{}", &hex::encode(Sha256::digest(provider_ref.as_bytes()))[..20])
}

/// Check an invoice body and give back its lines and total. The total must equal the sum of its lines.
pub fn parse_invoice(b: &Value) -> Result<(Vec<Line>, i64), String> {
    if !valid_id(s(b, "id")) { return Err("an invoice id is 1-80 letters, digits or - _ . :".into()); }
    if s(b, "client").is_empty() { return Err("name the client the invoice is for".into()); }
    if !s(b, "currency").eq_ignore_ascii_case(CURRENCY) { return Err("invoices are in GBP for now (currency: \"GBP\")".into()); }
    let ls = b.get("lines").and_then(Value::as_array).ok_or("an invoice needs lines: [{desc, qty, unit}]")?;
    if ls.is_empty() || ls.len() > MAX_LINES { return Err(format!("an invoice has 1 to {MAX_LINES} lines")); }
    let mut lines = vec![];
    let mut sum: i64 = 0;
    for (i, l) in ls.iter().enumerate() {
        let desc: String = s(l, "desc").chars().take(200).collect();
        if desc.is_empty() { return Err(format!("line {} needs a description", i + 1)); }
        let qty = l.get("qty").and_then(Value::as_i64).filter(|q| (1..=10_000).contains(q)).ok_or(format!("line {}: qty must be a whole number from 1 to 10000", i + 1))?;
        let unit = l.get("unit").and_then(Value::as_i64).filter(|u| (0..=MAX_TOTAL).contains(u)).ok_or(format!("line {}: unit must be whole pence, 0 or more", i + 1))?;
        sum = sum.saturating_add(qty.saturating_mul(unit));
        lines.push(Line { desc, qty, unit });
    }
    let total = b.get("total").and_then(Value::as_i64).ok_or("an invoice needs total (pence)")?;
    if total != sum { return Err(format!("the total ({total}p) isn't the sum of the lines ({sum}p)")); }
    if total <= 0 || total > MAX_TOTAL { return Err("an invoice total is more than £0 and at most £1,000,000".into()); }
    Ok((lines, total))
}

/// The money verdict on an event, judged on its own author: None when it isn't a money event.
pub fn decide(teams: &teamspaces::State, ev: &Value) -> Option<Result<(), String>> {
    let kind = s(ev, "kind");
    if !is_money_kind(kind) { return None; }
    if HUB_ONLY.contains(&kind) {
        return Some(Err(format!("{kind} is written only by the hub (from Stripe's signed webhook, or Record a payment in the Money view). Nothing was saved.")));
    }
    let Some(login) = author_login(ev) else { return Some(Err("money events need a signed-in author".into())) };
    let ts = teamspace_of(ev);
    if !teams.teamspaces.contains_key(&ts) { return Some(Err(format!("teamspace {ts} doesn't exist. Nothing was saved."))); }
    let role = teams.role_of(&login, &ts);
    Some(match kind {
        "invoice.create" => {
            if !matches!(role, Some(Role::Owner | Role::Member)) {
                return Some(Err(format!("only members and owners of {ts} create invoices there. Nothing was saved.")));
            }
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            if s(ev, "item") != s(&b, "id") { return Some(Err("an invoice.create's item must be its own id".into())); }
            parse_invoice(&b).map(|_| ()).map_err(|e| format!("{e}. Nothing was saved."))
        }
        _ => {
            if role != Some(Role::Owner) { return Some(Err(format!("only an owner of {ts} changes its money settings. Nothing was saved."))); }
            if kind == "money.account" {
                let b = ev.get("body").cloned().unwrap_or(Value::Null);
                if !valid_account(s(&b, "account")) { return Some(Err("a Stripe connected account looks like acct_ followed by letters and digits".into())); }
            }
            Ok(())
        }
    })
}

pub fn valid_account(a: &str) -> bool {
    a.len() > 5 && a.len() <= 64 && a.starts_with("acct_") && a[5..].bytes().all(|b| b.is_ascii_alphanumeric())
}

/// May `login` read this money event? Members and owners of its teamspace; never guests or outsiders.
pub fn visible(teams: &teamspaces::State, login: &str, ev: &Value) -> bool {
    matches!(teams.role_of(login, &teamspace_of(ev)), Some(Role::Owner | Role::Member))
}

/// A Stripe secret key may be used only if it is a test key.
pub fn test_key_ok(key: &str) -> bool {
    let k = key.trim();
    (k.starts_with("sk_test_") || k.starts_with("rk_test_")) && k.len() > 12
}

impl State {
    /// Fold Studio-shaped events in record order. `via_hub` is true for facts the hub signed itself (`studio.write`):
    /// only those may carry payments, fees, refunds and checkout links, and only from the hub's runner.
    pub fn fold<'a>(teams: teamspaces::State, hub_owners: &[String], docs: impl IntoIterator<Item = (&'a str, &'a Value, bool)>) -> Self {
        let mut st = State { teams, hub_owners: hub_owners.iter().map(|o| lc(o)).filter(|o| !o.is_empty()).collect(), ..State::default() };
        for (doc_id, ev, via_hub) in docs { st.observe(doc_id, ev, via_hub); }
        st
    }

    fn finding(&mut self, doc_id: &str, kind: &str, reason: impl Into<String>) {
        self.findings.push(profiles::Finding { doc_id: doc_id.into(), kind: kind.into(), reason: reason.into() });
    }

    fn hub_owner(&self, ev: &Value) -> bool {
        author_login(ev).is_some_and(|l| self.hub_owners.is_empty() || self.hub_owners.contains(&l))
    }

    fn observe(&mut self, doc_id: &str, ev: &Value, via_hub: bool) {
        let kind = s(ev, "kind");
        let b = ev.get("body").cloned().unwrap_or(Value::Null);
        // the fee rules node: only a hub owner's versions count
        let subject = if kind == "item.create" { s(&b, "id") } else { s(ev, "item") };
        if subject == RULES_ID && matches!(kind, "item.create" | "item.edit") {
            if !self.hub_owner(ev) { if b.get("value").is_some() { self.finding(doc_id, kind, "only a hub owner changes the fee rules"); } return; }
            if kind == "item.create" && self.rules.node.is_none() { self.rules.node = Some(RULES_ID.into()); }
            let Some(v) = b.get("value") else { return };
            match FeeRules::parse(v, &self.rules) {
                Ok(mut r) => { r.node = Some(RULES_ID.into()); r.version += 1; r.updated = Some(s(ev, "ts").into()); r.doc_id = Some(doc_id.into()); self.rules = r; }
                Err(e) => self.finding(doc_id, kind, e),
            }
            return;
        }
        if !is_money_kind(kind) { return; }
        let ts_id = teamspace_of(ev);
        let at = s(ev, "ts").to_string();
        if HUB_ONLY.contains(&kind) && !(via_hub && author_id(ev) == RUNNER_ID) {
            self.finding(doc_id, kind, "not written by the hub: ignored");
            return;
        }
        let ok = match kind {
            "invoice.create" => {
                let id = s(&b, "id").to_string();
                let member = author_login(ev).is_some_and(|l| matches!(self.teams.role_of(&l, &ts_id), Some(Role::Owner | Role::Member)));
                if !member { self.finding(doc_id, kind, format!("not a member of {ts_id}")); false }
                else if self.invoices.contains_key(&id) { self.finding(doc_id, kind, format!("{id} already exists")); false }
                else {
                    match parse_invoice(&b) {
                        Ok((lines, total)) => {
                            self.invoices.insert(id.clone(), Invoice { id, teamspace: ts_id.clone(), client: s(&b, "client").chars().take(200).collect(), lines, total,
                                currency: CURRENCY.into(), co_vendor: b.get("co_vendor").and_then(Value::as_bool).unwrap_or(false), created: at.clone(),
                                author: author_login(ev).unwrap_or_default(), checkout_url: None, paid: 0, refunded: 0, status: "open".into() });
                            true
                        }
                        Err(e) => { self.finding(doc_id, kind, e); false }
                    }
                }
            }
            "invoice.checkout" => match self.invoices.get_mut(s(&b, "invoice")) {
                Some(inv) => { inv.checkout_url = Some(s(&b, "url").to_string()).filter(|u| !u.is_empty()); true }
                None => false,
            },
            "payment.received" => {
                let pid = s(&b, "id").to_string();
                let pref = s(&b, "provider_ref").to_string();
                let amount = b.get("amount").and_then(Value::as_i64).unwrap_or(0);
                match (Method::parse(s(&b, "method")), self.invoices.get_mut(s(&b, "invoice"))) {
                    (Ok(m), Some(inv)) if amount > 0 && !pref.is_empty() && !self.by_ref.contains_key(&pref) => {
                        inv.paid += amount;
                        self.by_ref.insert(pref.clone(), pid.clone());
                        self.payments.insert(pid.clone(), Payment { id: pid, invoice: inv.id.clone(), teamspace: inv.teamspace.clone(), amount, method: m,
                            provider_ref: pref, ts: at.clone(), recorded_by: b.get("recorded_by").and_then(Value::as_str).map(str::to_string) });
                        true
                    }
                    _ => { self.finding(doc_id, kind, "a payment needs a known invoice, an amount, a method and a new provider_ref"); false }
                }
            }
            "fee.charged" => {
                if !self.payments.contains_key(s(&b, "payment")) { self.finding(doc_id, kind, "fee for an unknown payment"); false }
                else {
                    self.fees.push(FeeLine { payment: s(&b, "payment").into(), invoice: s(&b, "invoice").into(), teamspace: ts_id.clone(), line: s(&b, "line").into(),
                        rate_bps: b.get("rate_bps").and_then(Value::as_u64).unwrap_or(0) as u32, amount: b.get("amount").and_then(Value::as_i64).unwrap_or(0),
                        payer: s(&b, "payer").into(), basis: s(&b, "basis").into(), estimated: b.get("estimated").and_then(Value::as_bool).unwrap_or(false),
                        waived: b.get("waived").and_then(Value::as_str).map(str::to_string), ts: at.clone() });
                    true
                }
            }
            "refund" => {
                let amount = b.get("amount").and_then(Value::as_i64).unwrap_or(0);
                match self.payments.get(s(&b, "payment")).cloned() {
                    Some(p) if amount > 0 => {
                        if let Some(inv) = self.invoices.get_mut(&p.invoice) { inv.refunded += amount; }
                        self.refunds.push(Refund { id: s(&b, "id").into(), payment: p.id, invoice: p.invoice, teamspace: p.teamspace, amount, provider_ref: s(&b, "provider_ref").into(), ts: at.clone() });
                        true
                    }
                    _ => { self.finding(doc_id, kind, "a refund needs a known payment and an amount"); false }
                }
            }
            "money.charging" | "money.account" => {
                let owner = author_login(ev).is_some_and(|l| self.teams.role_of(&l, &ts_id) == Some(Role::Owner));
                if !owner { self.finding(doc_id, kind, format!("only an owner of {ts_id} changes its money settings")); false }
                else if kind == "money.charging" {
                    let on = b.get("on").and_then(Value::as_bool).unwrap_or(false);
                    self.charging.insert(ts_id.clone(), Charging { on, since: Some(at.clone()), by: author_login(ev), note: s(&b, "note").chars().take(200).collect() });
                    true
                } else if valid_account(s(&b, "account")) { self.accounts.insert(ts_id.clone(), s(&b, "account").into()); true }
                else { self.finding(doc_id, kind, "not a Stripe account id"); false }
            }
            _ => false,
        };
        if ok { self.lines.entry(ts_id).or_default().push((doc_id.to_string(), ev.clone())); }
        for inv in self.invoices.values_mut() {
            let net = inv.paid - inv.refunded;
            inv.status = if inv.refunded > 0 && net <= 0 { "refunded" } else if net >= inv.total { "paid" } else if net > 0 { "part paid" } else { "open" }.into();
        }
    }

    pub fn charging_on(&self, teamspace: &str) -> bool { self.charging.get(teamspace).is_some_and(|c| c.on) }

    pub fn payment_for_ref(&self, provider_ref: &str) -> Option<&Payment> { self.by_ref.get(provider_ref).and_then(|p| self.payments.get(p)) }

    pub fn totals(&self, teamspace: &str) -> Totals {
        let mut t = Totals::default();
        for i in self.invoices.values().filter(|i| i.teamspace == teamspace) { t.invoiced += i.total; t.outstanding += (i.total - (i.paid - i.refunded)).max(0); }
        for p in self.payments.values().filter(|p| p.teamspace == teamspace) { t.received += p.amount; }
        for r in self.refunds.iter().filter(|r| r.teamspace == teamspace) { t.refunded += r.amount; }
        for f in self.fees.iter().filter(|f| f.teamspace == teamspace) {
            match f.line.as_str() { "platform_fee" => t.platform_fees += f.amount, "provider_cost" => t.provider_costs += f.amount, _ => {} }
        }
        t.nodach_net = t.platform_fees - t.provider_costs;
        t.vendor_net = t.received - t.refunded - t.platform_fees;
        t
    }

    /// The Studio events (kind, item, body) the hub writes for one payment: payment.received, then Nodach's fee line,
    /// then (through Nodach only) Stripe's cost as its own line. Ok(empty) if this provider_ref is already recorded,
    /// so a webhook Stripe sends twice records the payment once.
    pub fn payment_events(&self, invoice: &str, amount: i64, method: Method, provider_ref: &str, provider_cost: Option<i64>, recorded_by: Option<&str>)
        -> Result<Vec<(String, String, Value)>, String> {
        if self.by_ref.contains_key(provider_ref) { return Ok(vec![]); }
        let inv = self.invoices.get(invoice).ok_or_else(|| format!("invoice {invoice} isn't on the record"))?;
        if amount <= 0 { return Err("a payment is more than £0".into()); }
        if provider_ref.trim().is_empty() { return Err("a payment needs its provider reference".into()); }
        let pid = payment_id(provider_ref);
        let q = quote(&self.rules, &inv.teamspace, method, amount, inv.co_vendor, self.charging_on(&inv.teamspace), provider_cost);
        let mut pay = json!({"id": pid, "invoice": inv.id, "teamspace": inv.teamspace, "amount": amount, "currency": CURRENCY, "method": method.key(), "provider_ref": provider_ref});
        if let Some(r) = recorded_by { pay["recorded_by"] = json!(r); }
        let mut fee = json!({"payment": pid, "invoice": inv.id, "teamspace": inv.teamspace, "line": "platform_fee", "rate_bps": q.rate_bps, "amount": q.platform_fee,
            "payer": "vendor", "basis": q.basis, "rules_version": self.rules.version});
        if let Some(w) = &q.waived { fee["waived"] = json!(w); fee["would_be"] = json!(pct_of(amount, q.rate_bps)); }
        let mut out = vec![("payment.received".to_string(), inv.id.clone(), pay), ("fee.charged".to_string(), inv.id.clone(), fee)];
        if method.through_nodach() {
            out.push(("fee.charged".into(), inv.id.clone(), json!({"payment": pid, "invoice": inv.id, "teamspace": inv.teamspace, "line": "provider_cost",
                "rate_bps": 0, "amount": q.provider_cost, "payer": "nodach", "basis": "Stripe's processing fee, paid by Nodach out of its fee", "estimated": q.provider_cost_estimated})));
        }
        Ok(out)
    }

    /// The refund event for a Stripe charge whose refunded total is now `refunded_total` (Stripe reports the running
    /// total): only the part not yet recorded. Ok(None) when nothing new was refunded.
    pub fn refund_event(&self, payment_ref: &str, refunded_total: i64, refund_ref: &str) -> Result<Option<(String, String, Value)>, String> {
        let p = self.payment_for_ref(payment_ref).ok_or_else(|| format!("no payment recorded for {payment_ref}"))?;
        let done: i64 = self.refunds.iter().filter(|r| r.payment == p.id).map(|r| r.amount).sum();
        let delta = refunded_total.min(p.amount) - done;
        if delta <= 0 { return Ok(None); }
        let id = format!("RF-{}-{}", &p.id[4..], refunded_total);
        // TODO(Needs Nowed): whether Nodach's fee is refunded with the payment. v1 keeps it (Stripe keeps its fee too).
        Ok(Some(("refund".into(), p.invoice.clone(), json!({"id": id, "payment": p.id, "invoice": p.invoice, "teamspace": p.teamspace, "amount": delta, "provider_ref": refund_ref,
            "fee_refunded": 0}))))
    }

    /// Stripe Checkout Session parameters (form fields) for an invoice, as a destination charge: the customer pays on
    /// Nodach's platform account, Stripe's fee is debited from Nodach, Nodach keeps `application_fee_amount` (4.5% by
    /// default) and the rest goes to the teamspace's connected account, which is the business of record
    /// (`on_behalf_of`). With no connected account yet (test mode only) the charge stays on the platform account.
    pub fn checkout_form(&self, invoice: &str, base_url: &str) -> Result<(Vec<(String, String)>, Quote), String> {
        let inv = self.invoices.get(invoice).ok_or_else(|| format!("invoice {invoice} isn't on the record"))?;
        let due = inv.total - (inv.paid - inv.refunded);
        if due <= 0 { return Err(format!("{invoice} is already paid")); }
        let q = quote(&self.rules, &inv.teamspace, Method::Card, due, inv.co_vendor, self.charging_on(&inv.teamspace), None);
        let mut f: Vec<(String, String)> = vec![
            ("mode".into(), "payment".into()),
            ("client_reference_id".into(), inv.id.clone()),
            ("success_url".into(), format!("{base_url}/studio#money-paid-{}", inv.id)),
            ("cancel_url".into(), format!("{base_url}/studio#money-cancelled-{}", inv.id)),
            ("metadata[invoice]".into(), inv.id.clone()),
            ("metadata[teamspace]".into(), inv.teamspace.clone()),
            ("payment_intent_data[metadata][invoice]".into(), inv.id.clone()),
            ("payment_intent_data[metadata][teamspace]".into(), inv.teamspace.clone()),
            ("payment_intent_data[description]".into(), format!("Invoice {} for {}", inv.id, inv.client)),
        ];
        if due == inv.total {
            for (i, l) in inv.lines.iter().enumerate() {
                f.push((format!("line_items[{i}][price_data][currency]"), "gbp".into()));
                f.push((format!("line_items[{i}][price_data][product_data][name]"), l.desc.clone()));
                f.push((format!("line_items[{i}][price_data][unit_amount]"), l.unit.to_string()));
                f.push((format!("line_items[{i}][quantity]"), l.qty.to_string()));
            }
        } else {
            f.push(("line_items[0][price_data][currency]".into(), "gbp".into()));
            f.push(("line_items[0][price_data][product_data][name]".into(), format!("Balance of invoice {}", inv.id)));
            f.push(("line_items[0][price_data][unit_amount]".into(), due.to_string()));
            f.push(("line_items[0][quantity]".into(), "1".into()));
        }
        if let Some(acct) = self.accounts.get(&inv.teamspace) {
            f.push(("payment_intent_data[application_fee_amount]".into(), q.platform_fee.to_string()));
            f.push(("payment_intent_data[transfer_data][destination]".into(), acct.clone()));
            f.push(("payment_intent_data[on_behalf_of]".into(), acct.clone()));
        }
        Ok((f, q))
    }

    /// The Money view of one teamspace for `login` (a member or owner of it, or a hub owner). None: not theirs.
    pub fn view_for(&self, login: &str, teamspace: &str, hub_owner: bool) -> Option<Value> {
        let t = self.teams.teamspaces.get(teamspace)?;
        let role = self.teams.role_of(login, teamspace);
        if !hub_owner && !matches!(role, Some(Role::Owner | Role::Member)) { return None; }
        let invoices: Vec<&Invoice> = self.invoices.values().filter(|i| i.teamspace == teamspace).collect();
        let payments: Vec<&Payment> = self.payments.values().filter(|p| p.teamspace == teamspace).collect();
        let fees: Vec<&FeeLine> = self.fees.iter().filter(|f| f.teamspace == teamspace).collect();
        let refunds: Vec<&Refund> = self.refunds.iter().filter(|r| r.teamspace == teamspace).collect();
        let ledger: Vec<Value> = self.lines.get(teamspace).map(|v| v.iter().map(|(d, e)| json!({"doc_id": d, "ts": e.get("ts"), "kind": e.get("kind"), "body": e.get("body")})).collect()).unwrap_or_default();
        let example = quote(&self.rules, teamspace, Method::Card, 10_000, false, true, None);
        Some(json!({"teamspace": teamspace, "label": t.label, "role": role, "owner": role == Some(Role::Owner),
            "charging": self.charging.get(teamspace).cloned().unwrap_or_default(), "account": self.accounts.get(teamspace),
            "rules": self.rules, "rules_node": RULES_ID, "default_rules": FeeRules::default_value(), "override": self.rules.overrides.get(teamspace),
            "example_100": example, "invoices": invoices, "payments": payments, "fees": fees, "refunds": refunds, "ledger": ledger,
            "totals": self.totals(teamspace), "findings": self.findings.iter().filter(|f| self.lines.get(teamspace).is_some_and(|l| l.iter().any(|(d, _)| *d == f.doc_id))).count(),
            "currency": CURRENCY}))
    }
}

/// Check a `Stripe-Signature` header (`t=<unix s>,v1=<hex hmac>[,v1=…]`) against the raw body and the endpoint's
/// signing secret: HMAC-SHA256(secret, "<t>.<body>"), compared in constant time, and `t` within 5 minutes of now.
pub fn verify_signature(header: &str, payload: &str, secret: &str, now_s: i64) -> Result<(), String> {
    if secret.trim().is_empty() { return Err("no webhook signing secret".into()); }
    let mut t: Option<i64> = None;
    let mut sigs = vec![];
    for part in header.split(',') {
        let (k, v) = part.trim().split_once('=').unwrap_or(("", ""));
        match k { "t" => t = v.parse().ok(), "v1" => sigs.push(v.to_string()), _ => {} }
    }
    let t = t.ok_or("the Stripe-Signature header has no timestamp")?;
    if sigs.is_empty() { return Err("the Stripe-Signature header has no v1 signature".into()); }
    if (now_s - t).abs() > SIGNATURE_TOLERANCE_S { return Err("the signature is too old (or from the future)".into()); }
    let want = hex::encode(crate::session::hmac_sha256(secret.trim().as_bytes(), format!("{t}.{payload}").as_bytes()));
    let eq = |a: &[u8], b: &[u8]| a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0;
    if sigs.iter().any(|s| eq(s.as_bytes(), want.as_bytes())) { Ok(()) } else { Err("the signature doesn't match".into()) }
}

/// What a verified Stripe event asks the hub to do.
#[derive(Debug, PartialEq)]
pub enum Webhook {
    /// checkout.session.completed and paid
    Paid { invoice: String, teamspace: String, amount: i64, payment_intent: String, session: String },
    /// charge.refunded (amount_refunded is Stripe's running total for the charge)
    Refunded { payment_intent: String, amount_refunded: i64, charge: String },
    Ignored(String),
}

/// Read a (signature-checked) Stripe event. Live-mode events are refused: this build is test mode only.
pub fn parse_webhook(v: &Value) -> Result<Webhook, String> {
    if v.get("livemode").and_then(Value::as_bool) != Some(false) { return Err("only test-mode events are accepted (livemode must be false)".into()); }
    let o = v.pointer("/data/object").cloned().unwrap_or(Value::Null);
    match s(v, "type") {
        "checkout.session.completed" | "checkout.session.async_payment_succeeded" => {
            if s(&o, "payment_status") != "paid" { return Ok(Webhook::Ignored("checkout not paid yet".into())); }
            if !s(&o, "currency").eq_ignore_ascii_case("gbp") { return Err("only GBP payments are recorded".into()); }
            let inv = o.pointer("/metadata/invoice").and_then(Value::as_str).unwrap_or("").to_string();
            if inv.is_empty() { return Ok(Webhook::Ignored("not a Nodach invoice".into())); }
            Ok(Webhook::Paid { invoice: inv, teamspace: o.pointer("/metadata/teamspace").and_then(Value::as_str).unwrap_or("").into(),
                amount: o.get("amount_total").and_then(Value::as_i64).unwrap_or(0), payment_intent: s(&o, "payment_intent").into(), session: s(&o, "id").into() })
        }
        "charge.refunded" => Ok(Webhook::Refunded { payment_intent: s(&o, "payment_intent").into(),
            amount_refunded: o.get("amount_refunded").and_then(Value::as_i64).unwrap_or(0), charge: s(&o, "id").into() }),
        other => Ok(Webhook::Ignored(format!("{other} isn't used"))),
    }
}

/// Verify, then read, one webhook delivery: the raw body, its Stripe-Signature header and the signing secret.
pub fn read_webhook(header: &str, payload: &str, secret: &str, now_s: i64) -> Result<Webhook, String> {
    verify_signature(header, payload, secret, now_s)?;
    let v: Value = serde_json::from_str(payload).map_err(|_| "the webhook body isn't JSON".to_string())?;
    parse_webhook(&v)
}

impl State {
    /// The events the hub writes for a verified webhook (empty when there is nothing new, e.g. a retry).
    /// `provider_cost` is Stripe's fee for a payment, from its balance transaction (None: estimate, marked).
    pub fn plan(&self, w: &Webhook, provider_cost: Option<i64>) -> Result<Vec<(String, String, Value)>, String> {
        match w {
            Webhook::Paid { invoice, teamspace, amount, payment_intent, session } => {
                let inv = self.invoices.get(invoice).ok_or_else(|| format!("invoice {invoice} isn't on the record"))?;
                if !teamspace.is_empty() && *teamspace != inv.teamspace { return Err(format!("{invoice} isn't in {teamspace}")); }
                let pref = if payment_intent.is_empty() { session.as_str() } else { payment_intent.as_str() };
                self.payment_events(invoice, *amount, Method::Card, pref, provider_cost, None)
            }
            Webhook::Refunded { payment_intent, amount_refunded, charge } => {
                if self.payment_for_ref(payment_intent).is_none() { return Ok(vec![]); }
                Ok(self.refund_event(payment_intent, *amount_refunded, charge)?.into_iter().collect())
            }
            Webhook::Ignored(_) => Ok(vec![]),
        }
    }
}

/// Stripe's own fee for a payment intent fetched with `expand[]=latest_charge.balance_transaction`.
pub fn stripe_fee(pi: &Value) -> Option<i64> {
    pi.pointer("/latest_charge/balance_transaction/fee").and_then(Value::as_i64)
}

/// Form-encode Stripe parameters.
pub fn form(fields: &[(String, String)]) -> String {
    fields.iter().map(|(k, v)| format!("{}={}", crate::oauth::enc(k), crate::oauth::enc(v))).collect::<Vec<_>>().join("&")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r() -> FeeRules { FeeRules::default() }

    #[test]
    fn four_and_a_half_percent_minus_stripe_cost_is_nodachs_net() {
        // £100.00 by card: Nodach charges £4.50; Stripe's fee (reported) is £1.70; Nodach keeps £2.80
        let q = quote(&r(), "TS-a", Method::Card, 10_000, false, true, Some(170));
        assert_eq!((q.rate_bps, q.platform_fee, q.provider_cost, q.nodach_net, q.vendor_net), (450, 450, 170, 280, 9_550));
        assert!(!q.provider_cost_estimated && q.waived.is_none());
        // Stripe's number not known yet: estimated as 1.5% + 20p and marked
        let q = quote(&r(), "TS-a", Method::Card, 10_000, false, true, None);
        assert_eq!((q.provider_cost, q.provider_cost_estimated, q.nodach_net), (170, true, 280));
        // no cap: £1,000,000 pays 4.5% too
        assert_eq!(quote(&r(), "TS-a", Method::Bank, 100_000_000, false, true, Some(0)).platform_fee, 4_500_000);
        // and no minimum: on £1.00 Nodach's net is negative, which the ledger shows rather than hides
        let q = quote(&r(), "TS-a", Method::Card, 100, false, true, None);
        assert_eq!((q.platform_fee, q.provider_cost, q.nodach_net), (5, 22, -17));
    }

    #[test]
    fn fees_round_to_the_nearest_penny_halves_up() {
        assert_eq!(pct_of(1_000, 450), 45);      // 45.0
        assert_eq!(pct_of(1_010, 450), 45);      // 45.45 → 45
        assert_eq!(pct_of(1_100, 450), 50);      // 49.5  → 50 (half up)
        assert_eq!(pct_of(1_099, 450), 49);      // 49.455 → 49
        assert_eq!(pct_of(11, 450), 0);          // 0.495 → 0
        assert_eq!(pct_of(12, 450), 1);          // 0.54 → 1
        assert_eq!(pct_of(-1_100, 450), -50);    // symmetric
        assert_eq!(pct_of(i64::MAX / 2, 10_000), i64::MAX / 2, "no overflow");
        assert_eq!(stripe_estimate(&r(), 1_234), 19 + 20); // 18.51 → 19, + 20p
    }

    #[test]
    fn co_vendor_sales_pay_no_processing_fee() {
        let q = quote(&r(), "TS-a", Method::Card, 25_000, true, true, Some(395));
        assert_eq!((q.rate_bps, q.platform_fee, q.vendor_net), (0, 0, 25_000));
        assert!(q.basis.contains("co-vendor"));
        assert_eq!(q.nodach_net, -395, "Nodach still pays Stripe on a co-vendor sale (flagged for Nowed)");
    }

    #[test]
    fn teamspace_overrides_own_provider_and_recorded_transfers() {
        let rules = FeeRules::parse(&json!({"overrides": {"TS-managed": {"rate_bps": 250, "plan": "managed", "note": "managed-service contract"}}, "plans": {"free": 0, "managed": 75}}), &r()).unwrap();
        let q = quote(&rules, "TS-managed", Method::Card, 10_000, false, true, Some(170));
        assert_eq!((q.rate_bps, q.platform_fee, q.nodach_net), (250, 250, 80));
        assert!(q.basis.contains("managed-service contract"));
        assert_eq!(quote(&rules, "TS-other", Method::Card, 10_000, false, true, Some(170)).platform_fee, 450, "others keep the default");
        // own provider: the placeholder surcharge, no Stripe cost for Nodach
        let q = quote(&rules, "TS-other", Method::OwnProvider, 10_000, false, true, None);
        assert_eq!((q.rate_bps, q.platform_fee, q.provider_cost), (100, 100, 0));
        assert!(q.basis.contains("placeholder"));
        // recorded bank transfers: per the teamspace's plan
        assert_eq!(quote(&rules, "TS-other", Method::RecordedBankTransfer, 10_000, false, true, None).platform_fee, 0);
        assert_eq!(quote(&rules, "TS-managed", Method::RecordedBankTransfer, 10_000, false, true, None).platform_fee, 75);
        // the override wins over the co-vendor rule? No: a co-vendor sale is always free
        assert_eq!(quote(&rules, "TS-managed", Method::Card, 10_000, true, true, None).platform_fee, 0);
        // bad values are refused with the reason
        assert!(FeeRules::parse(&json!({"nodach_bps": 20_000}), &r()).is_err());
        assert!(FeeRules::parse(&json!({"default_plan": "gold"}), &r()).is_err());
    }

    #[test]
    fn before_the_first_test_passes_the_fee_is_waived_but_shown() {
        let q = quote(&r(), "TS-a", Method::Card, 10_000, false, false, Some(170));
        assert_eq!((q.platform_fee, q.nodach_net, q.vendor_net), (0, -170, 10_000));
        assert_eq!(q.waived.as_deref(), Some(WAIVED));
    }

    #[test]
    fn webhook_signatures_are_checked() {
        let secret = "whsec_test_abc";
        let body = r#"{"id":"evt_1","livemode":false}"#;
        let sig = hex::encode(crate::session::hmac_sha256(secret.as_bytes(), format!("1700000000.{body}").as_bytes()));
        let h = format!("t=1700000000,v1=deadbeef,v1={sig}");
        assert_eq!(verify_signature(&h, body, secret, 1_700_000_100), Ok(()));
        assert!(verify_signature(&h, body, secret, 1_700_000_400).is_err(), "too old");
        assert!(verify_signature(&h, &body.replace("false", "true"), secret, 1_700_000_000).is_err(), "body changed");
        assert!(verify_signature(&h, body, "whsec_other", 1_700_000_000).is_err(), "other secret");
        assert!(verify_signature("v1=abc", body, secret, 1_700_000_000).is_err(), "no timestamp");
        assert!(verify_signature(&h, body, "", 1_700_000_000).is_err(), "no secret: refuse");
    }

    #[test]
    fn only_test_keys_and_test_events() {
        // key-shaped strings are built here, never written out whole (the secrets scan rightly flags those)
        let key = |prefix: &str| format!("{prefix}_{}", "x".repeat(12));
        assert!(test_key_ok(&key("sk_test")));
        assert!(test_key_ok(&key("rk_test")));
        assert!(!test_key_ok(&key("sk_live")));
        assert!(!test_key_ok(""));
        assert!(parse_webhook(&json!({"type": "checkout.session.completed", "livemode": true})).is_err());
        assert!(parse_webhook(&json!({"type": "checkout.session.completed"})).is_err(), "livemode must say false");
        let w = parse_webhook(&json!({"type": "checkout.session.completed", "livemode": false, "data": {"object": {"id": "cs_test_1", "payment_status": "paid",
            "currency": "gbp", "amount_total": 12_000, "payment_intent": "pi_1", "metadata": {"invoice": "INV-1", "teamspace": "TS-a"}}}})).unwrap();
        assert_eq!(w, Webhook::Paid { invoice: "INV-1".into(), teamspace: "TS-a".into(), amount: 12_000, payment_intent: "pi_1".into(), session: "cs_test_1".into() });
    }

    #[test]
    fn invoices_must_add_up() {
        let ok = json!({"id": "INV-1", "teamspace": "TS-a", "client": "Insydr", "currency": "GBP", "lines": [{"desc": "Chairs", "qty": 4, "unit": 2_500}, {"desc": "Fitting", "qty": 1, "unit": 1_999}], "total": 11_999});
        assert_eq!(parse_invoice(&ok).unwrap().1, 11_999);
        let mut bad = ok.clone(); bad["total"] = json!(12_000);
        assert!(parse_invoice(&bad).unwrap_err().contains("sum of the lines"));
        let mut usd = ok.clone(); usd["currency"] = json!("USD");
        assert!(parse_invoice(&usd).is_err());
        let mut none = ok; none["lines"] = json!([]);
        assert!(parse_invoice(&none).is_err());
    }
}
