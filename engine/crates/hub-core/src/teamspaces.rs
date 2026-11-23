//! Teamspaces (FUN-1 v2, BQ-1 step 2, 22 Nov 2026): shared areas inside the one graph.
//!
//! A teamspace is not a second database: it is a set of events on the same record that say who is in it, with what
//! role, and which nodes are in it or shared into it. Membership is held by profiles (a person works as a profile, see
//! `profiles`), and rights are worked out per account: a person's role in a teamspace is the strongest role any of their
//! active profiles holds there.
//!
//! Kinds (all Studio-shaped: {ts, kind, item, body, author}; `item` is the teamspace id, or the node for node.share):
//!   teamspace.create {id, label, profile?}            the author's profile (default: the window's, else Personal) is owner
//!   member.invite    {teamspace, profile, role}       owner only; role owner | member | guest
//!   member.join      {teamspace, profile}             the invited profile's own account accepts the invitation
//!   member.role      {teamspace, profile, role}       owner only; a teamspace always keeps at least one owner
//!   member.remove    {teamspace, profile}             an owner removes anyone, a person removes (leaves) themselves
//!   node.share       {node, profile | teamspace, level}   level viewer | commenter | editor | none (unshare)
//! A node is in a teamspace when its `item.create` says `teamspace: <id>`; that is a link, not part of its id
//! (D-nodach-ids). Each teamspace's money rules are a node in it (type setting, meta.money_rules); its ledger is hub_core::money (BQ-1 step 4).
//!
//! Why new kinds and not access.grant / profile.assign: access.grant is the hub owner's grant to a sign-in by item id
//! prefix (one owner, no roles), access.invite is a sign-in invitation by email hash, and profile.assign moves an
//! item's ownership between one account's profiles. None of them has roles inside a shared area, and changing their
//! meaning would change who can see what on the existing record. They are reused as they are: a guest still signs in
//! through access.invite, and members are profiles from `profiles`.
//!
//! Rules (enforced in `decide` before anything is stored, and again in `fold`, where a rule break is kept, ignored and
//! reported as a finding, like `profiles`):
//!   1. only an owner of a teamspace changes roles, invites or removes others;
//!   2. guests read only what is shared with them (a node shared to them or to the teamspace); members and owners read
//!      every node in the teamspace;
//!   3. a write into a teamspace needs member (or owner) rights, or an editor share on that node; a commenter share
//!      allows comments only;
//!   4. a node is shared by its author or an owner of its teamspace (or a hub owner, for nodes outside teamspaces).
//!
//! Decision 114a (a): these rules bind the hub owner too. A hub owner has no role inside anyone's teamspace by being
//! the hub owner; every write is judged on its own author.
//! Decision 114b (a): the hub owner reads only the teamspaces he is a member of. Any other teamspace is reachable only
//! through a logged support access event:
//!   support.access   {teamspace, reason}              hub owner only; opens that teamspace to him for reading, for
//!                                                     24 hours from the event's ts (SUPPORT_MS), then closes by itself.
//! The event is shown to that teamspace's owners (their "Support access log"), so access is never silent. It grants
//! reads only: writes still need a role there (114a). The hub owner sees a directory of other teamspaces (id and
//! name only, `directory_for`) so he can ask; nothing inside them.
//! Rollback: revert the PR. The events stay on the record (append-only) and simply stop meaning anything.
use crate::profiles;
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};

pub const KINDS: [&str; 7] = ["teamspace.create", "member.invite", "member.join", "member.role", "member.remove", "node.share", "support.access"];

/// How long one support.access event opens a teamspace to the hub owner (114b a): 24 hours.
pub const SUPPORT_MS: i64 = 24 * 60 * 60 * 1000;

/// `YYYY-MM-DDTHH:MM:SS(.fff)Z` → milliseconds since 1970 (days-from-civil, Howard Hinnant). None if it isn't that shape.
pub fn iso_ms(ts: &str) -> Option<i64> {
    let b = ts.trim().as_bytes();
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' || b[13] != b':' || b[16] != b':' { return None; }
    let num = |r: std::ops::Range<usize>| std::str::from_utf8(&b[r]).ok()?.parse::<i64>().ok();
    let (y, m, d, hh, mm, ss) = (num(0..4)?, num(5..7)?, num(8..10)?, num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) || hh > 23 || mm > 59 || ss > 60 { return None; }
    let mut ms = 0;
    if b[19] == b'.' {
        let frac: String = b[20..].iter().take_while(|c| c.is_ascii_digit()).map(|&c| c as char).collect();
        if frac.is_empty() { return None; }
        ms = format!("{:0<3}", &frac[..frac.len().min(3)]).parse::<i64>().ok()?;
    }
    let y2 = if m <= 2 { y - 1 } else { y };
    let era = y2.div_euclid(400);
    let yoe = y2 - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(((days * 86_400) + hh * 3600 + mm * 60 + ss) * 1000 + ms)
}

/// One support access (114b a): who opened which teamspace, when, why, and until when.
#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Support {
    pub doc_id: String,
    pub teamspace: String,
    pub by: String,
    pub reason: String,
    pub at: String,
    pub until_ms: i64,
}

#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Role { Guest, Member, Owner }

#[derive(Serialize, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Level { Viewer, Commenter, Editor }

fn role(s: &str) -> Result<Role, String> {
    match s.trim() {
        "owner" => Ok(Role::Owner),
        "member" => Ok(Role::Member),
        "guest" => Ok(Role::Guest),
        x => Err(format!("role must be owner, member or guest (got {x:?})")),
    }
}

fn level(s: &str) -> Result<Option<Level>, String> {
    match s.trim() {
        "viewer" => Ok(Some(Level::Viewer)),
        "commenter" => Ok(Some(Level::Commenter)),
        "editor" => Ok(Some(Level::Editor)),
        "none" => Ok(None),
        x => Err(format!("level must be viewer, commenter, editor or none (got {x:?})")),
    }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Teamspace {
    pub id: String,
    pub label: String,
    pub created: String,
    /// profile id → role
    pub members: BTreeMap<String, Role>,
    /// profile id → the role it was invited with (until it joins or is removed)
    pub invites: BTreeMap<String, Role>,
}

#[derive(Default, Clone, Debug)]
pub struct State {
    pub teamspaces: BTreeMap<String, Teamspace>,
    /// node id → the teamspace it was created in
    pub item_space: HashMap<String, String>,
    /// node id → the account that created it
    item_author: HashMap<String, String>,
    /// node id → share target (a profile id or a teamspace id) → level
    pub shares: HashMap<String, BTreeMap<String, Level>>,
    pub findings: Vec<profiles::Finding>,
    /// 114b a: every accepted support.access, in record order
    pub support: Vec<Support>,
    profiles: profiles::State,
    hub_owners: Vec<String>,
    /// The clock reads are judged at (ms); None: no support access is open (safe default)
    now_ms: Option<i64>,
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).map(str::trim).unwrap_or("")
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 80 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.:".contains(&b))
}

fn lc(s: &str) -> String { s.trim().to_ascii_lowercase() }

/// The GitHub login an event is authored by, lowercased.
fn author_login(ev: &Value) -> Option<String> {
    ev.pointer("/author/id").and_then(Value::as_str).and_then(|a| a.strip_prefix("github:")).map(lc).filter(|l| !l.is_empty())
}

/// The teamspace an event is about: body.teamspace, else body.id (create), else item.
pub fn teamspace_of(ev: &Value) -> String {
    let b = ev.get("body").cloned().unwrap_or(Value::Null);
    let key = if s(ev, "kind") == "teamspace.create" { "id" } else { "teamspace" };
    match s(&b, key) { "" => s(ev, "item").to_string(), v => v.to_string() }
}

fn node_of(ev: &Value) -> String {
    let b = ev.get("body").cloned().unwrap_or(Value::Null);
    let n = s(&b, "node");
    if !n.is_empty() { n.to_string() } else { s(ev, "item").to_string() }
}

/// The node an ordinary event is about (its item, or the id an item.create makes).
fn subject(ev: &Value) -> Option<String> {
    crate::access::subject(ev).filter(|x| !x.is_empty())
}

impl State {
    /// Fold Studio-shaped events, in record order. `hub_owners` are the hub's owner logins (they may share nodes that
    /// are outside every teamspace, e.g. genesis items; they get no role inside anyone's teamspace).
    pub fn fold<'a>(profiles: profiles::State, hub_owners: &[String], docs: impl IntoIterator<Item = (&'a str, &'a Value)>) -> Self {
        let mut st = State { profiles, hub_owners: hub_owners.iter().map(|o| lc(o)).collect(), ..State::default() };
        for (id, ev) in docs { st.observe(id, ev); }
        st
    }

    pub fn observe(&mut self, doc_id: &str, ev: &Value) {
        let kind = s(ev, "kind");
        if kind == "item.create" {
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            let id = s(&b, "id").to_string();
            if id.is_empty() || self.item_author.contains_key(&id) { return; } // F-19: a second create changes nothing
            if let Some(a) = author_login(ev) { self.item_author.insert(id.clone(), self.account_of_login(&a)); }
            let ts = s(&b, "teamspace");
            if !ts.is_empty() {
                // kept in the teamspace even when the author had no right to write there, so it stays hidden
                if let Some(Err(reason)) = self.decide(ev) {
                    self.findings.push(profiles::Finding { doc_id: doc_id.into(), kind: kind.into(), reason });
                }
                self.item_space.insert(id, ts.to_string());
            }
            return;
        }
        if !KINDS.contains(&kind) { return; }
        if kind == "support.access" {
            match self.support_check(ev) {
                Ok(sup) => self.support.push(Support { doc_id: doc_id.into(), ..sup }),
                Err(reason) => self.findings.push(profiles::Finding { doc_id: doc_id.into(), kind: kind.into(), reason }),
            }
            return;
        }
        if let Err(reason) = self.apply(ev) {
            self.findings.push(profiles::Finding { doc_id: doc_id.into(), kind: kind.into(), reason });
        }
    }

    /// Would this teamspace event be accepted now?
    pub fn check(&self, ev: &Value) -> Result<(), String> {
        if s(ev, "kind") == "support.access" { return self.support_check(ev).map(|_| ()); }
        self.clone().apply(ev)
    }

    /// Judge reads at this clock (ms since 1970); support access is open only while `now` is inside its 24 hours.
    pub fn with_now(mut self, now_ms: i64) -> Self { self.now_ms = Some(now_ms); self }

    pub fn is_hub_owner(&self, login: &str) -> bool { self.hub_owners.contains(&lc(login)) }

    /// 114b a: a support.access is written by a hub owner, names a teamspace that exists and says why.
    fn support_check(&self, ev: &Value) -> Result<Support, String> {
        let login = author_login(ev).ok_or("support access needs a signed-in GitHub author")?;
        if !self.is_hub_owner(&login) { return Err("only the hub owner can open support access to a teamspace".into()); }
        let b = ev.get("body").cloned().unwrap_or(Value::Null);
        let ts = s(&b, "teamspace");
        if !self.teamspaces.contains_key(ts) { return Err(format!("teamspace {ts} doesn't exist")); }
        let reason: String = s(&b, "reason").chars().take(500).collect();
        if reason.chars().count() < 3 { return Err("say why you need support access (a few words): it is shown to the teamspace's owners".into()); }
        let at = s(ev, "ts").to_string();
        let start = iso_ms(&at).ok_or("support access needs the event's time (ts)")?;
        Ok(Support { doc_id: String::new(), teamspace: ts.to_string(), by: login, reason, at, until_ms: start + SUPPORT_MS })
    }

    /// Is a support access by `login` to `ts` open at the clock these reads are judged at?
    pub fn support_open(&self, login: &str, ts: &str) -> bool {
        let Some(now) = self.now_ms else { return false };
        let l = lc(login);
        self.support.iter().any(|x| x.teamspace == ts && x.by == l
            && iso_ms(&x.at).is_some_and(|start| start <= now) && now < x.until_ms)
    }

    /// The teamspace an event touches, if any: its own (teamspace kinds), the node's (node.share and node events).
    pub fn space_of_event(&self, ev: &Value) -> Option<String> {
        let kind = s(ev, "kind");
        if kind == "node.share" {
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            let t = s(&b, "teamspace");
            if !t.is_empty() && self.teamspaces.contains_key(t) { return Some(t.to_string()); }
            return self.item_space.get(&node_of(ev)).cloned();
        }
        if KINDS.contains(&kind) { return Some(teamspace_of(ev)).filter(|t| !t.is_empty()); }
        // BQ-1 step 4: a money event belongs to its teamspace (so support access can open it for reading)
        if crate::money::is_money_kind(kind) { return Some(crate::money::teamspace_of(ev)).filter(|t| self.teamspaces.contains_key(t)); }
        let n = subject(ev)?;
        self.item_space.get(&n).cloned()
    }

    /// 114b a: the hub owner's reads. Teamspace events and nodes are his only where he is in the teamspace, where
    /// they are shared with him, or while a support access he opened is running. None: teamspaces have no say.
    pub fn visible_to_hub_owner(&self, login: &str, ev: &Value) -> Option<bool> {
        let v = self.visible(login, ev)?;
        if v { return Some(true); }
        Some(self.space_of_event(ev).is_some_and(|ts| self.support_open(login, &ts)))
    }

    /// For a hub owner: the other teamspaces, by id and name only (never members or nodes), with any open support.
    pub fn directory_for(&self, login: &str) -> Value {
        if !self.is_hub_owner(login) { return json!([]); }
        let l = lc(login);
        json!(self.teamspaces.values().filter(|t| self.role_of(login, &t.id).is_none()).map(|t| {
            let open = self.support.iter().filter(|x| x.teamspace == t.id && x.by == l && self.support_open(login, &t.id)).map(|x| x.until_ms).max();
            json!({"id": t.id, "label": t.label, "support_until": open.map(crate::mcp::iso)})
        }).collect::<Vec<_>>())
    }

    /// The support access log of a teamspace, for its owners only (newest last).
    pub fn support_log_for(&self, login: &str, ts: &str) -> Option<Vec<Support>> {
        (self.role_of(login, ts) == Some(Role::Owner)).then(|| self.support.iter().filter(|x| x.teamspace == ts).cloned().collect())
    }

    fn account_of_login(&self, login: &str) -> String {
        self.profiles.account_of(&format!("github:{}", lc(login))).cloned().unwrap_or_else(|| format!("acct:github:{}", lc(login)))
    }

    /// The account an active profile belongs to; a Personal profile `P-<login>` belongs to that login even before the
    /// account has written anything. Revoked or unknown profiles belong to nobody (they hold no rights).
    fn account_of_profile(&self, pid: &str) -> Option<String> {
        match self.profiles.profiles.get(pid) {
            Some(p) if p.revoked.is_some() => None,
            Some(p) => Some(p.account.clone()),
            None => pid.strip_prefix("P-").filter(|l| crate::session::valid_login(l)).map(|l| self.account_of_login(l)),
        }
    }

    fn acting_profile(ev: &Value, login: &str) -> String {
        ev.pointer("/author/profile").and_then(Value::as_str).map(str::trim).filter(|p| !p.is_empty())
            .map(str::to_string).unwrap_or_else(|| profiles::personal_id(login))
    }

    /// The strongest role any of `login`'s active profiles holds in `ts`.
    pub fn role_of(&self, login: &str, ts: &str) -> Option<Role> {
        let acct = self.account_of_login(login);
        let t = self.teamspaces.get(ts)?;
        t.members.iter().filter(|(p, _)| self.account_of_profile(p).as_deref() == Some(acct.as_str())).map(|(_, r)| *r).max()
    }

    fn invited(&self, login: &str, ts: &str) -> bool {
        let acct = self.account_of_login(login);
        self.teamspaces.get(ts).is_some_and(|t| t.invites.keys().any(|p| self.account_of_profile(p).as_deref() == Some(acct.as_str())))
    }

    /// The strongest share `login` has on `node`: shared to one of their profiles, or to a teamspace they are in.
    pub fn share_level(&self, login: &str, node: &str) -> Option<Level> {
        let acct = self.account_of_login(login);
        self.shares.get(node)?.iter().filter(|(target, _)| {
            if self.teamspaces.contains_key(*target) { self.role_of(login, target).is_some() }
            else { self.account_of_profile(target).as_deref() == Some(acct.as_str()) }
        }).map(|(_, l)| *l).max()
    }

    /// The GitHub logins of a teamspace's owners (BQ-1 step 3: whose queue a usage notice goes to).
    pub fn owner_logins(&self, ts: &str) -> Vec<String> {
        let Some(t) = self.teamspaces.get(ts) else { return vec![] };
        let mut v: Vec<String> = t.members.iter().filter(|(_, r)| **r == Role::Owner)
            .filter_map(|(p, _)| self.account_of_profile(p)?.strip_prefix("acct:github:").map(str::to_string)).collect();
        v.sort(); v.dedup(); v
    }

    /// Teamspaces `login` is in (with their role) or invited to, for Studio and /studio/api/me.
    pub fn view_for(&self, login: &str) -> Value {
        let mine: Vec<Value> = self.teamspaces.values().filter_map(|t| {
            let r = self.role_of(login, &t.id);
            if r.is_none() && !self.invited(login, &t.id) { return None; }
            let mut v = json!({"id": t.id, "label": t.label, "role": r, "invited": r.is_none(), "members": t.members, "invites": t.invites});
            if let Some(log) = self.support_log_for(login, &t.id) { v["support_log"] = json!(log); }
            Some(v)
        }).collect();
        json!({"teamspaces": mine, "others": self.directory_for(login)})
    }

    /// May `login` read this node? None when no teamspace or share is involved (the access rules decide).
    pub fn can_read(&self, login: &str, node: &str) -> Option<bool> {
        if let Some(ts) = self.item_space.get(node) {
            if matches!(self.role_of(login, ts), Some(Role::Owner | Role::Member)) { return Some(true); }
            return Some(self.share_level(login, node).is_some());
        }
        self.share_level(login, node).map(|_| true)
    }

    /// May `login` see this event? None when teamspaces have nothing to say about it.
    pub fn visible(&self, login: &str, ev: &Value) -> Option<bool> {
        let kind = s(ev, "kind");
        // BQ-1 step 4: a teamspace's money is read by its members and owners only
        if crate::money::is_money_kind(kind) { return Some(crate::money::visible(self, login, ev)); }
        if kind == "node.share" {
            let b = ev.get("body").cloned().unwrap_or(Value::Null);
            let node = node_of(ev);
            let to_me = {
                let p = s(&b, "profile");
                !p.is_empty() && self.account_of_profile(p) == Some(self.account_of_login(login))
            };
            let to_my_space = { let t = s(&b, "teamspace"); !t.is_empty() && self.role_of(login, t).is_some() };
            return Some(to_me || to_my_space || self.can_read(login, &node).unwrap_or(false));
        }
        if kind == "support.access" {
            // 114b a: logged where the teamspace's owners see it (the hub owner sees his own as their author)
            return Some(self.role_of(login, &teamspace_of(ev)) == Some(Role::Owner));
        }
        if KINDS.contains(&kind) {
            let ts = teamspace_of(ev);
            return Some(self.role_of(login, &ts).is_some() || self.invited(login, &ts));
        }
        let node = subject(ev)?;
        self.can_read(login, &node)
    }

    /// Whether the event's own author may put it on the record, as far as teamspaces are concerned:
    /// Some(Ok) allowed, Some(Err(why)) refused, None when no teamspace or share is involved.
    /// The check is on the event's author, so an owner importing someone else's event (D-129 sync) can't carry a write
    /// that its author had no right to make.
    pub fn decide(&self, ev: &Value) -> Option<Result<(), String>> {
        let kind = s(ev, "kind");
        // BQ-1 step 4: invoices need member rights, money settings owner rights; payments and fees come from the hub only
        if let Some(v) = crate::money::decide(self, ev) { return Some(v); }
        if KINDS.contains(&kind) { return Some(self.check(ev)); }
        let Some(login) = author_login(ev) else {
            // not a GitHub author: only refuse when it touches a teamspace node
            let n = subject(ev)?;
            return self.item_space.contains_key(&n).then(|| Err("writes into a teamspace need a signed-in author".into()));
        };
        if kind == "item.create" {
            let ts = ev.pointer("/body/teamspace").and_then(Value::as_str).map(str::trim).unwrap_or("");
            if !ts.is_empty() {
                if !self.teamspaces.contains_key(ts) { return Some(Err(format!("teamspace {ts} doesn't exist. Nothing was saved."))); }
                return Some(match self.role_of(&login, ts) {
                    Some(Role::Owner | Role::Member) => Ok(()),
                    Some(Role::Guest) => Err(format!("guests can't add nodes to {ts}: a write into a teamspace needs member or editor rights. Nothing was saved.")),
                    None => Err(format!("you aren't a member of {ts}, so you can't add nodes to it. Nothing was saved.")),
                });
            }
        }
        let node = subject(ev)?;
        let commenting = matches!(kind, "comment" | "retract");
        let lvl = self.share_level(&login, &node);
        let by_share = lvl.is_some_and(|l| l == Level::Editor || (commenting && l >= Level::Commenter));
        if let Some(ts) = self.item_space.get(&node) {
            if matches!(self.role_of(&login, ts), Some(Role::Owner | Role::Member)) || by_share { return Some(Ok(())); }
            let why = match (self.role_of(&login, ts), lvl) {
                (_, Some(Level::Commenter)) => format!("{node} is shared with you as commenter: you can comment, not change it. Nothing was saved."),
                (_, Some(Level::Viewer)) => format!("{node} is shared with you as viewer: you can read it, not change it. Nothing was saved."),
                (Some(Role::Guest), None) => format!("{node} isn't shared with you: guests in {ts} read and write only what is shared with them. Nothing was saved."),
                _ => format!("{node} is in teamspace {ts}: a write there needs member or editor rights. Nothing was saved."),
            };
            return Some(Err(why));
        }
        // outside teamspaces a share only adds rights; the access rules decide the rest
        by_share.then_some(Ok(()))
    }

    fn owners(t: &Teamspace) -> usize { t.members.values().filter(|r| **r == Role::Owner).count() }

    fn apply(&mut self, ev: &Value) -> Result<(), String> {
        let kind = s(ev, "kind");
        let login = author_login(ev).ok_or("teamspace events need a signed-in GitHub author")?;
        let actor = self.account_of_login(&login);
        let b = ev.get("body").cloned().unwrap_or(Value::Null);
        let ts_id = teamspace_of(ev);
        match kind {
            "teamspace.create" => {
                if !valid_id(&ts_id) { return Err("a teamspace id is 1-80 letters, digits or - _ . :".into()); }
                if self.teamspaces.contains_key(&ts_id) || self.item_author.contains_key(&ts_id) { return Err(format!("{ts_id} already exists")); }
                let label: String = s(&b, "label").chars().take(80).collect();
                if label.is_empty() { return Err("give the teamspace a name".into()); }
                let owner = match s(&b, "profile") { "" => Self::acting_profile(ev, &login), p => p.to_string() };
                if self.account_of_profile(&owner).as_deref() != Some(actor.as_str()) {
                    return Err(format!("{owner} isn't one of your active profiles, so it can't own a teamspace"));
                }
                let created = s(ev, "ts").to_string();
                self.teamspaces.insert(ts_id.clone(), Teamspace { id: ts_id, label, created, members: BTreeMap::from([(owner, Role::Owner)]), invites: BTreeMap::new() });
                Ok(())
            }
            "member.invite" | "member.join" | "member.role" | "member.remove" => {
                let is_owner = self.role_of(&login, &ts_id) == Some(Role::Owner);
                let target = match s(&b, "profile") { "" if kind == "member.join" => Self::acting_profile(ev, &login), p => p.to_string() };
                let own_target = self.account_of_profile(&target).as_deref() == Some(actor.as_str());
                let t = self.teamspaces.get_mut(&ts_id).ok_or_else(|| format!("teamspace {ts_id} doesn't exist"))?;
                if !valid_id(&target) { return Err("name the member by profile id".into()); }
                match kind {
                    "member.invite" => {
                        if !is_owner { return Err(format!("only an owner of {ts_id} can invite people")); }
                        let r = role(s(&b, "role"))?;
                        if t.members.contains_key(&target) { return Err(format!("{target} is already in {ts_id}; change their role instead")); }
                        t.invites.insert(target, r);
                    }
                    "member.join" => {
                        if !own_target { return Err(format!("{target} isn't one of your profiles; only the invited person can accept")); }
                        let r = t.invites.remove(&target).ok_or_else(|| format!("{target} hasn't been invited to {ts_id}"))?;
                        t.members.insert(target, r);
                    }
                    "member.role" => {
                        if !is_owner { return Err(format!("only an owner of {ts_id} can change roles")); }
                        let r = role(s(&b, "role"))?;
                        let cur = *t.members.get(&target).ok_or_else(|| format!("{target} isn't a member of {ts_id}"))?;
                        if cur == Role::Owner && r != Role::Owner && Self::owners(t) == 1 {
                            return Err(format!("{ts_id} needs at least one owner: make someone else owner first"));
                        }
                        t.members.insert(target, r);
                    }
                    _ => {
                        if !is_owner && !own_target { return Err(format!("only an owner of {ts_id} can remove someone else")); }
                        let cur = t.members.get(&target).copied();
                        if cur.is_none() && t.invites.remove(&target).is_some() { return Ok(()); }
                        let cur = cur.ok_or_else(|| format!("{target} isn't in {ts_id}"))?;
                        if cur == Role::Owner && Self::owners(t) == 1 {
                            return Err(format!("{ts_id} needs at least one owner: export it, then make someone else owner before leaving"));
                        }
                        t.members.remove(&target);
                    }
                }
                Ok(())
            }
            "node.share" => {
                let node = node_of(ev);
                if !valid_id(&node) { return Err("name the node to share".into()); }
                let lvl = level(s(&b, "level"))?;
                let (p, t) = (s(&b, "profile"), s(&b, "teamspace"));
                let target = match (p.is_empty(), t.is_empty()) {
                    (false, true) if valid_id(p) => p.to_string(),
                    (true, false) if self.teamspaces.contains_key(t) => t.to_string(),
                    (true, false) => return Err(format!("teamspace {t} doesn't exist")),
                    _ => return Err("share with one profile or one teamspace".into()),
                };
                let space = self.item_space.get(&node).cloned();
                let may = self.item_author.get(&node) == Some(&actor)
                    || space.as_ref().is_some_and(|ts| self.role_of(&login, ts) == Some(Role::Owner))
                    || (space.is_none() && self.hub_owners.contains(&login));
                if !may { return Err(format!("only {node}'s author or an owner of its teamspace can share it")); }
                let m = self.shares.entry(node).or_default();
                match lvl { Some(l) => { m.insert(target, l); } None => { m.remove(&target); } }
                Ok(())
            }
            _ => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(author: &str, kind: &str, item: Option<&str>, body: Value) -> Value {
        json!({"ts": "2026-11-22T12:00:00.000Z", "kind": kind, "item": item, "body": body,
               "author": {"id": format!("github:{author}"), "label": author, "role": "member"}})
    }

    /// nowed owns TS-a; rad is a member, gus a guest; N-in is in TS-a, N-shared is in TS-a and shared with gus
    fn world(extra: Vec<Value>) -> State {
        let mut evs = vec![
            ev("nowed", "teamspace.create", Some("TS-a"), json!({"id": "TS-a", "label": "Insydr pilot"})),
            ev("nowed", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-rad", "role": "member"})),
            ev("rad", "member.join", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-rad"})),
            ev("nowed", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-gus", "role": "guest"})),
            ev("gus", "member.join", Some("TS-a"), json!({"teamspace": "TS-a"})),
            ev("nowed", "item.create", None, json!({"id": "N-in", "type": "idea", "title": "in", "teamspace": "TS-a"})),
            ev("rad", "item.create", None, json!({"id": "N-shared", "type": "idea", "title": "shared", "teamspace": "TS-a"})),
            ev("nowed", "node.share", Some("N-shared"), json!({"node": "N-shared", "profile": "P-gus", "level": "commenter"})),
        ];
        evs.extend(extra);
        let ids: Vec<String> = (0..evs.len()).map(|i| format!("d{i}")).collect();
        State::fold(profiles::State::default(), &["nowed".to_string()], ids.iter().map(String::as_str).zip(evs.iter()))
    }

    #[test]
    fn roles_fold_from_events() {
        let st = world(vec![]);
        assert!(st.findings.is_empty(), "{:?}", st.findings);
        assert_eq!(st.role_of("nowed", "TS-a"), Some(Role::Owner));
        assert_eq!(st.role_of("Rad", "TS-a"), Some(Role::Member));
        assert_eq!(st.role_of("gus", "TS-a"), Some(Role::Guest));
        assert_eq!(st.role_of("eve", "TS-a"), None);
    }

    #[test]
    fn only_an_owner_changes_roles() {
        let st = world(vec![]);
        let promote = |who: &str| ev(who, "member.role", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-gus", "role": "member"}));
        assert_eq!(st.decide(&promote("nowed")), Some(Ok(())));
        let r = st.decide(&promote("rad")).unwrap().unwrap_err();
        assert!(r.contains("only an owner of TS-a can change roles"), "{r}");
        assert!(st.decide(&promote("gus")).unwrap().is_err(), "a guest can't promote themselves");
        // invites and removing others are owner-only too; anyone may leave
        assert!(st.decide(&ev("rad", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-eve", "role": "owner"}))).unwrap().is_err());
        assert!(st.decide(&ev("rad", "member.remove", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-gus"}))).unwrap().is_err());
        assert_eq!(st.decide(&ev("rad", "member.remove", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-rad"}))), Some(Ok(())));
        // a forged role change on the record is ignored and reported
        let st2 = world(vec![promote("rad")]);
        assert_eq!(st2.role_of("gus", "TS-a"), Some(Role::Guest));
        assert_eq!(st2.findings.len(), 1);
    }

    #[test]
    fn a_teamspace_keeps_an_owner() {
        let st = world(vec![]);
        let demote = ev("nowed", "member.role", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-nowed", "role": "member"}));
        assert!(st.decide(&demote).unwrap().unwrap_err().contains("at least one owner"));
        let leave = ev("nowed", "member.remove", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-nowed"}));
        assert!(st.decide(&leave).unwrap().is_err());
        let st = world(vec![ev("nowed", "member.role", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-rad", "role": "owner"}))]);
        assert_eq!(st.decide(&leave), Some(Ok(())));
    }

    #[test]
    fn only_the_invited_person_joins() {
        let st = world(vec![ev("nowed", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-eve", "role": "member"}))]);
        assert!(st.decide(&ev("rad", "member.join", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-eve"}))).unwrap().is_err());
        assert!(st.decide(&ev("mal", "member.join", Some("TS-a"), json!({"teamspace": "TS-a"}))).unwrap().unwrap_err().contains("hasn't been invited"));
        assert_eq!(st.decide(&ev("eve", "member.join", Some("TS-a"), json!({"teamspace": "TS-a"}))), Some(Ok(())));
        // the invitee sees the teamspace before joining; a stranger doesn't
        let inv = ev("nowed", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-eve", "role": "member"}));
        assert_eq!(st.visible("eve", &inv), Some(true));
        assert_eq!(st.visible("mal", &inv), Some(false));
    }

    #[test]
    fn guests_read_only_what_is_shared_with_them() {
        let st = world(vec![]);
        let about = |n: &str| ev("nowed", "comment", Some(n), json!({"text": "x"}));
        let create_in = ev("nowed", "item.create", None, json!({"id": "N-in", "teamspace": "TS-a"}));
        assert_eq!(st.visible("gus", &about("N-in")), Some(false));
        assert_eq!(st.visible("gus", &create_in), Some(false));
        assert_eq!(st.visible("gus", &about("N-shared")), Some(true));
        assert_eq!(st.visible("rad", &about("N-in")), Some(true), "members read every node in the teamspace");
        assert_eq!(st.visible("eve", &about("N-shared")), Some(false), "outsiders read nothing in it");
        // the teamspace's own events (members, roles) are for the people in it
        assert_eq!(st.visible("gus", &ev("nowed", "member.invite", Some("TS-a"), json!({"teamspace": "TS-a", "profile": "P-x", "role": "guest"}))), Some(true));
        assert_eq!(st.visible("eve", &ev("nowed", "teamspace.create", Some("TS-a"), json!({"id": "TS-a"}))), Some(false));
        // nodes outside teamspaces are left to the access rules
        assert_eq!(st.visible("gus", &about("D-001")), None);
        // a share to the teamspace reaches guests too; unsharing takes it away
        let st = world(vec![ev("nowed", "node.share", Some("N-in"), json!({"node": "N-in", "teamspace": "TS-a", "level": "viewer"}))]);
        assert_eq!(st.visible("gus", &about("N-in")), Some(true));
        let st = world(vec![ev("nowed", "node.share", Some("N-shared"), json!({"node": "N-shared", "profile": "P-gus", "level": "none"}))]);
        assert_eq!(st.visible("gus", &about("N-shared")), Some(false));
    }

    #[test]
    fn a_write_into_a_teamspace_needs_member_or_editor_rights() {
        let st = world(vec![]);
        let add = |who: &str| ev(who, "item.create", None, json!({"id": "N-new", "type": "idea", "title": "t", "teamspace": "TS-a"}));
        assert_eq!(st.decide(&add("rad")), Some(Ok(())));
        assert!(st.decide(&add("gus")).unwrap().unwrap_err().contains("member or editor rights"));
        assert!(st.decide(&add("eve")).unwrap().unwrap_err().contains("aren't a member"));
        let edit = |who: &str, n: &str| ev(who, "item.edit", Some(n), json!({"title": "changed"}));
        let comment = |who: &str, n: &str| ev(who, "comment", Some(n), json!({"text": "hi"}));
        assert_eq!(st.decide(&edit("rad", "N-in")), Some(Ok(())));
        assert!(st.decide(&edit("gus", "N-shared")).unwrap().unwrap_err().contains("as commenter"));
        assert_eq!(st.decide(&comment("gus", "N-shared")), Some(Ok(())), "a commenter share allows comments");
        assert!(st.decide(&comment("gus", "N-in")).unwrap().unwrap_err().contains("isn't shared with you"));
        assert!(st.decide(&comment("eve", "N-in")).unwrap().is_err());
        // an editor share lets a guest change that node
        let st = world(vec![ev("rad", "node.share", Some("N-shared"), json!({"node": "N-shared", "profile": "P-gus", "level": "editor"}))]);
        assert_eq!(st.decide(&edit("gus", "N-shared")), Some(Ok(())));
        // ordinary nodes outside teamspaces: no opinion
        assert_eq!(st.decide(&edit("gus", "D-001")), None);
    }

    #[test]
    fn only_the_author_or_an_owner_shares_a_node() {
        let st = world(vec![]);
        let share = |who: &str, n: &str| ev(who, "node.share", Some(n), json!({"node": n, "profile": "P-eve", "level": "viewer"}));
        assert_eq!(st.decide(&share("rad", "N-shared")), Some(Ok(())), "the author");
        assert_eq!(st.decide(&share("nowed", "N-shared")), Some(Ok(())), "an owner of its teamspace");
        assert!(st.decide(&share("rad", "N-in")).unwrap().is_err(), "a member can't share someone else's node");
        assert!(st.decide(&share("gus", "N-shared")).unwrap().is_err());
        assert_eq!(st.decide(&share("nowed", "D-001")), Some(Ok(())), "a hub owner shares nodes outside teamspaces");
        assert!(st.decide(&share("rad", "D-001")).unwrap().is_err());
        assert!(st.decide(&ev("nowed", "node.share", Some("N-in"), json!({"node": "N-in", "profile": "P-eve", "level": "admin"}))).unwrap().is_err());
    }

    #[test]
    fn create_needs_an_own_profile_and_a_fresh_id() {
        let st = world(vec![]);
        assert!(st.decide(&ev("rad", "teamspace.create", Some("TS-a"), json!({"id": "TS-a", "label": "again"}))).unwrap().unwrap_err().contains("already exists"));
        assert!(st.decide(&ev("rad", "teamspace.create", Some("TS-b"), json!({"id": "TS-b", "label": "b", "profile": "P-nowed"}))).unwrap().is_err());
        assert!(st.decide(&ev("rad", "teamspace.create", Some("TS-b"), json!({"id": "TS-b", "label": ""}))).unwrap().is_err());
        assert_eq!(st.decide(&ev("rad", "teamspace.create", Some("TS-b"), json!({"id": "TS-b", "label": "Rad's"}))), Some(Ok(())));
    }

    #[test]
    fn iso_ms_reads_studio_timestamps() {
        for ms in [0i64, 1_795_000_000_123, 951_782_400_000 /* 2000-02-29 */, 4_102_444_799_999] {
            assert_eq!(iso_ms(&crate::mcp::iso(ms)), Some(ms));
        }
        assert_eq!(iso_ms("2026-11-22T12:00:00Z"), iso_ms("2026-11-22T12:00:00.000Z"));
        assert_eq!(iso_ms("yesterday"), None);
        assert_eq!(iso_ms("2026-13-01T00:00:00Z"), None);
    }

    #[test]
    fn support_access_rules() {
        let sup = |who: &str, ts: &str, reason: &str| ev(who, "support.access", Some(ts), json!({"teamspace": ts, "reason": reason}));
        // rad owns TS-r; nowed (hub owner) is not in it
        let st = world(vec![ev("rad", "teamspace.create", Some("TS-r"), json!({"id": "TS-r", "label": "Rad's"})),
                            ev("rad", "item.create", None, json!({"id": "N-r", "teamspace": "TS-r"}))]);
        assert_eq!(st.decide(&sup("nowed", "TS-r", "helping with an import")), Some(Ok(())));
        assert!(st.decide(&sup("rad", "TS-a", "nosy")).unwrap().unwrap_err().contains("only the hub owner"));
        assert!(st.decide(&sup("nowed", "TS-zz", "help")).unwrap().unwrap_err().contains("doesn't exist"));
        assert!(st.decide(&sup("nowed", "TS-r", " ")).unwrap().unwrap_err().contains("say why"));
        let about = ev("rad", "comment", Some("N-r"), json!({"text": "x"}));
        let start = iso_ms("2026-11-22T12:00:00.000Z").unwrap();
        // without one: the hub owner reads nothing in it; with one: only inside the 24 hours
        let st0 = st.clone().with_now(start + 1);
        assert_eq!(st0.visible_to_hub_owner("nowed", &about), Some(false));
        let st = world(vec![ev("rad", "teamspace.create", Some("TS-r"), json!({"id": "TS-r", "label": "Rad's"})),
                            ev("rad", "item.create", None, json!({"id": "N-r", "teamspace": "TS-r"})),
                            sup("nowed", "TS-r", "helping with an import")]);
        assert!(st.findings.is_empty(), "{:?}", st.findings);
        assert_eq!(st.clone().with_now(start + 1).visible_to_hub_owner("nowed", &about), Some(true));
        assert_eq!(st.clone().with_now(start + SUPPORT_MS).visible_to_hub_owner("nowed", &about), Some(false));
        assert_eq!(st.visible_to_hub_owner("nowed", &about), Some(false), "no clock, no support access");
        // the log: rad (owner of TS-r) sees it; gus, rad's... nobody else
        assert_eq!(st.visible("rad", &sup("nowed", "TS-r", "x")), Some(true));
        assert_eq!(st.visible("gus", &sup("nowed", "TS-r", "x")), Some(false));
        assert_eq!(st.support_log_for("rad", "TS-r").map(|l| l.len()), Some(1));
        assert_eq!(st.support_log_for("nowed", "TS-r"), None);
        // outside teamspaces the hub owner still reads everything
        assert_eq!(st.visible_to_hub_owner("nowed", &ev("rad", "comment", Some("D-001"), json!({}))), None);
    }
}
