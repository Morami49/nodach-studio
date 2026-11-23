//! BQ-1 step 4: money through the whole hub, with a mocked Stripe webhook. An owner's teamspace with a member and a
//! guest; the member creates an invoice; the owner starts charging; a signed test-mode webhook records the payment,
//! Nodach's 4.5% and Stripe's cost as separate lines; the retry records nothing; a forged payment is refused at the
//! door and ignored if it got onto the record; members see the ledger, guests and strangers don't; a refund follows.
use foundation_core::event::KeyPair;
use hub_core::money::{self, Webhook};
use hub_core::*;
use serde_json::{json, Value};

fn ev(author: &str, kind: &str, item: Option<&str>, body: Value, n: u32) -> Value {
    json!({"ts": format!("2026-11-22T12:00:{:02}.000Z", n), "kind": kind, "item": item, "body": body,
           "author": {"id": format!("github:{author}"), "label": author, "role": if author == "Morami49" { "owner" } else { "member" }}})
}

fn runner(kind: &str, item: &str, body: Value) -> Value {
    json!({"ts": "2026-11-22T12:05:00.000Z", "kind": kind, "item": item, "body": body,
           "author": {"id": money::RUNNER_ID, "label": "Flow runner (for stripe webhook)", "role": "agent"}, "via": "flow-runner"})
}

fn signed(secret: &str, body: &str, t: i64) -> String {
    format!("t={t},v1={}", hex::encode(session::hmac_sha256(secret.as_bytes(), format!("{t}.{body}").as_bytes())))
}

#[test]
fn a_signed_test_webhook_records_the_payment_and_both_fee_lines_once() {
    let mut h = Hub::new();
    h.set_owners(vec!["Morami49".into()]);
    let imp = KeyPair::from_seed([9; 32]);
    let k = KeyPair::from_seed([7; 32]);
    let setup = vec![
        ev("Morami49", "teamspace.create", Some("TS-pilot"), json!({"id": "TS-pilot", "label": "Insydr pilot"}), 1),
        ev("Morami49", "member.invite", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-nassar", "role": "member"}), 2),
        ev("nassar", "member.join", Some("TS-pilot"), json!({"teamspace": "TS-pilot"}), 3),
        ev("Morami49", "member.invite", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "profile": "P-gus", "role": "guest"}), 4),
        ev("gus", "member.join", Some("TS-pilot"), json!({"teamspace": "TS-pilot"}), 5),
    ];
    let docs: Vec<(String, Value)> = setup.into_iter().enumerate().map(|(i, e)| (format!("s{i}"), e)).collect();
    h.import(&imp, docs, 1).unwrap();

    // the member creates an invoice; a guest can't; the write rules say so before anything is stored
    let inv = ev("nassar", "invoice.create", Some("INV-1"), json!({"id": "INV-1", "teamspace": "TS-pilot", "client": "Acme Offices", "currency": "GBP",
        "lines": [{"desc": "Desk chairs", "qty": 4, "unit": 2_500}], "total": 10_000}), 6);
    let by_guest = ev("gus", "invoice.create", Some("INV-2"), json!({"id": "INV-2", "teamspace": "TS-pilot", "client": "x", "currency": "GBP",
        "lines": [{"desc": "x", "qty": 1, "unit": 100}], "total": 100}), 7);
    assert_eq!(authorship::refusal(&h.access(), "nassar", &inv), None);
    assert!(authorship::refusal(&h.access(), "gus", &by_guest).unwrap().contains("only members and owners"));
    let charging = ev("Morami49", "money.charging", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "on": true, "note": "first test passed"}), 8);
    let by_member = ev("nassar", "money.charging", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "on": false}), 9);
    assert!(authorship::refusal(&h.access(), "nassar", &by_member).unwrap().contains("only an owner"));
    h.import(&imp, vec![("d-inv".into(), inv), ("d-charge".into(), charging)], 2).unwrap();

    // a forged payment through Studio is refused at the door, even from the hub owner
    let forged = ev("Morami49", "payment.received", Some("INV-1"), json!({"id": "PAY-x", "invoice": "INV-1", "teamspace": "TS-pilot", "amount": 10_000, "method": "card", "provider_ref": "pi_fake"}), 10);
    assert!(authorship::refusal(&h.access(), "Morami49", &forged).unwrap().contains("written only by the hub"));
    // and if one got onto the record anyway (an old import), it counts for nothing
    let as_runner = runner("payment.received", "INV-1", json!({"id": "PAY-y", "invoice": "INV-1", "teamspace": "TS-pilot", "amount": 10_000, "method": "card", "provider_ref": "pi_fake2"}));
    h.import(&imp, vec![("d-forged".into(), as_runner)], 3).unwrap();
    assert_eq!(h.money().totals("TS-pilot").received, 0);
    assert_eq!(h.money().findings.len(), 1);

    // Stripe calls the webhook: checked, read, planned, written by the hub's runner
    let secret = "whsec_test_mock";
    let body = json!({"id": "evt_1", "type": "checkout.session.completed", "livemode": false, "data": {"object": {"id": "cs_test_1", "payment_status": "paid",
        "currency": "gbp", "amount_total": 10_000, "payment_intent": "pi_test_1", "metadata": {"invoice": "INV-1", "teamspace": "TS-pilot"}}}}).to_string();
    let now = 1_763_812_800;
    assert!(money::read_webhook(&signed("whsec_wrong", &body, now), &body, secret, now).is_err(), "a wrong signature is refused");
    let w = money::read_webhook(&signed(secret, &body, now), &body, secret, now + 10).unwrap();
    assert!(matches!(w, Webhook::Paid { amount: 10_000, .. }));
    let plan = h.money().plan(&w, Some(170)).unwrap();
    assert_eq!(plan.iter().map(|(k, _, _)| k.as_str()).collect::<Vec<_>>(), ["payment.received", "fee.charged", "fee.charged"]);
    for (kind, item, b) in plan { h.write(&k, "flow runner", runner(&kind, &item, b), 4).unwrap(); }
    let m = h.money();
    let t = m.totals("TS-pilot");
    assert_eq!((t.received, t.platform_fees, t.provider_costs, t.nodach_net, t.vendor_net, t.outstanding), (10_000, 450, 170, 280, 9_550, 0));
    assert_eq!(m.invoices["INV-1"].status, "paid");
    assert_eq!(m.fees.iter().map(|f| (f.line.as_str(), f.payer.as_str(), f.amount)).collect::<Vec<_>>(), [("platform_fee", "vendor", 450), ("provider_cost", "nodach", 170)]);

    // Stripe retries the same event: nothing new
    assert!(h.money().plan(&w, Some(170)).unwrap().is_empty());

    // who sees the ledger: the member yes, the guest and a stranger no
    let seen = |login: &str| -> usize {
        let (docs, _, _, _) = h.studio_events_for(login, 0, usize::MAX, 0);
        docs.iter().filter(|d| d.pointer("/event/kind").and_then(Value::as_str).is_some_and(money::is_money_kind)).count()
    };
    assert!(seen("nassar") >= 5, "invoice, charging, payment, two fee lines");
    assert_eq!(seen("gus"), 0);
    assert_eq!(seen("stranger"), 0);
    let view = m.view_for("nassar", "TS-pilot", false).unwrap();
    assert_eq!(view["totals"]["nodach_net"], 280);
    assert!(m.view_for("gus", "TS-pilot", false).is_none());

    // a partial refund, then the full one: each records only what is new
    let rbody = |n: i64| json!({"id": "evt_r", "type": "charge.refunded", "livemode": false, "data": {"object": {"id": "ch_1", "payment_intent": "pi_test_1", "amount_refunded": n}}}).to_string();
    for n in [4_000, 4_000, 10_000] {
        let b = rbody(n);
        let w = money::read_webhook(&signed(secret, &b, now), &b, secret, now).unwrap();
        for (kind, item, body) in h.money().plan(&w, None).unwrap() { h.write(&k, "flow runner", runner(&kind, &item, body), 5).unwrap(); }
    }
    let m = h.money();
    assert_eq!(m.refunds.iter().map(|r| r.amount).collect::<Vec<_>>(), [4_000, 6_000]);
    assert_eq!((m.totals("TS-pilot").refunded, m.invoices["INV-1"].status.as_str()), (10_000, "refunded"));
}

#[test]
fn checkout_is_a_destination_charge_with_the_fee_as_application_fee() {
    let mut h = Hub::new();
    h.set_owners(vec!["Morami49".into()]);
    let imp = KeyPair::from_seed([9; 32]);
    let docs = vec![
        ev("Morami49", "teamspace.create", Some("TS-pilot"), json!({"id": "TS-pilot", "label": "Insydr pilot"}), 1),
        ev("Morami49", "invoice.create", Some("INV-9"), json!({"id": "INV-9", "teamspace": "TS-pilot", "client": "Acme", "currency": "GBP",
            "lines": [{"desc": "Chairs", "qty": 2, "unit": 5_000}, {"desc": "Delivery", "qty": 1, "unit": 1_100}], "total": 11_100}), 2),
    ];
    h.import(&imp, docs.into_iter().enumerate().map(|(i, e)| (format!("c{i}"), e)).collect(), 1).unwrap();
    // no connected account yet: a plain test charge on the platform, no application fee
    let (f, q) = h.money().checkout_form("INV-9", "https://staging.nodach.com").unwrap();
    assert!(!f.iter().any(|(k, _)| k.contains("application_fee")));
    assert_eq!(q.waived.as_deref(), Some(money::WAIVED), "charging hasn't started");
    let more = vec![
        ev("Morami49", "money.account", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "provider": "stripe", "account": "acct_1TestPilot"}), 3),
        ev("Morami49", "money.charging", Some("TS-pilot"), json!({"teamspace": "TS-pilot", "on": true}), 4),
    ];
    h.import(&imp, more.into_iter().enumerate().map(|(i, e)| (format!("m{i}"), e)).collect(), 2).unwrap();
    let (f, q) = h.money().checkout_form("INV-9", "https://staging.nodach.com").unwrap();
    let get = |k: &str| f.iter().find(|(a, _)| a == k).map(|(_, v)| v.as_str());
    assert_eq!(get("payment_intent_data[application_fee_amount]"), Some("500"), "4.5% of £111.00 = £4.995 → £5.00");
    assert_eq!(q.platform_fee, 500);
    assert_eq!(get("payment_intent_data[transfer_data][destination]"), Some("acct_1TestPilot"));
    assert_eq!(get("payment_intent_data[on_behalf_of]"), Some("acct_1TestPilot"));
    assert_eq!(get("metadata[invoice]"), Some("INV-9"));
    assert_eq!(get("line_items[1][price_data][unit_amount]"), Some("1100"));
    assert!(money::form(&f).contains("metadata%5Binvoice%5D=INV-9"));
}
