"""Independent money checker (BQ-thin-slice-insydr, Q61 C: a second implementation re-checks the hub).

Folds the money events of a hub record in plain Python, without hub_core, and checks what the Rust ledger promises:
  - payments, fee lines and refunds count only when the hub's runner wrote them (author flow:runner);
  - each payment has exactly one Nodach fee line, and card/bank payments one Stripe-cost line;
  - Nodach's fee is amount x rate_bps / 10000 rounded to the penny, halves up (0 with would_be when waived);
  - no provider reference is recorded twice; refunds never exceed their payment; invoices add up.
Then it recomputes every teamspace's totals and, given the hub's own Money view (GET /studio/api/money), says whether
the two agree. It reads Studio-shaped events: read_events output {"events": [...]}, /studio-export {"docs": [...]}, or
a plain list of {doc_id, event}. It doesn't check signatures or the hash chain (hub_status does that).

  python scripts/money_check.py events.json [money-view.json ...]
Exit 0: no problems and every view agrees. Exit 1: problems listed.
"""
import json
import sys
from collections import defaultdict

RUNNER = "flow:runner"
HUB_ONLY = {"invoice.checkout", "payment.received", "fee.charged", "refund"}
MONEY = HUB_ONLY | {"invoice.create", "money.charging", "money.account"}
TOTAL_KEYS = ("invoiced", "received", "outstanding", "refunded", "platform_fees", "provider_costs", "nodach_net", "vendor_net")


def pct_of(amount, bps):
    r = (abs(amount) * bps + 5000) // 10000
    return -r if amount < 0 else r


def load(path):
    with open(path, encoding="utf-8") as f:
        v = json.load(f)
    rows = (v.get("events") or v.get("docs") or []) if isinstance(v, dict) else v
    return [(r.get("doc_id", ""), r.get("event", r)) for r in rows]


def check(rows):
    """(totals by teamspace, problems)"""
    inv, pays, fees, refunds, refs, problems = {}, {}, defaultdict(list), [], set(), []
    for doc, e in rows:
        k, b = e.get("kind"), e.get("body") or {}
        if k not in MONEY:
            continue
        if k in HUB_ONLY and (e.get("author") or {}).get("id") != RUNNER:
            problems.append(f"{doc}: {k} not written by the hub's runner (ignored)")
            continue
        if k == "invoice.create":
            lines = b.get("lines") or []
            total = sum(int(l.get("qty", 0)) * int(l.get("unit", 0)) for l in lines)
            if b.get("id") in inv:
                problems.append(f"{doc}: invoice {b.get('id')} created twice")
            elif total != b.get("total") or total <= 0:
                problems.append(f"{doc}: invoice {b.get('id')} total {b.get('total')} isn't the sum of its lines ({total})")
            else:
                inv[b["id"]] = {"ts": b.get("teamspace") or e.get("item"), "total": total, "paid": 0, "refunded": 0}
        elif k == "payment.received":
            i, ref, amt = inv.get(b.get("invoice")), b.get("provider_ref", ""), b.get("amount", 0)
            if not i or amt <= 0 or not ref:
                problems.append(f"{doc}: payment without a known invoice, amount or provider_ref")
            elif ref in refs:
                problems.append(f"{doc}: provider_ref {ref} recorded twice")
            else:
                refs.add(ref); i["paid"] += amt
                pays[b.get("id")] = {"ts": i["ts"], "amount": amt, "method": b.get("method"), "invoice": b.get("invoice")}
        elif k == "fee.charged":
            if b.get("payment") not in pays:
                problems.append(f"{doc}: fee for an unknown payment {b.get('payment')}")
            else:
                fees[b["payment"]].append((doc, b))
        elif k == "refund":
            p = pays.get(b.get("payment"))
            if not p or b.get("amount", 0) <= 0:
                problems.append(f"{doc}: refund without a known payment or amount")
            else:
                refunds.append({"ts": p["ts"], "payment": b["payment"], "amount": b["amount"]})
                inv[p["invoice"]]["refunded"] += b["amount"]
    for pid, p in pays.items():
        lines = fees.get(pid, [])
        nodach = [b for _, b in lines if b.get("line") == "platform_fee"]
        stripe = [b for _, b in lines if b.get("line") == "provider_cost"]
        if len(nodach) != 1:
            problems.append(f"{pid}: {len(nodach)} Nodach fee lines (want 1)")
        want_stripe = 1 if p["method"] in ("card", "bank") else 0
        if len(stripe) != want_stripe:
            problems.append(f"{pid}: {len(stripe)} Stripe-cost lines (want {want_stripe})")
        for f in nodach:
            full = pct_of(p["amount"], int(f.get("rate_bps", 0)))
            if f.get("waived"):
                if f.get("amount") != 0 or f.get("would_be") != full:
                    problems.append(f"{pid}: waived fee should be 0 with would_be {full}, is {f.get('amount')}/{f.get('would_be')}")
            elif f.get("amount") != full:
                problems.append(f"{pid}: Nodach fee {f.get('amount')} isn't {f.get('rate_bps')} bps of {p['amount']} = {full}")
    for pid in {r["payment"] for r in refunds}:
        if sum(r["amount"] for r in refunds if r["payment"] == pid) > pays[pid]["amount"]:
            problems.append(f"{pid}: refunded more than was paid")
    totals = defaultdict(lambda: dict.fromkeys(TOTAL_KEYS, 0))
    for i in inv.values():
        t = totals[i["ts"]]; t["invoiced"] += i["total"]; t["outstanding"] += max(0, i["total"] - (i["paid"] - i["refunded"]))
    for p in pays.values():
        totals[p["ts"]]["received"] += p["amount"]
    for r in refunds:
        totals[r["ts"]]["refunded"] += r["amount"]
    for pid, lines in fees.items():
        t = totals[pays[pid]["ts"]]
        for _, f in lines:
            key = {"platform_fee": "platform_fees", "provider_cost": "provider_costs"}.get(f.get("line"))
            if key:
                t[key] += f.get("amount", 0)
    for t in totals.values():
        t["nodach_net"] = t["platform_fees"] - t["provider_costs"]
        t["vendor_net"] = t["received"] - t["refunded"] - t["platform_fees"]
    return dict(totals), problems


def compare(totals, view):
    """Differences between the checker's totals and the hub's Money view for one teamspace."""
    ts, hub = view.get("teamspace"), view.get("totals") or {}
    mine = totals.get(ts, dict.fromkeys(TOTAL_KEYS, 0))
    return [f"{ts} {k}: checker {mine[k]}, hub {hub.get(k)}" for k in TOTAL_KEYS if mine[k] != hub.get(k)]


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip()); return 2
    totals, problems = check(load(argv[1]))
    for path in argv[2:]:
        with open(path, encoding="utf-8") as f:
            problems += compare(totals, json.load(f))
    gbp = lambda p: f"£{p / 100:,.2f}"
    for ts, t in sorted(totals.items()):
        print(f"{ts}: received {gbp(t['received'])} · Nodach's fees {gbp(t['platform_fees'])} · Stripe {gbp(t['provider_costs'])} · net {gbp(t['nodach_net'])} · outstanding {gbp(t['outstanding'])}")
    for p in problems:
        print("PROBLEM", p)
    print("agrees" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
