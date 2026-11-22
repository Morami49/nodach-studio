//! P1 c (INC-261118-seed, AUD-secrets-state action 2): the restore drill for the shared record.
//!
//! Take a full export of a hub's record (every `/pull` page), rebuild a fresh hub from it the way a cold start
//! would, and compare. Counts and checksums only: the report never holds event content, so it can be posted.
//! Nothing here writes to any hub; the Private tier and the point-in-time restore are later slices.

use crate::Hub;
use foundation_core::event::Event;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// What the source hub said about itself when the export was taken (`/status`).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Default)]
pub struct Source {
    pub events: usize,
    pub verified: bool,
}

#[derive(Serialize, Deserialize, Debug, PartialEq)]
pub struct Report {
    pub source_events: usize,
    pub exported: usize,
    pub rebuilt: usize,
    pub refused: usize,
    pub duplicates: usize,
    pub verified: bool,
    pub detail: String,
    pub imported_studio_events: usize,
    /// SHA-256 over the sorted event ids: equal when no event was lost or added.
    pub export_set_sha256: String,
    pub rebuilt_set_sha256: String,
    /// A second push of the same export must add nothing (re-sending is always safe).
    pub resend_added: usize,
    pub ok: bool,
    pub problems: Vec<String>,
}

pub fn set_sha256<'a>(ids: impl IntoIterator<Item = &'a String>) -> String {
    let mut v: Vec<&String> = ids.into_iter().collect();
    v.sort();
    v.dedup();
    let mut h = Sha256::new();
    for id in v {
        h.update(id.as_bytes());
        h.update(b"\n");
    }
    hex::encode(h.finalize())
}

pub fn run(source: &Source, export: Vec<Event>) -> Report {
    let exported = export.len();
    let export_set = set_sha256(export.iter().map(|e| &e.id));
    let (mut hub, r) = Hub::restore(export.clone());
    let st = hub.status();
    let rebuilt_events = hub.pull(0, usize::MAX).events;
    let rebuilt_set = set_sha256(rebuilt_events.iter().map(|e| &e.id));
    let resend_added = hub.push(export).added.len();

    let mut problems = vec![];
    if !source.verified { problems.push("the source hub did not verify its own chain".into()); }
    if exported < source.events { problems.push(format!("export has {exported} events, the source reported {}", source.events)); }
    if !r.refused.is_empty() { problems.push(format!("{} events refused on rebuild (first: {})", r.refused.len(), r.refused[0].1)); }
    if r.duplicates > 0 { problems.push(format!("{} duplicate events in the export", r.duplicates)); }
    if st.events != exported { problems.push(format!("rebuilt {} events from {exported}", st.events)); }
    if !st.verified { problems.push(format!("rebuilt chain does not verify: {}", st.detail)); }
    if export_set != rebuilt_set { problems.push("rebuilt event set differs from the export".into()); }
    if resend_added != 0 { problems.push(format!("re-sending the export added {resend_added} events")); }

    Report {
        source_events: source.events,
        exported,
        rebuilt: st.events,
        refused: r.refused.len(),
        duplicates: r.duplicates,
        verified: st.verified,
        detail: st.detail,
        imported_studio_events: st.imported_studio_events,
        export_set_sha256: export_set,
        rebuilt_set_sha256: rebuilt_set,
        resend_added,
        ok: problems.is_empty(),
        problems,
    }
}
