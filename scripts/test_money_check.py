from money_check import check, compare, pct_of


def ev(kind, body, author="github:Morami49", doc=None):
    return (doc or f"d-{kind}-{id(body)}", {"kind": kind, "item": body.get("invoice") or body.get("id"), "body": body, "author": {"id": author}})


def run(kind, body):
    return ev(kind, body, "flow:runner")


INV = ev("invoice.create", {"id": "INV-1", "teamspace": "TS-test", "client": "Dummy", "lines": [{"desc": "t", "qty": 1, "unit": 10000}], "total": 10000})
PAY = run("payment.received", {"id": "PAY-1", "invoice": "INV-1", "teamspace": "TS-test", "amount": 10000, "method": "card", "provider_ref": "pi_1"})
FEE = run("fee.charged", {"payment": "PAY-1", "invoice": "INV-1", "teamspace": "TS-test", "line": "platform_fee", "rate_bps": 450, "amount": 450})
COST = run("fee.charged", {"payment": "PAY-1", "invoice": "INV-1", "teamspace": "TS-test", "line": "provider_cost", "rate_bps": 0, "amount": 170})


def test_rounding_matches_hub_core():
    assert (pct_of(10000, 450), pct_of(11100, 450), pct_of(-11100, 450), pct_of(1, 5000)) == (450, 500, -500, 1)


def test_bug_122_before_and_after():
    # before: invoice and card link only, so every total is £0 (what Nowed saw on 23 Nov)
    t, p = check([INV, run("invoice.checkout", {"invoice": "INV-1", "teamspace": "TS-test", "session": "cs_1"})])
    assert t["TS-test"]["received"] == 0 and t["TS-test"]["outstanding"] == 10000 and not p
    # after the hub found the payment: £100 in, £4.50 to Nodach, £1.70 to Stripe, £2.80 net
    t, p = check([INV, PAY, FEE, COST])
    assert not p, p
    assert {k: t["TS-test"][k] for k in ("received", "platform_fees", "provider_costs", "nodach_net", "vendor_net", "outstanding")} == \
        {"received": 10000, "platform_fees": 450, "provider_costs": 170, "nodach_net": 280, "vendor_net": 9550, "outstanding": 0}


def test_agrees_with_the_hub_view_or_says_where_not():
    t, _ = check([INV, PAY, FEE, COST])
    view = {"teamspace": "TS-test", "totals": dict(t["TS-test"])}
    assert compare(t, view) == []
    view["totals"]["platform_fees"] = 0
    assert compare(t, view) == ["TS-test platform_fees: checker 450, hub 0"]


def test_forged_and_wrong_lines_are_problems():
    forged = ev("payment.received", {"id": "PAY-x", "invoice": "INV-1", "amount": 10000, "method": "card", "provider_ref": "pi_x"})
    _, p = check([INV, forged])
    assert any("not written by the hub" in x for x in p)
    wrong = run("fee.charged", {"payment": "PAY-1", "line": "platform_fee", "rate_bps": 450, "amount": 400})
    _, p = check([INV, PAY, wrong, COST])
    assert any("isn't 450 bps" in x for x in p)
    _, p = check([INV, PAY, FEE])
    assert any("Stripe-cost lines" in x for x in p)
    again = run("payment.received", {"id": "PAY-2", "invoice": "INV-1", "amount": 10000, "method": "card", "provider_ref": "pi_1"})
    _, p = check([INV, PAY, FEE, COST, again])
    assert any("recorded twice" in x for x in p)


def test_waived_fee_carries_would_be():
    waived = run("fee.charged", {"payment": "PAY-1", "line": "platform_fee", "rate_bps": 450, "amount": 0, "waived": "not yet", "would_be": 450})
    t, p = check([INV, PAY, waived, COST])
    assert not p and t["TS-test"]["platform_fees"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
