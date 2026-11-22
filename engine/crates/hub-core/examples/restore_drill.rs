//! P1 c restore drill, rebuild step. Reads {"source": {events, verified}, "events": [...]} (written by
//! scripts/restore_drill.py from a hub's /status and /pull) and prints the drill report as JSON.
//! Exit code 0 when the rebuild matches, 1 when it doesn't, 2 on bad input.
use hub_core::drill::{run, Source};
use std::io::Read;

fn main() {
    let mut raw = String::new();
    let res = match std::env::args().nth(1) {
        Some(p) => std::fs::read_to_string(p).map(|s| raw = s),
        None => std::io::stdin().read_to_string(&mut raw).map(|_| ()),
    };
    if let Err(e) = res { eprintln!("cannot read input: {e}"); std::process::exit(2); }
    #[derive(serde::Deserialize)]
    struct Input { source: Source, events: Vec<foundation_core::event::Event> }
    let input: Input = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => { eprintln!("bad input: {e}"); std::process::exit(2); }
    };
    let report = run(&input.source, input.events);
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
    std::process::exit(if report.ok { 0 } else { 1 });
}
