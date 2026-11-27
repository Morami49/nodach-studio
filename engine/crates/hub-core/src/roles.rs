//! Discord-style access (D-access-model, BQ-access-model, 27 Nov 2026): permission switches, named roles in an order,
//! overrides per planet or node, one Owner at the top of each teamspace.
//!
//! This module holds the shapes (switches, roles, overrides) and the pure arithmetic. `teamspaces` folds the events
//! and decides who may do what, because it already knows members, accounts, nodes and shares.
//!
//! Kinds (Studio-shaped; `item` is the teamspace id):
//!   role.create   {teamspace, role, name?, switches[], position?, hidden?}   a new named role
//!   role.edit     {teamspace, role, name?, switches?, position?, hidden?}   rename it, change switches, order or visibility
//!   role.assign   {teamspace, role, profile, on?}                           give (on, the default) or take a role
//!   override.set  {teamspace, target, role, allow[], deny[]}                target "planet:<node type>" or "node:<id>";
//!                                                                          both lists empty removes the override
//!   agent.allow   {teamspace, login, switches[]}                            Owner only: lets one agent hold merge_high
//!                                                                          or hand_on (219a: off for agents by default)
//!
//! Today's roles carry over with their meaning unchanged (222b): owner → Owner (every switch, can't be edited), member →
//! the built-in Member role (read, comment, add and change nodes, link and move nodes), guest → the built-in Guest role
//! (no switches; reads only what is shared), and every signed-in account holds Everyone (no switches by default, 222a).
//! Node shares act as an account's own override on that node, and they only add (viewer: read; commenter: + comment;
//! editor: + add and change, link and move), as before. Role names are free text (222b); a hidden role and its holders
//! are seen only by those who can manage roles (222c).
//!
//! Order (as in Discord): an account's highest role is its rank. You manage only roles below your highest, only people
//! whose highest is below yours, and you can't hand out a switch you don't hold. The Owner is above everyone.
//!
//! How a switch is worked out for an account on a node: Owner → all. Otherwise the switches of every role it holds
//! (Everyone, its base role, assigned roles), then the planet's overrides, then the node's: at each, Everyone's deny
//! then allow, then the other roles' denies together, then their allows together. Then its shares add. Agents lose
//! merge_high and hand_on unless an Owner allowed them.
use serde::{Serialize, Serializer};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

pub const KINDS: [&str; 5] = ["role.create", "role.edit", "role.assign", "override.set", "agent.allow"];

/// D-access-model's switches, in its order. `add_nodes` covers adding and changing nodes (edits, status, answers).
pub const SWITCHES: [&str; 13] = ["read", "comment", "add_nodes", "link_move", "invite", "manage_roles", "run_agents",
    "merge_low", "merge_high", "money", "legal", "change_rules", "hand_on"];

pub const OWNER: &str = "owner";
pub const MEMBER: &str = "member";
pub const GUEST: &str = "guest";
pub const EVERYONE: &str = "everyone";
pub const BUILT_IN: [&str; 4] = [OWNER, MEMBER, GUEST, EVERYONE];
/// The Owner's rank: above every role anyone can make (custom roles sit at 1-999).
pub const OWNER_POS: i32 = 1000;

#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
pub struct Perms(pub u16);

impl Perms {
    pub const NONE: Perms = Perms(0);
    pub const ALL: Perms = Perms((1 << SWITCHES.len()) - 1);

    pub fn bit(name: &str) -> Option<Perms> { SWITCHES.iter().position(|s| *s == name).map(|i| Perms(1 << i)) }
    /// A switch by name; panics on a typo in our own code (names from the record go through `parse`).
    pub fn of(names: &[&str]) -> Perms { Perms(names.iter().map(|n| Self::bit(n).expect("known switch").0).fold(0, |a, b| a | b)) }
    pub fn has(self, name: &str) -> bool { Self::bit(name).is_some_and(|b| self.0 & b.0 != 0) }
    pub fn contains(self, other: Perms) -> bool { self.0 & other.0 == other.0 }
    pub fn union(self, o: Perms) -> Perms { Perms(self.0 | o.0) }
    pub fn minus(self, o: Perms) -> Perms { Perms(self.0 & !o.0) }
    pub fn names(self) -> Vec<&'static str> { SWITCHES.iter().copied().filter(|n| self.has(n)).collect() }

    /// Switch names from an event body (a list of strings); unknown names are refused with the list of known ones.
    pub fn parse(v: Option<&Value>) -> Result<Perms, String> {
        let Some(v) = v else { return Ok(Perms::NONE) };
        let arr = v.as_array().ok_or("switches are a list of names")?;
        let mut p = Perms::NONE;
        for x in arr {
            let n = x.as_str().map(str::trim).unwrap_or("");
            p = p.union(Self::bit(n).ok_or_else(|| format!("unknown switch {n:?}; switches are {}", SWITCHES.join(", ")))?);
        }
        Ok(p)
    }
}

impl Serialize for Perms {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> { self.names().serialize(s) }
}

/// What a share level adds on its node (shares only add, as before the access model).
pub fn share_perms(level: crate::teamspaces::Level) -> Perms {
    use crate::teamspaces::Level::*;
    match level {
        Viewer => Perms::of(&["read"]),
        Commenter => Perms::of(&["read", "comment"]),
        Editor => Perms::of(&["read", "comment", "add_nodes", "link_move"]),
    }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct RoleDef {
    pub id: String,
    pub name: String,
    pub switches: Perms,
    pub position: i32,
    pub hidden: bool,
    /// Profile ids given this role (custom roles only; Member and Guest come from membership, Everyone is everyone).
    pub holders: BTreeSet<String>,
}

#[derive(Serialize, Clone, Copy, Debug, PartialEq, Default)]
pub struct Override { pub allow: Perms, pub deny: Perms }

/// One teamspace's roles, overrides and agent allowances.
#[derive(Clone, Debug, PartialEq)]
pub struct Space {
    pub roles: BTreeMap<String, RoleDef>,
    /// (target, role) → override; target is "planet:<type>" or "node:<id>"
    pub overrides: BTreeMap<(String, String), Override>,
    /// agent login → the agent-limited switches an Owner allowed it (merge_high, hand_on)
    pub agent_allow: BTreeMap<String, Perms>,
}

fn built_in(id: &str, name: &str, switches: Perms, position: i32) -> (String, RoleDef) {
    (id.to_string(), RoleDef { id: id.into(), name: name.into(), switches, position, hidden: false, holders: BTreeSet::new() })
}

impl Default for Space {
    fn default() -> Self {
        Space {
            roles: BTreeMap::from([
                built_in(OWNER, "Owner", Perms::ALL, OWNER_POS),
                built_in(MEMBER, "Member", Perms::of(&["read", "comment", "add_nodes", "link_move"]), 20),
                built_in(GUEST, "Guest", Perms::NONE, 10),
                built_in(EVERYONE, "Everyone", Perms::NONE, 0),
            ]),
            overrides: BTreeMap::new(),
            agent_allow: BTreeMap::new(),
        }
    }
}

/// The switches agents never get by default (219a a).
pub fn agent_limited() -> Perms { Perms::of(&["merge_high", "hand_on"]) }

/// Is this login an agent? Agent keys the hub issues (BQ-sign-in-switch step 3) sign in as `agent:<name>`; the CI
/// token is an agent too. GitHub and email logins can't contain ':', so no person is ever taken for one.
pub fn is_agent(login: &str) -> bool {
    let l = login.trim().to_ascii_lowercase();
    l.starts_with("agent:") || crate::access::is_service(&l)
}

/// Apply one scope's overrides (Discord's order): Everyone's deny then allow, then the other held roles' denies
/// together, then their allows together.
pub fn apply_scope(sp: &Space, scope: &str, held: &[&str], p: Perms) -> Perms {
    let mut p = p;
    if let Some(o) = sp.overrides.get(&(scope.to_string(), EVERYONE.to_string())) { p = p.minus(o.deny).union(o.allow); }
    let (mut allow, mut deny) = (Perms::NONE, Perms::NONE);
    for r in held.iter().filter(|r| **r != EVERYONE) {
        if let Some(o) = sp.overrides.get(&(scope.to_string(), r.to_string())) { allow = allow.union(o.allow); deny = deny.union(o.deny); }
    }
    p.minus(deny).union(allow)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn switches_parse_and_print_by_name() {
        let p = Perms::parse(Some(&json!(["read", "merge_high"]))).unwrap();
        assert!(p.has("read") && p.has("merge_high") && !p.has("comment"));
        assert_eq!(serde_json::to_value(p).unwrap(), json!(["read", "merge_high"]));
        assert!(Perms::parse(Some(&json!(["admin"]))).unwrap_err().contains("unknown switch"));
        assert_eq!(Perms::ALL.names().len(), SWITCHES.len());
    }

    #[test]
    fn overrides_follow_discords_order() {
        let mut sp = Space::default();
        let k = |t: &str, r: &str| (t.to_string(), r.to_string());
        // Everyone denied read on the planet, the Member role allowed it again: members read, Everyone alone doesn't
        sp.overrides.insert(k("planet:idea", EVERYONE), Override { allow: Perms::NONE, deny: Perms::of(&["read"]) });
        sp.overrides.insert(k("planet:idea", MEMBER), Override { allow: Perms::of(&["read"]), deny: Perms::NONE });
        let base = Perms::of(&["read", "comment"]);
        assert!(apply_scope(&sp, "planet:idea", &[EVERYONE, MEMBER], base).has("read"));
        assert!(!apply_scope(&sp, "planet:idea", &[EVERYONE], base).has("read"));
        // a role's allow beats another role's deny at the same scope
        sp.overrides.insert(k("node:N", "a"), Override { allow: Perms::NONE, deny: Perms::of(&["comment"]) });
        sp.overrides.insert(k("node:N", "b"), Override { allow: Perms::of(&["comment"]), deny: Perms::NONE });
        assert!(apply_scope(&sp, "node:N", &[EVERYONE, "a", "b"], base).has("comment"));
        assert!(!apply_scope(&sp, "node:N", &[EVERYONE, "a"], base).has("comment"));
    }

    #[test]
    fn agents_are_only_agent_logins_and_the_ci_token() {
        assert!(is_agent("agent:sam") && is_agent("service:deploy-check"));
        assert!(!is_agent("morami49") && !is_agent("email:abc"));
    }
}
